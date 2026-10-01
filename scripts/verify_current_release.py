"""Verify an installed current wheel offline; optional local obs_test* fixture DB.

Run with a fresh virtual environment's Python, from outside the source checkout.
This verifies package contents and synthetic engineering fixtures, not production
corpus/embedding/answer equivalence. It never loads dotenv or calls a model.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import io
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from importlib.metadata import distributions, version
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile
from zoneinfo import ZoneInfo


class VerificationError(RuntimeError):
    """Only fixed failure codes are emitted; never connection strings."""


def require(condition, code):
    if not condition:
        raise VerificationError(code)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def deny_python_network(event, arguments):
    if event == "socket.connect":
        raise VerificationError("offline_network_connection_refused")


def package_inventory(source_root, wheel):
    source = source_root.resolve() / "src" / "observatory"
    require(source.is_dir(), "source_package_missing")
    expected = {
        "observatory/" + path.relative_to(source).as_posix(): sha256(path.read_bytes())
        for path in source.rglob("*")
        if path.is_file() and (path.suffix in {".py", ".sql"}
                               or path.relative_to(source).parts[0] == "assets")
    }
    with ZipFile(wheel) as archive:
        require(len(archive.namelist()) == len(set(archive.namelist())), "duplicate_wheel_entries")
        packaged = {name for name in archive.namelist()
                    if name.startswith("observatory/") and not name.endswith("/")
                    and (Path(name).suffix in {".py", ".sql"} or name.startswith("observatory/assets/"))}
        require(packaged == set(expected), "wheel_package_inventory_mismatch")
        for name, checksum in expected.items():
            require(name in archive.namelist(), "wheel_missing_package_file")
            require(sha256(archive.read(name)) == checksum, "wheel_source_mismatch")
    return expected


def test_database_url(environ):
    """An explicit local test URL is the only allowed database authority."""
    from psycopg.conninfo import conninfo_to_dict

    url = environ.get("OBS_TEST_DATABASE_URL", "")
    require(bool(url), "missing_test_database_url")
    try:
        params = conninfo_to_dict(url)
    except Exception:
        raise VerificationError("invalid_test_database_url") from None
    name = params.get("dbname", "")
    require(re.fullmatch(r"obs_test(?:_[A-Za-z0-9_]+)?", name), "non_test_database_refused")
    require(params.get("host") in {"127.0.0.1", "localhost", "::1"}, "non_local_database_refused")
    require(not params.get("hostaddr") or params["hostaddr"] in {"127.0.0.1", "::1"},
            "non_local_database_refused")
    return url, name


def fixture_file(directory, year):
    body = "Header\n" + (
        "This is a synthetic release fixture. The advertiser describes emissions reductions. " * 120
    ) + "\nFooter"
    record = {
        "record_id": f"release-fixture:{year}", "dataset": "native",
        "url": f"https://example.org/release-fixture/{year}",
        "title": f"Synthetic release fixture {year}", "published_at": f"{year}-01-01",
        "body": body, "retrievable": True, "retrieval_ranges": [[7, len(body) - 7]],
        "raw": {"fixture_only": True},
    }
    path = directory / f"fixture-{year}.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def run_cli(*arguments):
    from observatory.cli import main

    previous = sys.argv
    stream = io.StringIO()
    try:
        sys.argv = ["observatory", *map(str, arguments)]
        with contextlib.redirect_stdout(stream):
            main()
    finally:
        sys.argv = previous
    return json.loads(stream.getvalue())


def verify_offline(directory):
    from observatory.indexing import SENTENCE_PROFILE, definition, expected_chunks
    from observatory.migrations import discover_migrations

    path = fixture_file(directory, 2025)
    # Any accidental connection during a CLI dry-run must fail independently of URL.
    from observatory.db import Database
    original = Database.connect

    def forbidden(*args, **kwargs):
        raise VerificationError("dry_run_opened_database")

    Database.connect = forbidden
    try:
        dry = run_cli("import-records", path, "--dataset", "native", "--dry-run", "--out", "")
    finally:
        Database.connect = original
    require(dry["validated"] and dry["mode"] == "upsert", "canonical_dry_run_failed")
    from observatory.import_records import load_records

    record = load_records(path, dataset="native").records[0]
    payload = record.model_dump(mode="json")
    source = {"record_id": record.record_id, "version_id": "synthetic-fixture-version",
              "body": record.body, "payload": payload}
    spec = definition(SENTENCE_PROFILE)
    chunks = expected_chunks(source, spec)
    require(chunks and chunks == expected_chunks(source, spec), "sentence_chunks_not_deterministic")
    require(all(record.body[item["start_char"]:item["end_char"]] == item["text"]
                and 7 <= item["start_char"] < item["end_char"] <= len(record.body) - 7
                for item in chunks), "sentence_fixture_locator_mismatch")
    return {"canonical_dry_run": dry, "migration_versions": [m.version for m in discover_migrations()],
            "sentence_profile_definition": spec, "fixture_sentence_chunks": len(chunks),
            "fixture_chunk_manifest_sha256": sha256(json.dumps(chunks, sort_keys=True).encode())}


def application_table_names():
    """Guard the current migrations' unquoted tables and migration ledger."""
    from observatory.migrations import discover_migrations

    names = {"schema_migrations"}
    for migration in discover_migrations():
        for declaration in re.finditer(
            r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?", migration.sql, re.IGNORECASE
        ):
            name = re.match(r"([a-z_][a-z0-9_]*)\s*\(", migration.sql[declaration.end():],
                            re.IGNORECASE)
            require(name is not None, "unsupported_migration_table_declaration")
            names.add(name[1].lower())
    require(len(names) > 1, "migration_table_inventory_missing")
    return sorted(names)


