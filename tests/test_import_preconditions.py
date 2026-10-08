"""Offline transaction-contract checks; these do not prove PostgreSQL lock behavior."""

import copy
import json

import pytest

from observatory import indexing
from observatory.db import Database, ImportPreconditionFailed, digest
from observatory.models import ImportBatch, RecordInput


def record(record_id="a", **changes):
    fields = {
        "record_id": record_id, "dataset": "native", "url": f"https://example.org/{record_id}",
        "title": "Source advertisement", "body": "Original source text.", "retrievable": False,
    }
    return RecordInput(**(fields | changes))


def stored(record_input):
    payload = record_input.model_dump(mode="json")
    version = digest(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return {"version_id": version, "body": record_input.body,
            "body_hash": digest(record_input.body), "payload": payload}


def binding(record_input):
    row = stored(record_input)
    return {"version_id": row["version_id"], "body_sha256": row["body_hash"],
            "url": record_input.url, "dataset": record_input.dataset}


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class ImportTransaction:
    """Model transactional effects and capture ordering, without emulating locks."""

    def __init__(self, records):
        self.state = {"records": {}, "versions": {}, "annotations": [], "chunks": [],
                      "imports": [], "preparations": 0}
        self.events = []
        self.connection_count = 0
        self.after_guard = None
        self.fail_version_number = None
        self.version_attempts = 0
        for rec in records:
            self.set_current(rec)

    def set_current(self, rec, *, active=True):
        version = stored(rec)
        self.state["records"][rec.record_id] = {
            "record_id": rec.record_id, "dataset": rec.dataset,
            "current_version": version["version_id"], "active": active,
        }
        self.state["versions"][version["version_id"]] = version
        for ordinal, annotation in enumerate(version["payload"]["annotations"]):
            item = (version["version_id"], ordinal, copy.deepcopy(annotation))
            if item not in self.state["annotations"]:
                self.state["annotations"].append(item)

    def connect(self):
        self.connection_count += 1
        return self

    def __enter__(self):
        self.before = copy.deepcopy(self.state)
        self.events.append(("transaction", "begin"))
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type:
            self.state = self.before
            self.events.append(("transaction", "rollback"))
        else:
            self.events.append(("transaction", "commit"))
        return False

    def execute(self, sql, params=None):
        if sql == "SELECT pg_advisory_xact_lock(54901)":
            self.events.append(("advisory", sql))
            return Rows([])
        if "FOR UPDATE OF r,v" in sql:
            self.events.append(("guard", sql, params))
            rows = []
            for record_id in params[0]:
                source = self.state["records"].get(record_id)
                if not source:
                    continue
                version = self.state["versions"].get(source["current_version"])
                if version:
                    rows.append(copy.deepcopy(source | {
                        "body": version["body"], "body_hash": version["body_hash"],
                        "payload": version["payload"],
                    }))
            if self.after_guard:
                self.after_guard()
            return Rows(rows)
        if sql.startswith("SELECT current_version,dataset"):
            self.events.append(("current", params[0]))
            source = self.state["records"].get(params[0])
            return Rows([copy.deepcopy(source)] if source else [])
        self.events.append(("write", sql, params))
        if sql.startswith("UPDATE records SET active=true"):
            self.state["records"][params[0]]["active"] = True
        elif sql.startswith("INSERT INTO records("):
            record_id, dataset, version = params
            self.state["records"][record_id] = {
                "record_id": record_id, "dataset": dataset,
                "current_version": version, "active": True,
            }
        elif sql.startswith("INSERT INTO record_versions("):
            self.version_attempts += 1
            if self.version_attempts == self.fail_version_number:
                raise RuntimeError("synthetic_write_failure")
            version, record_id, body, body_hash, payload = params
            self.state["versions"].setdefault(version, {
                "version_id": version, "body": body, "body_hash": body_hash,
                "payload": copy.deepcopy(payload.obj),
            })
        elif sql.startswith("INSERT INTO annotations"):
            self.state["annotations"].append(copy.deepcopy((params[0], params[1], params[2].obj)))
        elif sql.startswith("INSERT INTO imports"):
            self.state["imports"].append(copy.deepcopy(params[0].obj))
        else:
            raise AssertionError(f"Unexpected SQL in isolated transaction mock: {sql}")
        return Rows([])


@pytest.fixture
def repository(monkeypatch):
    def make(records):
        tx = ImportTransaction(records)
        db = Database("not-a-real-database")
        monkeypatch.setattr(db, "connect", tx.connect)

        def profile(conn):
            conn.events.append(("profile",))
            return "synthetic-profile"

        def chunks(conn, profile_id, row):
            conn.events.append(("chunks", row["record_id"]))
            conn.state["chunks"].append(copy.deepcopy(row))

        def prepare(conn, profile_id):
            conn.events.append(("prepare",))
            conn.state["preparations"] += 1

        monkeypatch.setattr(indexing, "active_profile", profile)
        monkeypatch.setattr(indexing, "store_record_chunks", chunks)
        monkeypatch.setattr(indexing, "record_preparation", prepare)
        return db, tx
    return make


def assert_prewrite_rejection(tx, before):
    assert tx.state == before
    assert [event[0] for event in tx.events] == ["transaction", "advisory", "guard", "transaction"]
    assert tx.events[-1] == ("transaction", "rollback")


def test_guard_checks_whole_batch_before_profile_or_mutation(repository):
    old_a, old_b = record("a"), record("b")
    db, tx = repository([old_a, old_b])
    new_a = old_a.model_copy(update={"body": "Reviewed replacement.", "annotations": []}, deep=True)
    new_b = old_b.model_copy(update={"body": "Another reviewed replacement."}, deep=True)
    report = db.import_batch(ImportBatch(records=[new_b, new_a]),
                             expected_current={"b": binding(old_b), "a": binding(old_a)})
    assert report["new_versions"] == 2
    assert report["precondition_checked"] == 2
    assert report["already_applied"] == 0
    assert [event[0] for event in tx.events[:4]] == ["transaction", "advisory", "guard", "profile"]
    assert tx.events[2][2] == (["a", "b"],)
    assert "ORDER BY r.record_id FOR UPDATE OF r,v" in tx.events[2][1]
    assert tx.events[-1] == ("transaction", "commit")
    assert len(tx.state["versions"]) == 4
    assert tx.state["imports"] == [report]


@pytest.mark.parametrize("kind", ["missing", "missing_version", "inactive", "version", "body_hash", "url", "dataset"])
def test_any_stale_source_blocks_entire_batch_before_first_write(repository, kind):
    old_a, old_b = record("a"), record("b")
    db, tx = repository([old_a, old_b])
    expected = {"a": binding(old_a), "b": binding(old_b)}
    replacement = [record("a", body="Reviewed A."), record("b", body="Reviewed B.")]
    source = tx.state["records"]["b"]
    version = tx.state["versions"][source["current_version"]]
    if kind == "missing":
        del tx.state["records"]["b"]
    elif kind == "missing_version":
        del tx.state["versions"][source["current_version"]]
    elif kind == "inactive":
        source["active"] = False
    elif kind == "version":
        tx.set_current(record("b", body="Different update."))
    elif kind == "body_hash":
        expected["b"]["body_sha256"] = "f" * 64
    elif kind == "url":
        expected["b"]["url"] = "https://different.example.org/b"
    elif kind == "dataset":
        source["dataset"] = "social"
    before = copy.deepcopy(tx.state)
    with pytest.raises(ImportPreconditionFailed, match="missing or changed"):
        db.import_batch(ImportBatch(records=replacement), expected_current=expected)
    assert_prewrite_rejection(tx, before)
    assert version


@pytest.mark.parametrize("changed", ["body", "payload"])
def test_in_place_source_changes_fail_integrity_before_write(repository, changed):
    old = record()
    db, tx = repository([old])
    version = tx.state["versions"][stored(old)["version_id"]]
    if changed == "body":
        version["body"] = "Edited without changing stored hash."
    else:
        version["payload"]["sponsor"] = "Changed without a version."
    before = copy.deepcopy(tx.state)
    with pytest.raises(ImportPreconditionFailed):
        db.import_batch(ImportBatch(records=[record(body="Reviewed.")]), expected_current={"a": binding(old)})
    assert_prewrite_rejection(tx, before)


def test_exact_retry_uses_unchanged_and_preserves_old_history(repository):
    old = record(annotations=[{"labels": ["historical"]}])
    replacement = record(body="Reviewed text.", annotations=[])
    db, tx = repository([old])
    expected = {"a": binding(old)}
    first = db.import_batch(ImportBatch(records=[replacement]), expected_current=expected)
    assert first["new_versions"] == 1
    tx.events.clear()
    second = db.import_batch(ImportBatch(records=[replacement]), expected_current=expected)
    assert second["new_versions"] == 0
    assert second["unchanged"] == 1
    assert second["already_applied"] == 1
    assert len(tx.state["versions"]) == 2
    assert stored(old)["version_id"] in tx.state["versions"]
    assert tx.state["annotations"] == [(stored(old)["version_id"], 0, {"labels": ["historical"]})]
    assert not any(event[0] == "write" and "INSERT INTO record_versions" in event[1] for event in tx.events)


def test_mixed_exact_retry_and_original_sources_share_one_atomic_guard(repository):
    old_a, old_b = record("a"), record("b")
    new_a, new_b = record("a", body="Reviewed A."), record("b", body="Reviewed B.")
    db, tx = repository([old_a, old_b])
    tx.set_current(new_a)
    report = db.import_batch(ImportBatch(records=[new_a, new_b]),
                             expected_current={"a": binding(old_a), "b": binding(old_b)})
    assert report["precondition_checked"] == 2
    assert report["already_applied"] == 1
    assert report["new_versions"] == 1
    assert report["unchanged"] == 1


@pytest.mark.parametrize("changed", ["metadata", "annotation", "retrieval_range", "inactive"])
def test_retry_cannot_accept_same_body_with_different_payload_or_revive(repository, changed):
    old = record()
    replacement = record(body="Reviewed text.")
    db, tx = repository([old])
    expected = {"a": binding(old)}
    db.import_batch(ImportBatch(records=[replacement]), expected_current=expected)
    if changed == "metadata":
        tx.set_current(replacement.model_copy(update={"sponsor": "Different sponsor"}, deep=True))
    elif changed == "annotation":
        tx.set_current(replacement.model_copy(update={"annotations": [{"labels": ["other"]}]}, deep=True))
    elif changed == "retrieval_range":
        tx.set_current(replacement.model_copy(update={"retrieval_end": 5}, deep=True))
    else:
        tx.state["records"]["a"]["active"] = False
    tx.events.clear()
    before = copy.deepcopy(tx.state)
    with pytest.raises(ImportPreconditionFailed):
        db.import_batch(ImportBatch(records=[replacement]), expected_current=expected)
    assert_prewrite_rejection(tx, before)


def test_guarded_input_and_expectations_are_frozen_before_connection(repository):
    old = record()
    replacement = record(body="Reviewed text.", raw={"nested": {"approved": True}})
    batch = ImportBatch(records=[replacement])
    expected = {"a": binding(old)}
    db, tx = repository([old])

    def mutate_callers_after_guard_read():
        replacement.body = "Changed while waiting."
        replacement.annotations.append({"labels": ["not reviewed"]})
        replacement.raw["nested"]["approved"] = False
        expected["a"]["url"] = "https://different.example.org/"
        batch.records.clear()

    tx.after_guard = mutate_callers_after_guard_read
    report = db.import_batch(batch, expected_current=expected)
    assert report["new_versions"] == 1
    current_version = tx.state["records"]["a"]["current_version"]
    payload = tx.state["versions"][current_version]["payload"]
    assert payload["body"] == "Reviewed text."
    assert payload["raw"]["nested"]["approved"] is True
    assert payload["annotations"] == []


def test_later_write_failure_rolls_back_whole_guarded_batch(repository):
    old_a, old_b = record("a"), record("b")
    db, tx = repository([old_a, old_b])
    before = copy.deepcopy(tx.state)
    tx.fail_version_number = 2
    with pytest.raises(RuntimeError, match="synthetic_write_failure"):
        db.import_batch(ImportBatch(records=[record("a", body="New A."), record("b", body="New B.")]),
                        expected_current={"a": binding(old_a), "b": binding(old_b)})
    assert tx.state == before
    assert tx.events[-1] == ("transaction", "rollback")
    assert any(event[0] == "write" for event in tx.events)


@pytest.mark.parametrize("invalid", [
    "not_dict", "missing_id", "extra_id", "duplicate_id", "missing_field", "extra_field",
    "binding_not_dict", "bad_version", "bad_body_hash", "upper_hash", "bad_url", "bad_dataset",
])
def test_malformed_bindings_reject_without_opening_connection(repository, invalid):
    old = record()
    db, tx = repository([old])
    batch = ImportBatch(records=[record(body="Reviewed text.")])
    expected = {"a": binding(old)}
    if invalid == "not_dict":
        expected = []
    elif invalid == "missing_id":
        expected = {}
    elif invalid == "extra_id":
        expected["b"] = binding(record("b"))
    elif invalid == "duplicate_id":
        batch.records.append(batch.records[0])
    elif invalid == "missing_field":
        del expected["a"]["body_sha256"]
    elif invalid == "extra_field":
        expected["a"]["approved"] = True
    elif invalid == "binding_not_dict":
        expected["a"] = []
    elif invalid == "bad_version":
        expected["a"]["version_id"] = "old"
    elif invalid == "bad_body_hash":
        expected["a"]["body_sha256"] = "hash"
    elif invalid == "upper_hash":
        expected["a"]["version_id"] = "A" * 64
    elif invalid == "bad_url":
        expected["a"]["url"] = None
    elif invalid == "bad_dataset":
        expected["a"]["dataset"] = "social"
    with pytest.raises(ValueError):
        db.import_batch(batch, expected_current=expected)
    assert tx.connection_count == 0
    assert tx.events == []


@pytest.mark.parametrize("snapshot", ["native", "social", "invalid"])
def test_guarded_import_rejects_snapshot_mode_before_connection(repository, snapshot):
    old = record()
    db, tx = repository([old])
    with pytest.raises(ValueError, match="cannot replace a dataset snapshot"):
        db.import_batch(ImportBatch(records=[record(body="New.")]), snapshot_dataset=snapshot,
                        expected_current={"a": binding(old)})
    assert tx.connection_count == 0


def test_default_import_has_no_new_guard_or_report_fields_and_can_reactivate(repository):
    old = record()
    db, tx = repository([old])
    tx.state["records"]["a"]["active"] = False
    report = db.import_batch(ImportBatch(records=[old]))
    assert report["unchanged"] == 1
    assert tx.state["records"]["a"]["active"] is True
    assert "precondition_checked" not in report and "already_applied" not in report
    assert not any(event[0] == "guard" for event in tx.events)

