"""Back up one exported local snapshot and compare a new, separate restore database.

Uses the project's guarded PostgreSQL operator. No API calls, source writes,
connection switching, overwrites, or database deletion occur. Private dumps stay
under .runtime/backups; reports contain counts and fingerprints, never row text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import local_postgres as pg
import psycopg
from psycopg import sql
from psycopg.rows import dict_row


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_target(database):
    if not re.fullmatch(r"observatory_restore_[a-z0-9_]{1,43}", database):
        raise ValueError("Choose a new database named observatory_restore_<suffix>.")
    return database


def open_snapshot(database):
    conn = pg.connection(database, admin=True)
    conn.autocommit = False
    conn.row_factory = dict_row
    conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    conn.execute("SET LOCAL TIME ZONE 'UTC'")
    identity = conn.execute(
        "SELECT current_database() AS database,current_setting('port')::int AS port,"
        "current_setting('data_directory') AS directory"
    ).fetchone()
    if (
        identity["database"] != database
        or identity["port"] != pg.PORT
        or Path(identity["directory"]).resolve() != pg.DATA.resolve()
    ):
        conn.close()
        raise RuntimeError("Database identity does not match the project cluster.")
    return conn


def schema_digest(database, snapshot=None):
    command = [str(pg.BIN / "pg_dump.exe"), "--schema-only", "--dbname", database]
    if snapshot is not None:
        command.extend(["--snapshot", snapshot])
    ddl = pg.run(command, env=pg.pg_environment()).stdout
    # Recent pg_dump versions randomize only these psql execution guard tokens.
    ddl = re.sub(r"(?m)^\\(?:un)?restrict [A-Za-z0-9]+\r?$", "", ddl)
    return digest(ddl.replace("\r\n", "\n"))


def inventory(conn):
    tables = conn.execute(
        "SELECT n.nspname AS schema,c.relname AS name,pg_get_userbyid(c.relowner) AS owner "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE c.relkind IN ('r','p','m') AND n.nspname NOT LIKE 'pg_%' "
        "AND n.nspname<>'information_schema' ORDER BY n.nspname,c.relname"
    ).fetchall()
    result = {}
    for table in tables:
        query = sql.SQL("SELECT to_jsonb(t)::text AS row FROM {}.{} t").format(
            sql.Identifier(table["schema"]), sql.Identifier(table["name"])
        )
        hashes = sorted(digest(row["row"]) for row in conn.execute(query))
        result[f"{table['schema']}.{table['name']}"] = {
            "rows": len(hashes), "content_sha256": digest("\n".join(hashes)),
            "owner": table["owner"],
        }
    sequences = {}
    for sequence in conn.execute(
        "SELECT schemaname,sequencename,sequenceowner,start_value,min_value,max_value,"
        "increment_by,cycle,cache_size FROM pg_sequences "
        "WHERE schemaname NOT LIKE 'pg_%' ORDER BY schemaname,sequencename"
    ).fetchall():
        state = conn.execute(sql.SQL("SELECT last_value,is_called FROM {}.{}").format(
            sql.Identifier(sequence["schemaname"]), sql.Identifier(sequence["sequencename"])
        )).fetchone()
        sequences[f"{sequence['schemaname']}.{sequence['sequencename']}"] = {
            **sequence, **state,
        }
    extensions = conn.execute(
        "SELECT extname,extversion FROM pg_extension ORDER BY extname"
    ).fetchall()
    from observatory.indexing import profile_snapshot

    return {
        "tables": result, "sequences": sequences, "extensions": extensions,
        "retrieval": profile_snapshot(conn),
    }


def validate_locators(conn):
    versions = {r["version_id"]: r for r in conn.execute(
        "SELECT version_id,record_id,body,body_hash FROM record_versions"
    )}
    invalid_bodies = sum(digest(r["body"]) != r["body_hash"] for r in versions.values())
    chunks = {r["chunk_id"]: r for r in conn.execute("SELECT * FROM chunks")}
    invalid_chunks = 0
    for chunk in chunks.values():
        body = versions.get(chunk["version_id"])
        invalid_chunks += not (
            body and body["record_id"] == chunk["record_id"]
            and 0 <= chunk["start_char"] < chunk["end_char"] <= len(body["body"])
            and body["body"][chunk["start_char"]:chunk["end_char"]] == chunk["text"]
            and digest(chunk["text"]) == chunk["text_hash"]
        )
    current = {r["record_id"]: r["current_version"] for r in conn.execute(
        "SELECT record_id,current_version FROM records"
    )}
    counts = {
        "body_versions": len(versions), "invalid_body_hashes": invalid_bodies,
        "stored_chunks": len(chunks), "invalid_chunk_locators": invalid_chunks,
        "stored_answers": 0, "answer_evidence": 0, "invalid_answer_evidence": 0,
        "answer_citations": 0, "invalid_answer_citations": 0,
        "historical_evidence": 0, "historical_citations": 0,
    }
    for row in conn.execute("SELECT result FROM answer_runs"):
        counts["stored_answers"] += 1
        result = row["result"]
        evidence = {e["evidence_id"]: e for e in result.get("evidence", [])}
        for item in evidence.values():
            counts["answer_evidence"] += 1
            chunk = chunks.get(item["evidence_id"])
            valid = chunk and all([
                item["record_id"] == chunk["record_id"],
                item["version_id"] == chunk["version_id"],
                item["start"] == chunk["start_char"], item["end"] == chunk["end_char"],
                item["text"] == chunk["text"],
            ])
            counts["invalid_answer_evidence"] += not valid
            counts["historical_evidence"] += current.get(item["record_id"]) != item["version_id"]
        for citation in result.get("citations", []):
            counts["answer_citations"] += 1
            item = evidence.get(citation["evidence_id"])
            counts["invalid_answer_citations"] += not (
                item and citation["quote"] and citation["quote"] in item["text"]
            )
            if item:
                counts["historical_citations"] += current.get(item["record_id"]) != item["version_id"]
    return counts


def search_checks(database, probe):
    from observatory.db import Database
    from observatory.models import Filters

    private = pg.private_settings()
    url = psycopg.conninfo.make_conninfo(
        host=pg.HOST, port=pg.PORT, dbname=database,
        user=private["app_user"], password=private["app_password"],
    )
    db = Database(url)
    selections = [
        ("native_ccs", "carbon capture and storage", Filters()),
        ("native_biogas", "biogas", Filters()),
        ("nyt_ccs", "carbon capture", Filters(publishers=["The New York Times"])),
        ("all_ccs", "carbon capture", Filters(dataset="all")),
    ]
    checks = {}
    for name, query, filters in selections:
        evidence = db.search(query, filters)
        if not evidence:
            raise RuntimeError(f"Supported retrieval probe {name} unexpectedly returned no evidence.")
        checks[name] = [
            [e.evidence_id, e.record_id, e.version_id, e.retrieval_sources, e.score]
            for e in evidence
        ]
    if db.search("carbon capture", Filters(record_ids=["restore-check-no-such-record"])):
        raise RuntimeError("Empty-record filter returned evidence outside its scope.")
    checks["empty_record_filter"] = []
    # Reuse an actual stored vector. This checks adapter/search restoration, not
    # relevance for a newly embedded question. No model request is made.
    evidence = db.search("carbon capture", Filters(), vector=probe)
    if not evidence or not any("vector" in e.retrieval_sources for e in evidence):
        raise RuntimeError("Restored vector search did not return vector-channel evidence.")
    checks["hybrid_with_reused_stored_vector"] = [
        [e.evidence_id, e.record_id, e.version_id, e.retrieval_sources, e.score]
        for e in evidence
    ]
    return checks


def run(database, output):
    validate_target(database)
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Evidence output already exists; previous results are never overwritten.")
    if not output.parent.is_dir():
        raise ValueError("Evidence output directory must already exist.")
    pg.verify_server()
    with pg.connection("postgres", admin=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
            raise ValueError("Restore database already exists; no database was changed.")
    private = [pg.ENV_FILE, pg.SECRET_FILE]
    before = [hashlib.sha256(p.read_bytes()).digest() for p in private]
    with open_snapshot(pg.DATABASE) as source:
        snapshot = source.execute("SELECT pg_export_snapshot() AS id").fetchone()["id"]
        source_inventory = inventory(source)
        source_ddl = schema_digest(pg.DATABASE, snapshot)
        probe = source.execute(
            "SELECT e.embedding::text AS vector FROM embeddings e "
            "JOIN chunks c USING(text_hash) JOIN chunk_profile_membership m USING(chunk_id) "
            "JOIN retrieval_state s ON s.active_profile=m.profile_id "
            "JOIN records r ON r.current_version=c.version_id AND r.record_id=c.record_id "
            "WHERE r.active AND e.model='text-embedding-3-small' "
            "ORDER BY c.chunk_id LIMIT 1"
        ).fetchone()
        if not probe:
            raise RuntimeError("This full-corpus check requires real stored active embeddings.")
        probe = json.loads(probe["vector"])
        source_search = search_checks(pg.DATABASE, probe)
        backup = pg.backup(snapshot=snapshot)
    pg.restore(backup, database)
    with open_snapshot(database) as restored:
        restored_inventory = inventory(restored)
        locators = validate_locators(restored)
        restored_ddl = schema_digest(database)
    restored_search = search_checks(database, probe)
    with open_snapshot(pg.DATABASE) as source_after:
        after_inventory = inventory(source_after)
    comparisons = {
        "snapshot_inventory_equals_restore": source_inventory == restored_inventory,
        "schema_dump_equals_restore": source_ddl == restored_ddl,
        "keyword_and_hybrid_search_equal_restore": source_search == restored_search,
        "source_unchanged_during_verification": source_inventory == after_inventory,
        "private_configuration_unchanged": before == [
            hashlib.sha256(p.read_bytes()).digest() for p in private
        ],
        "backup_hash_matches_sidecar": hashlib.sha256(backup.read_bytes()).hexdigest()
        == backup.with_suffix(".dump.sha256").read_text().strip(),
        "all_stored_locators_valid": all(
            locators[key] == 0 for key in locators if key.startswith("invalid_")
        ),
    }
    receipt = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Current local database snapshot restored to a new database in the same cluster",
        "application_package_version": version("ciss-observatory"),
        "passed": all(comparisons.values()), "comparisons": comparisons,
        "backup": {"private_filename": backup.name, "bytes": backup.stat().st_size,
                   "sha256": hashlib.sha256(backup.read_bytes()).hexdigest()},
        "source_database": pg.DATABASE, "restore_database": database,
        "source_schema_sha256": source_ddl, "restored_schema_sha256": restored_ddl,
        "snapshot_inventory": source_inventory, "locator_validation": locators,
        "search_checks": {"stored_vector_dimensions": len(probe),
                          "source_equals_restore": source_search == restored_search,
                          "returned_evidence_per_probe": {key: len(value) for key, value in source_search.items()},
                          "result_sha256": digest(json.dumps(source_search, sort_keys=True))},
        "model_calls": 0, "application_connection_switched": False,
        "boundaries": [
            "Not a Railway snapshot or an independent machine disaster recovery test.",
            "No global roles/passwords, environment configuration, PDFs or preview files in this logical dump.",
            "Content and vector fingerprints plus source locations do not establish semantic answer quality.",
            "Source writes and sequence allocation by another process can fail the unchanged-source check; the exported dump remains a fixed snapshot.",
        ],
    }
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"passed": receipt["passed"], "comparisons": comparisons,
                      "locator_validation": locators, "output": str(output)}, indent=2))
    return 0 if receipt["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore-database", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        return run(args.restore_database, args.output)
    except psycopg.Error as exc:
        print(f"Database verification failed ({type(exc).__name__}, SQLSTATE {exc.sqlstate or 'unavailable'}); credentials and SQL withheld.", file=sys.stderr)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
