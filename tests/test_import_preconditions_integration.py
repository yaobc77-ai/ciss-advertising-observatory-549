"""Opt-in, real guarded-import tests in one owned disposable obs_test schema.

Set OBS_RUN_GUARDED_IMPORT_INTEGRATION=1 to run. The target comes from the explicit
OBS_TEST_DATABASE_URL; .env and shared business tables are never read.
The tests do not start PostgreSQL, install extensions or call a model.
"""

import hashlib
import importlib.util
import ipaddress
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from uuid import uuid4

import psycopg
import pytest
from postgres_fixtures import (
    configured_test_url,
    validate_test_target,
    verified_test_connection,
)
from psycopg import sql

from observatory.db import Database, ImportPreconditionFailed, digest
from observatory.models import ImportBatch, RecordInput

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]
RECEIPT_DIR = ROOT / ".runtime" / "guarded_import_validation_20261006"
CHECKS = {
    "adoption_retry_history", "third_version_and_withdrawal", "whole_batch_prewrite_rejection",
    "two_competing_updates", "row_lock_withdrawal", "two_identical_updates",
    "late_write_atomic_rollback",
}
TABLES = (
    "records", "record_versions", "annotations", "chunks", "chunk_profile_membership",
    "imports", "retrieval_preparations",
)


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


benchmark = _script("benchmark_dashboard")


def local_test_setting():
    """Honor the explicit private test URL without reading main-cluster settings."""
    return configured_test_url()


def verified_connection(setting, schema=None):
    target = validate_test_target(setting)
    conn = verified_test_connection(setting, schema)
    try:
        row = conn.execute("SELECT current_database() AS database, "
                           "inet_server_addr()::text AS address").fetchone()
        if row["database"] != target["dbname"] or str(ipaddress.ip_interface(row["address"]).ip) != target["host"]:
            raise ValueError("Actual server is not the exact loopback test target")
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


class IsolatedDatabase(Database):
    def __init__(self, setting, schema, receipt):
        super().__init__(setting)
        self.schema = schema
        self.receipt = receipt

    def connect(self, vector=False):
        if vector:
            raise ValueError("Guarded import verification does not call vector search")
        return verified_connection(self.url, self.schema)

    def checked(self, name, **details):
        self.receipt["checks"].append({"name": name, "status": "passed", **details})


def _public_relations(conn):
    """Inspect catalogs only, never public business rows."""
    return conn.execute("SELECT c.oid,c.relname,c.relkind FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname='public' ORDER BY c.oid").fetchall()


