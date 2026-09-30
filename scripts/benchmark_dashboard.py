"""Measure real dashboard SQL against synthetic rows in a disposable test schema.

Only an explicit OBS_TEST_DATABASE_URL targeting a numeric loopback address and
an obs_test* database is accepted. The application URL and .env are never read.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import platform
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from observatory.db import Database, digest
from observatory.models import Filters, RecordInput

SCHEMA_PATTERN = re.compile(r"obs_benchmark_[0-9a-f]{32}\Z")


def validate_target(url: str) -> dict:
    if not url:
        raise ValueError("OBS_TEST_DATABASE_URL must be explicitly supplied")
    try:
        target = conninfo_to_dict(url)
    except psycopg.Error:
        raise ValueError("Invalid test connection setting; credentials were not printed") from None
    if not re.fullmatch(r"obs_test[a-zA-Z0-9_]*", target.get("dbname", "")):
        raise ValueError("Benchmark refuses databases outside obs_test*")
    for field in ("host", "hostaddr"):
        value = target.get(field)
        if field == "hostaddr" and value is None:
            continue
        try:
            is_loopback = ipaddress.ip_address(value or "").is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise ValueError("Benchmark requires an explicit numeric loopback address")
    if target.get("service") or target.get("replication"):
        raise ValueError("Service indirection and replication connections are not supported")
    return target


def verified_connection(url: str, schema: str | None = None):
    target = validate_target(url)
    if schema is not None and not SCHEMA_PATTERN.fullmatch(schema):
        raise ValueError("Unexpected benchmark schema name")
    options = "-c statement_timeout=60000 -c lock_timeout=5000"
    if schema:
        options += f" -c search_path={schema},public"
    conn = psycopg.connect(
        make_conninfo(url, options=options), connect_timeout=5, row_factory=dict_row,
    )
    try:
        actual = conn.execute("""SELECT current_database() AS database,
            inet_server_addr()::text AS address,current_schema() AS schema""").fetchone()
        if actual["database"] != target["dbname"] or not ipaddress.ip_interface(actual["address"]).ip.is_loopback:
            raise ValueError("Connected database is not the requested loopback test database")
        if schema and actual["schema"] != schema:
            raise ValueError("Benchmark schema is not the current search path")
        # Product reads set their own transaction isolation before querying.
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


class BenchmarkDatabase(Database):
    def __init__(self, url: str, schema: str):
        super().__init__(url)
        self.schema = schema
        self.verified_connections = 0

    def connect(self, vector=False):
        if vector:
            raise ValueError("This SQL benchmark does not use vector search")
        conn = verified_connection(self.url, self.schema)
        self.verified_connections += 1
        return conn


def percentile(values: list[float], proportion: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * proportion
    lower, upper = math.floor(position), math.ceil(position)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3)


def seed(db: BenchmarkDatabase, rows: int) -> dict:
    body = ("Synthetic benchmark advertisement. This text is fabricated for SQL testing; "
            "it is not an advertising record, research finding, or CLAIMS prediction. ") * 12
    began = time.perf_counter()
    with db.connect() as conn:
        conn.execute("""CREATE TEMP TABLE synthetic_seed (
            record_id text,version_id text,body text,body_hash text,payload jsonb,labels jsonb
        ) ON COMMIT DROP""")
        with conn.cursor().copy("COPY synthetic_seed FROM STDIN") as copy:
            for i in range(rows):
                record = RecordInput(
                    record_id=f"synthetic-benchmark:{i:08d}", dataset="native",
                    title=f"Synthetic benchmark ad {i:08d}", url=f"https://example.invalid/benchmark/{i}",
                    publisher=f"Synthetic outlet {i % 20 + 1:02d}",
                    sponsor="" if i % 271 == 0 else f"Synthetic company {(i // 20) % 200 + 1:03d}",
                    keyword=f"Synthetic collection term {i % 5 + 1}",
                    published_at=None if i % 11 == 0 else date(2018, 1, 1) + timedelta(days=i % 3287),
                    body=body, retrievable=i % 7 != 0,
                    raw={"synthetic": True, "benchmark_seed": "dashboard-v1"},
                    provenance=[{"type": "synthetic_benchmark", "not_customer_data": True}],
                )
                payload = record.model_dump(mode="json")
                serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                labels = [] if i % 6 == 0 else [f"synthetic_topic_{i % 12:02d}"]
                copy.write_row((record.record_id, digest(serialized), body, digest(body), Jsonb(payload), Jsonb(labels)))
        conn.execute("INSERT INTO records(record_id,dataset,current_version,active) "
                     "SELECT record_id,'native',version_id,true FROM synthetic_seed")
        conn.execute("""INSERT INTO record_versions(version_id,record_id,body,body_hash,payload)
            SELECT version_id,record_id,body,body_hash,payload FROM synthetic_seed""")
        conn.execute("""INSERT INTO annotations(version_id,ordinal,payload)
            SELECT version_id,0,jsonb_build_object('version','claims-calibrated',
                'synthetic',true,'labels',labels) FROM synthetic_seed""")
        for table in ("records", "record_versions", "annotations"):
            conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))
        counts = conn.execute("""SELECT count(*) AS records,
            count(*) FILTER (WHERE v.payload->>'published_at' IS NULL) AS unknown_dates,
            count(*) FILTER (WHERE (v.payload->>'retrievable')::boolean) AS retrievable,
            count(DISTINCT v.payload->>'publisher') AS publishers,
            count(DISTINCT NULLIF(v.payload->>'sponsor','')) AS sponsors
            FROM records r JOIN record_versions v ON v.version_id=r.current_version""").fetchone()
        if counts["records"] != rows:
            raise ValueError("Inserted row count does not match requested synthetic scale")
        counts["versions"] = conn.execute("SELECT count(*) AS n FROM record_versions").fetchone()["n"]
        counts["annotations"] = conn.execute("SELECT count(*) AS n FROM annotations").fetchone()["n"]
        if counts["versions"] != rows or counts["annotations"] != rows:
            raise ValueError("Synthetic version and annotation row counts do not match records")
    return {**counts, "seed_seconds": round(time.perf_counter() - began, 3),
            "body_characters_per_record": len(body), "method": "validated synthetic RecordInput + COPY + SQL inserts"}


def measure(name, operation, repeats: int, warmups: int) -> dict:
    for _ in range(warmups):
        operation()
    samples = []
    for _ in range(repeats):
        began = time.perf_counter()
        result = operation()
        retrieved = time.perf_counter()
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        encoded = time.perf_counter()
        samples.append({"read_ms": (retrieved - began) * 1000,
                        "serialize_ms": (encoded - retrieved) * 1000,
                        "total_ms": (encoded - began) * 1000, "payload_bytes": len(payload)})
    shape = {"returned_rows": len(result)} if isinstance(result, list) else {}
    if isinstance(result, dict):
        if "rows" in result:
            shape = {"returned_rows": len(result["rows"]), "total": result["total"]}
        elif "page" in result:
            shape = {"returned_rows": len(result["page"]["rows"]), "total": result["stats"]["total"],
                     "relationships": len(result["stats"]["relationships"])}
        elif "relationships" in result:
            shape = {key: result[key] for key in ("total_relationships", "total_records", "truncated")}
            shape["returned_relationships"] = len(result["relationships"])
        else:
            shape = {key: len(value) for key, value in result.items() if isinstance(value, list)}
    return {"operation": name, "repeats": repeats, "warmups_excluded": warmups, "result_shape": shape,
            "latency_ms": {field: {"p50": percentile([s[field] for s in samples], .5),
                                    "p95": percentile([s[field] for s in samples], .95)}
                           for field in ("read_ms", "serialize_ms", "total_ms")},
            "payload_bytes": {"min": min(s["payload_bytes"] for s in samples),
                              "max": max(s["payload_bytes"] for s in samples)},
            "samples": samples}


def run(url: str, *, rows=37000, repeats=15, warmups=1) -> dict:
    target = validate_target(url)
    schema = f"obs_benchmark_{uuid4().hex}"
    created = False
    report = {"status": "running", "synthetic": True, "customer_acceptance": "not_tested",
              "started_at_utc": datetime.now(timezone.utc).isoformat(),
              "database": target["dbname"], "schema": schema, "requested_rows": rows,
              "host": target["host"],
              "python_version": platform.python_version(), "platform": platform.platform(),
              "application_version": version("ciss-observatory"), "measurements": [],
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "scope": "existing repository SQL and JSON encoding; synthetic native rows; warmed serial local workload",
              "not_measured": ["production", "concurrent_users", "browser_rendering", "graph_assembly",
                               "real_social_data", "full_import_pipeline", "rag", "embeddings"],
              "customer_latency_threshold": None}
    db = None
    try:
        with verified_connection(url) as conn:
            report["postgresql_version"] = conn.execute("SHOW server_version").fetchone()["server_version"]
            report["postgresql_port"] = int(conn.execute("SHOW port").fetchone()["port"])
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        created = True
        db = BenchmarkDatabase(url, schema)
        db.initialize()
        print("Seeding fabricated benchmark rows in the isolated test schema...", flush=True)
        report["actual_scale"] = seed(db, rows)
        print(f"Seeded {report['actual_scale']['records']:,} actual database rows.", flush=True)
        filters = Filters(dataset="native")
        cases = [
            ("dashboard_all", lambda: db.dashboard(filters, limit=50)),
            ("dashboard_company", lambda: db.dashboard(Filters(sponsors=["Synthetic company 001"]), limit=50)),
            ("page_first_50", lambda: db.public_page(filters, limit=50)),
            ("page_middle_50", lambda: db.public_page(filters, offset=rows // 2, limit=50)),
            ("field_facets", lambda: db.facets("native")),
            ("bounded_company_outlet_relationships_100", lambda: db.network(filters, limit=100)),
            ("collection_metadata_all", lambda: db.knowledge_map_rows(filters)),
        ]
        for name, operation in cases:
            measurement = measure(name, operation, repeats, warmups)
            report["measurements"].append(measurement)
            print(f"{name}: p95 {measurement['latency_ms']['total_ms']['p95']:.1f} ms; "
                  f"payload {measurement['payload_bytes']['max']:,} bytes", flush=True)
        report["status"] = "completed"
    except Exception as exc:
        report["status"] = "failed"
        report["failure_type"] = type(exc).__name__
    finally:
        if db is not None:
            report["verified_query_connections"] = db.verified_connections
        if created:
            with verified_connection(url) as conn:
                owned = conn.execute("SELECT nspowner=current_user::regrole AS owned "
                                     "FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone()
                if not owned or not owned["owned"] or not SCHEMA_PATTERN.fullmatch(schema):
                    raise ValueError("Cannot safely verify benchmark schema ownership for cleanup")
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            report["schema_dropped"] = True
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=37000)
    parser.add_argument("--repeats", type=int, default=15)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--out", type=Path, default=Path("outputs/dashboard_benchmark.json"))
    args = parser.parse_args()
    if not 1 <= args.rows <= 1000000 or not 1 <= args.repeats <= 100 or not 0 <= args.warmups <= 10:
        parser.error("rows must be 1..1000000, repeats 1..100, and warmups 0..10")
    try:
        result = run(os.environ.get("OBS_TEST_DATABASE_URL", ""), rows=args.rows,
                     repeats=args.repeats, warmups=args.warmups)
    except Exception as exc:
        print(f"Benchmark stopped safely: {type(exc).__name__}. No connection credentials were printed.", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved benchmark receipt: {args.out}", flush=True)
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