def require_no_public_application_relations(connection, names):
    """Reject every same-name public relation before creating a test schema."""
    conflicts = connection.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relname=ANY(%s)", (names,),
    ).fetchall()
    require(not conflicts, "public_application_relation_conflict")


def require_application_table_schema(connection, schema, names):
    """Every unqualified application name must resolve to an ordinary test table."""
    resolved = connection.execute(
        "SELECT requested.name,n.nspname AS schema,c.relkind AS kind "
        "FROM unnest(%s::text[]) AS requested(name) "
        "LEFT JOIN pg_class c ON c.oid=to_regclass(quote_ident(requested.name)) "
        "LEFT JOIN pg_namespace n ON n.oid=c.relnamespace", (names,),
    ).fetchall()
    require(len(resolved) == len(names) and {row["name"] for row in resolved} == set(names)
            and all(row["schema"] == schema and row["kind"] == "r" for row in resolved),
            "application_table_schema_mismatch")


def verify_database(directory, url, database_name):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from psycopg.rows import dict_row

    from observatory.db import Database, digest
    from observatory.indexing import SENTENCE_PROFILE, IndexManager
    from observatory.migrations import migration_status
    from observatory.models import Filters

    schema = "obs_release_" + uuid4().hex
    tables = application_table_names()
    tables_verified = False
    original = Database.connect

    def guarded(database, vector=False):
        require(database.url == isolated_url, "unexpected_database_connection")
        connection = original(database, vector=vector)
        try:
            require(connection.execute("SELECT current_database() AS name").fetchone()["name"]
                    == database_name, "connected_database_refused")
            require(connection.execute("SELECT current_schema() AS name").fetchone()["name"]
                    == schema, "connected_schema_refused")
            require_no_public_application_relations(connection, tables)
            if tables_verified:
                require_application_table_schema(connection, schema, tables)
            connection.commit()
            return connection
        except BaseException:
            connection.close()
            raise

    with psycopg.connect(url, row_factory=dict_row, connect_timeout=5) as connection:
        require(connection.execute("SELECT current_database() AS name").fetchone()["name"]
                == database_name, "connected_database_refused")
        require(connection.execute("SELECT extname FROM pg_extension WHERE extname='vector'")
                .fetchone() is not None, "test_database_vector_extension_missing")
        require_no_public_application_relations(connection, tables)
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated_url = make_conninfo(url, options=f"-c search_path={schema},public")
    os.environ["OBS_DATABASE_URL"] = isolated_url
    Database.connect = guarded
    try:
        first_migration = run_cli("migrate")
        db = Database(isolated_url)
        with db.connect() as connection:
            require_application_table_schema(connection, schema, tables)
        tables_verified = True
        repeated = run_cli("migrate")
        require(first_migration["current_version"] == repeated["current_version"]
                and not repeated["pending"], "migration_not_repeatable")
        prepared_empty = run_cli("index-prepare", SENTENCE_PROFILE)
        # An EMPTY test schema can legitimately activate without embeddings.
        require(prepared_empty["chunks"] == prepared_empty["missing_embeddings"] == 0,
                "test_schema_not_empty")
        run_cli("index-activate", SENTENCE_PROFILE, "--expected-source-version",
                prepared_empty["source_data_version"])
        imported = [run_cli("import-records", fixture_file(directory, year), "--dataset", "native", "--out", "")
                    for year in (2025, 2026)]
        repeated_import = run_cli("import-records", directory / "fixture-2026.jsonl",
                                  "--dataset", "native", "--out", "")
        require({row["record_id"] for row in db.public_rows(Filters())}
                == {"release-fixture:2025", "release-fixture:2026"}, "cross_year_upsert_failed")
        require(not any(item["deactivated"] for item in imported)
                and repeated_import["unchanged"] == 1, "upsert_not_idempotent")
        prepared = run_cli("index-prepare", SENTENCE_PROFILE)
        require(prepared["chunks"] > 0 and prepared["missing_embeddings"] > 0
                and prepared["prepared_snapshot_matches"], "sentence_preparation_failed")
        try:
            IndexManager(db).activate(SENTENCE_PROFILE,
                                     expected_source_data_version=prepared["source_data_version"])
        except ValueError as exc:
            require("embeddings are incomplete" in str(exc), "wrong_activation_failure")
        else:
            raise VerificationError("activation_allowed_missing_embeddings")
        with db.connect() as connection:
            chunks = connection.execute("SELECT c.*,v.body FROM chunks c JOIN record_versions v USING(version_id) "
                                        "JOIN chunk_profile_membership m USING(chunk_id) WHERE m.profile_id=%s",
                                        (SENTENCE_PROFILE,)).fetchall()
            require(all(row["body"][row["start_char"]:row["end_char"]] == row["text"]
                        and digest(row["text"]) == row["text_hash"] for row in chunks),
                    "stored_sentence_locator_mismatch")
            counts = {table: connection.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table)))
                      .fetchone()["n"] for table in ("embeddings", "answer_runs", "usage_ledger", "generation_outputs")}
            require(not any(counts.values()), "unexpected_model_or_answer_data")
            status = migration_status(connection)
        return {"database": database_name, "schema_isolated": True, "migration_version": status["current_version"],
                "public_application_relations_absent": True, "resolved_application_tables": len(tables),
                "cross_year_records": 2, "repeat_unchanged": repeated_import["unchanged"],
                "health": db.health(), "sentence_chunks_located": len(chunks),
                "missing_embeddings": prepared["missing_embeddings"],
                "missing_embeddings_activation_blocked": True, "model_tables": counts}
    finally:
        Database.connect = original
        os.environ["OBS_DATABASE_URL"] = ""
        with psycopg.connect(url, row_factory=dict_row, connect_timeout=5) as connection:
            require(connection.execute("SELECT current_database() AS name").fetchone()["name"]
                    == database_name, "cleanup_database_refused")
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--database", action="store_true", help="Explicitly use local OBS_TEST_DATABASE_URL")
    args = parser.parse_args(argv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Refuse to replace earlier evidence.
    with args.output.open("x", encoding="utf-8") as stream:
        now = datetime.now(UTC)
        report = {"checked_at_utc": now.isoformat(),
                  "checked_at_local": now.astimezone(ZoneInfo("America/New_York")).isoformat(),
                  "status": "failed",
                  "scope": "installed_current_wheel_synthetic_engineering_fixtures",
                  "model_calls": 0, "production_corpus_equivalence": "not_verified",
                  "production_vectors_answers_and_usage_restore": "not_verified",
                  "semantic_acceptance": "not_evaluated", "database": "not_requested"}
        try:
            inventory = package_inventory(args.source_root, args.wheel)
            import observatory

            installed = Path(observatory.__file__).resolve().parent
            require(sys.prefix != sys.base_prefix, "virtual_environment_required")
            require(installed.is_relative_to(Path(sys.prefix).resolve())
                    and not installed.is_relative_to(args.source_root.resolve() / "src"),
                    "source_checkout_import_refused")
            for name, checksum in inventory.items():
                path = installed / name.removeprefix("observatory/")
                require(path.is_file() and sha256(path.read_bytes()) == checksum, "installed_wheel_file_mismatch")
            test_url = test_database_url(os.environ) if args.database else None
            # Disable dotenv and every application/model authority except the explicit test URL.
            for name in tuple(os.environ):
                if name.startswith("OBS_") or name in {"OPENAI_API_KEY", "PYTHONPATH"}:
                    os.environ.pop(name, None)
            os.environ.update(PYTHON_DOTENV_DISABLED="1", OPENAI_API_KEY="", OBS_DATABASE_URL="")
            sys.addaudithook(deny_python_network)
            modules = sorted(name[:-3].replace("/", ".") for name in inventory
                             if name.endswith(".py") and not name.endswith("/__init__.py"))
            for module in modules:
                importlib.import_module(module)
            with tempfile.TemporaryDirectory(prefix="obs_release_fixture_") as temp:
                directory = Path(temp)
                report["offline"] = verify_offline(directory)
                if test_url:
                    report["database"] = verify_database(directory, *test_url)
            report.update(status="passed", package_version=version("ciss-observatory"),
                          python_version=sys.version.split()[0], package_location=str(installed),
                          wheel_sha256=sha256(args.wheel.read_bytes()), package_manifest=inventory,
                          verification_script_sha256=sha256(Path(__file__).read_bytes()),
                          dependency_lock_sha256=sha256((args.source_root / "uv.lock").read_bytes()),
                          installed_dependencies={item.metadata["Name"]: item.version for item in distributions()},
                          imported_modules=modules)
        except Exception as exc:
            report["failure_code"] = str(exc) if isinstance(exc, VerificationError) else type(exc).__name__
        json.dump(report, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
    print(json.dumps({"status": report["status"], "output": str(args.output),
                      "failure_code": report.get("failure_code")}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