@pytest.fixture(scope="module")
def isolated_db():
    if os.environ.get("OBS_RUN_GUARDED_IMPORT_INTEGRATION") != "1":
        pytest.skip("Explicit guarded-import integration opt-in is absent")
    schema = f"obs_benchmark_{uuid4().hex}"
    receipt = {
        "schema_version": "guarded-import-validation-v1", "status": "running",
        "database": "obs_test", "host": "numeric_loopback", "schema": schema,
        "started_at": datetime.now(timezone.utc).isoformat(), "synthetic": True,
        "checks": [], "schema_created": False, "schema_dropped": False,
        "shared_business_tables_accessed": False, "model_calls": 0,
        "test_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "database_source_sha256": hashlib.sha256((ROOT / "src/observatory/db.py").read_bytes()).hexdigest(),
        "limits": ["Fabricated rows in one disposable local schema.",
                   "Does not validate customer data, semantic accuracy, deployment or all writer patterns."],
    }
    setting, created, public_before = None, False, None
    try:
        setting = local_test_setting()
        receipt["database"] = validate_test_target(setting)["dbname"]
        with verified_connection(setting) as conn:
            public_before = _public_relations(conn)
            if not conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone():
                raise RuntimeError("ExistingTestVectorExtensionRequired")
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        created = True
        receipt["schema_created"] = True
        db = IsolatedDatabase(setting, schema, receipt)
        db.initialize()
        with db.connect() as conn:
            for table in TABLES:
                row = conn.execute("SELECT n.nspname AS schema FROM pg_class c "
                                   "JOIN pg_namespace n ON n.oid=c.relnamespace "
                                   "WHERE c.oid=to_regclass(%s)", (table,)).fetchone()
                if row is None or row["schema"] != schema:
                    raise ValueError("Business relation did not resolve inside the created schema")
            receipt["server_version"] = conn.execute("SHOW server_version").fetchone()["server_version"]
        yield db
        completed = {entry["name"] for entry in receipt["checks"]}
        receipt["status"] = "completed" if completed == CHECKS else "incomplete"
    except Exception as exc:
        receipt["status"] = "failed"
        receipt["failure_type"] = type(exc).__name__
        # Connection settings or full database exception messages are never printed.
        raise RuntimeError(f"Isolated guarded-import verification failed: {type(exc).__name__}") from None
    finally:
        if created:
            try:
                with verified_connection(setting) as conn:
                    owned = conn.execute("SELECT nspowner=current_user::regrole AS owned "
                                         "FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone()
                    if (not benchmark.SCHEMA_PATTERN.fullmatch(schema)
                            or not owned or owned["owned"] is not True):
                        raise ValueError("Disposable schema ownership is not verified")
                    conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
                    receipt["public_catalog_unchanged"] = _public_relations(conn) == public_before
                receipt["schema_dropped"] = True
            except Exception as exc:
                receipt["status"] = "cleanup_failed"
                receipt["cleanup_failure_type"] = type(exc).__name__
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
        path = RECEIPT_DIR / f"{schema}.receipt.json"
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if created and not receipt["schema_dropped"]:
            raise RuntimeError("Isolated guarded-import schema cleanup failed; inspect the private receipt")


def rec(record_id, **changes):
    fields = {
        "record_id": record_id, "dataset": "native", "url": f"https://example.invalid/{record_id}",
        "title": "Fabricated guarded import source", "sponsor": "Synthetic sponsor",
        "body": "Fabricated emissions proposal for PostgreSQL verification. " * 20,
        "retrievable": True, "raw": {"synthetic": True},
    }
    return RecordInput(**(fields | changes))


def expected(record):
    payload = record.model_dump(mode="json")
    return {"version_id": digest(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            "body_sha256": digest(record.body), "dataset": record.dataset, "url": record.url}


def current(db, record_id):
    with db.connect() as conn:
        return conn.execute("SELECT r.active,r.current_version,v.body,v.payload FROM records r "
                            "JOIN record_versions v ON v.version_id=r.current_version "
                            "WHERE r.record_id=%s", (record_id,)).fetchone()


def snapshot(db):
    with db.connect() as conn:
        return {table: conn.execute(sql.SQL("SELECT COALESCE(jsonb_agg(to_jsonb(t) "
                    "ORDER BY to_jsonb(t)::text),'[]'::jsonb) AS rows FROM {} t").format(sql.Identifier(table)))
                    .fetchone()["rows"] for table in TABLES}


def test_real_adoption_exact_retry_keeps_old_history(isolated_db):
    db = isolated_db
    old = rec("adoption", annotations=[{"version": "synthetic-old", "labels": ["historical"]}])
    new = rec("adoption", body="Reviewed synthetic replacement. " * 20, annotations=[])
    db.import_batch(ImportBatch(records=[old]))
    guard = {old.record_id: expected(old)}
    first = db.import_batch(ImportBatch(records=[new]), expected_current=guard)
    assert first["new_versions"] == 1 and first["precondition_checked"] == 1
    second = db.import_batch(ImportBatch(records=[new]), expected_current=guard)
    assert second["new_versions"] == 0 and second["unchanged"] == 1 and second["already_applied"] == 1
    assert current(db, old.record_id)["body"] == new.body
    with db.connect() as conn:
        versions = conn.execute("SELECT version_id FROM record_versions WHERE record_id=%s",
                                (old.record_id,)).fetchall()
        annotations = conn.execute("SELECT version_id,payload FROM annotations WHERE version_id=ANY(%s)",
                                   ([row["version_id"] for row in versions],)).fetchall()
    assert len(versions) == 2
    assert annotations == [{"version_id": expected(old)["version_id"], "payload": old.annotations[0]}]
    db.checked("adoption_retry_history", versions_retained=2, old_annotation_retained=True)


def test_real_third_version_and_withdrawal_reject(isolated_db):
    db = isolated_db
    old = rec("stale")
    proposed = rec("stale", body="Reviewed target. " * 20)
    third = rec("stale", body="A different committed update. " * 20)
    db.import_batch(ImportBatch(records=[old]))
    db.import_batch(ImportBatch(records=[third]))
    before = snapshot(db)
    with pytest.raises(ImportPreconditionFailed):
        db.import_batch(ImportBatch(records=[proposed]), expected_current={old.record_id: expected(old)})
    assert snapshot(db) == before
    with db.connect() as conn:
        conn.execute("UPDATE records SET active=false WHERE record_id=%s", (old.record_id,))
    before = snapshot(db)
    with pytest.raises(ImportPreconditionFailed):
        db.import_batch(ImportBatch(records=[third]), expected_current={old.record_id: expected(third)})
    assert snapshot(db) == before
    assert current(db, old.record_id)["active"] is False
    db.checked("third_version_and_withdrawal", zero_failed_import_mutations=True)


def test_real_whole_batch_checks_before_any_write(isolated_db):
    db = isolated_db
    a, b = rec("batch-a"), rec("batch-b")
    db.import_batch(ImportBatch(records=[a, b]))
    changed_b = rec("batch-b", body="Another source change. " * 20)
    db.import_batch(ImportBatch(records=[changed_b]))
    before = snapshot(db)
    with pytest.raises(ImportPreconditionFailed):
        db.import_batch(ImportBatch(records=[rec("batch-a", body="Reviewed A. " * 20),
                                            rec("batch-b", body="Reviewed B. " * 20)]),
                        expected_current={a.record_id: expected(a), b.record_id: expected(b)})
    assert snapshot(db) == before
    assert current(db, a.record_id)["body"] == a.body
    db.checked("whole_batch_prewrite_rejection", observed_tables=list(TABLES), unchanged=True)


class ConnectionHook:
    def __init__(self, connection, before_execute=None, before_commit=None):
        self.connection = connection
        self.before_execute = before_execute
        self.before_commit = before_commit

    def __enter__(self):
        self.connection.__enter__()
        return self

    def execute(self, statement, params=None):
        if self.before_execute:
            self.before_execute(self.connection, statement)
        return self.connection.execute(statement, params)

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None and self.before_commit:
            try:
                self.before_commit()
            except BaseException as hook_error:
                self.connection.__exit__(type(hook_error), hook_error, hook_error.__traceback__)
                raise
        return self.connection.__exit__(exc_type, exc, traceback)


class HookedDatabase(IsolatedDatabase):
    def __init__(self, original, *, before_execute=None, before_commit=None):
        super().__init__(original.url, original.schema, original.receipt)
        self.before_execute = before_execute
        self.before_commit = before_commit

    def connect(self, vector=False):
        return ConnectionHook(super().connect(vector=vector), self.before_execute, self.before_commit)


def wait_for_block(db, waiter, blocker, *, advisory_granted):
    """Observe a real server-side lock wait, with a deadline below lock_timeout."""
    deadline = time.monotonic() + 3.5
    with db.connect() as conn:
        while time.monotonic() < deadline:
            row = conn.execute("SELECT pg_blocking_pids(%s) AS blockers, "
                "EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND locktype='advisory' "
                "AND classid=0 AND objid=54901 AND granted=%s) AS advisory_state",
                (waiter, waiter, advisory_granted)).fetchone()
            if blocker in row["blockers"] and row["advisory_state"]:
                return {"blocker_observed": True, "waiter_advisory_granted": advisory_granted}
            time.sleep(.015)
    raise AssertionError("Expected server-side lock wait was not observed before the deadline")


def concurrent_guarded_updates(db, prefix, *, identical):
    old = rec(prefix)
    first = rec(prefix, body="First reviewed synthetic target. " * 20)
    second = first if identical else rec(prefix, body="Competing reviewed synthetic target. " * 20)
    db.import_batch(ImportBatch(records=[old]))
    guard = {prefix: expected(old)}
    first_ready, release_first, second_attempt = Event(), Event(), Event()
    pids = {}

    def first_statement(conn, statement):
        pids["first"] = conn.info.backend_pid

    def before_first_commit():
        first_ready.set()
        if not release_first.wait(8):
            raise TimeoutError("Bounded first-writer barrier expired")

    def second_statement(conn, statement):
        pids["second"] = conn.info.backend_pid
        if statement == "SELECT pg_advisory_xact_lock(54901)":
            second_attempt.set()

    first_db = HookedDatabase(db, before_execute=first_statement, before_commit=before_first_commit)
    second_db = HookedDatabase(db, before_execute=second_statement)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first_db.import_batch, ImportBatch(records=[first]), expected_current=guard)
        second_future = None
        try:
            assert first_ready.wait(4), "First writer did not reach its bounded commit barrier"
            second_future = pool.submit(second_db.import_batch, ImportBatch(records=[second]), expected_current=guard)
            assert second_attempt.wait(2), "Second writer did not attempt the advisory lock"
            observed = wait_for_block(db, pids["second"], pids["first"], advisory_granted=False)
        finally:
            release_first.set()
        first_report = first_future.result(timeout=7)
        assert first_report["new_versions"] == 1
        if identical:
            second_report = second_future.result(timeout=7)
            assert second_report["new_versions"] == 0 and second_report["already_applied"] == 1
        else:
            with pytest.raises(ImportPreconditionFailed):
                second_future.result(timeout=7)
    assert current(db, prefix)["body"] == first.body
    with db.connect() as conn:
        total = conn.execute("SELECT count(*) AS n FROM record_versions WHERE record_id=%s", (prefix,)).fetchone()["n"]
    assert total == 2
    return observed | {"versions_retained": total, "second_outcome": "exact_retry" if identical else "stale_rejected"}


def test_real_competing_different_updates_recheck_after_advisory_wait(isolated_db):
    observed = concurrent_guarded_updates(isolated_db, "competing", identical=False)
    isolated_db.checked("two_competing_updates", **observed)


def test_real_same_target_concurrent_retry_is_idempotent(isolated_db):
    observed = concurrent_guarded_updates(isolated_db, "identical", identical=True)
    isolated_db.checked("two_identical_updates", **observed)


def test_real_row_lock_wait_sees_external_withdrawal(isolated_db):
    db = isolated_db
    old = rec("row-lock")
    db.import_batch(ImportBatch(records=[old]))
    attempted = Event()
    pids = {}

    def before_execute(conn, statement):
        pids["guard"] = conn.info.backend_pid
        if isinstance(statement, str) and "FOR UPDATE OF r,v" in statement:
            attempted.set()

    guarded = HookedDatabase(db, before_execute=before_execute)
    with db.connect() as blocker:
        blocker.execute("SELECT record_id FROM records WHERE record_id=%s FOR UPDATE", (old.record_id,))
        blocker_pid = blocker.info.backend_pid
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(guarded.import_batch, ImportBatch(records=[rec("row-lock", body="Reviewed target. " * 20)]),
                                 expected_current={old.record_id: expected(old)})
            try:
                assert attempted.wait(2), "Guard did not reach the row-lock query"
                observed = wait_for_block(db, pids["guard"], blocker_pid, advisory_granted=True)
                blocker.execute("UPDATE records SET active=false WHERE record_id=%s", (old.record_id,))
                blocker.commit()
            finally:
                blocker.rollback()
            with pytest.raises(ImportPreconditionFailed):
                future.result(timeout=7)
    assert current(db, old.record_id)["active"] is False
    with db.connect() as conn:
        total = conn.execute("SELECT count(*) AS n FROM record_versions WHERE record_id=%s", (old.record_id,)).fetchone()["n"]
    assert total == 1
    db.checked("row_lock_withdrawal", **observed, replacement_versions_written=0)


def test_real_late_write_failure_rolls_back_all_changes(isolated_db):
    db = isolated_db
    a, b = rec("rollback-a"), rec("rollback-b")
    db.import_batch(ImportBatch(records=[a, b]))
    with db.connect() as conn:
        conn.execute("CREATE FUNCTION reject_synthetic_second_version() RETURNS trigger LANGUAGE plpgsql AS $$ "
                     "BEGIN IF NEW.record_id='rollback-b' THEN RAISE EXCEPTION 'synthetic_guarded_import_failure' "
                     "USING ERRCODE='23514'; END IF; RETURN NEW; END $$")
        conn.execute("CREATE TRIGGER synthetic_reject_second BEFORE INSERT ON record_versions "
                     "FOR EACH ROW EXECUTE FUNCTION reject_synthetic_second_version()")
    try:
        before = snapshot(db)
        with pytest.raises(psycopg.errors.CheckViolation):
            db.import_batch(ImportBatch(records=[rec(a.record_id, body="Reviewed A. " * 20),
                                                rec(b.record_id, body="Reviewed B. " * 20)]),
                            expected_current={a.record_id: expected(a), b.record_id: expected(b)})
        assert snapshot(db) == before
        assert current(db, a.record_id)["body"] == a.body
    finally:
        with db.connect() as conn:
            conn.execute("DROP TRIGGER synthetic_reject_second ON record_versions")
            conn.execute("DROP FUNCTION reject_synthetic_second_version()")
    db.checked("late_write_atomic_rollback", observed_tables=list(TABLES), unchanged=True)

