"""Independent source-binding regressions; no database or provider actions."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from observatory import indexing, migrations
from observatory.db import Database
from observatory.models import Evidence, RecordInput
from observatory.quality import body_hash
from observatory.social_admission import _admit_group
from observatory.social_source_binding import bind_search_row, evidence_source
from observatory.social_source_retrieval import (
    source_version_id,
    validated_observations,
)


@pytest.fixture
def search_row():
    body = "Saved literal source. Independent evidence check."
    original = RecordInput(
        record_id="junkipedia:101", dataset="social", url="https://twitter.com/example/status/9001",
        platform="Twitter", account="Example", sponsor="Example company", body=body,
        countable=False, retrievable=False,
        raw={"body_sha256": body_hash(body), "source_row": {"id": "101", "post_text": body}},
    )
    record, _ = _admit_group(
        [(1, original)], original.url, "b" * 64,
        {"data_sha256": "c" * 64, "selections": {"D02": {"id": "D02-reviewed"}, "D03": {"id": "D03-reviewed"}}},
    )
    payload = record.model_dump(mode="json")
    observation = validated_observations(payload)[0]
    canonical_version = source_version_id(payload)
    row = {"evidence_id": body_hash(f"{canonical_version}:{observation['source_version_id']}:0:{len(body)}"),
           "record_id": payload["record_id"], "version_id": canonical_version, "dataset": "social",
           "title": payload["title"], "url": payload["url"], "text": body, "start": 0, "end": len(body),
           "source_observation_id": observation["source_observation_id"],
           "source_version_id": observation["source_version_id"],
           "source_body_hash": observation["source_body_hash"], "observation_payload": payload}
    return row


def authoritative_row(search_row, evidence):
    payload = search_row["observation_payload"]
    return {"record_id": payload["record_id"], "version_id": source_version_id(payload), "dataset": "social",
            "retrievable": True, "social_source_observations": validated_observations(payload)}


def test_exact_validated_source_can_bind_and_payload_is_not_exposed(search_row):
    item = bind_search_row(search_row)
    assert evidence_source(authoritative_row(search_row, item), item)["body"] == item.text
    assert "observation_payload" not in item.model_dump()
    assert item.source_observation_count == 1


@pytest.mark.parametrize("field,value", [
    ("record_id", "social-post:other"), ("dataset", "native"),
    ("version_id", "e" * 64), ("url", "https://twitter.com/other/status/9002"),
])
def test_enclosing_identity_cannot_borrow_another_valid_observation(search_row, field, value):
    search_row[field] = value
    with pytest.raises(ValueError):
        bind_search_row(search_row)


def test_in_place_canonical_metadata_drift_cannot_retain_old_version(search_row):
    search_row["observation_payload"]["title"] += " metadata changed in place"
    with pytest.raises(ValueError):
        bind_search_row(search_row)


@pytest.mark.parametrize("field", ["source_observation_id", "source_version_id", "source_body_hash"])
def test_observation_identity_and_hash_are_checked_independently(search_row, field):
    search_row[field] = "junkipedia:102" if field == "source_observation_id" else "f" * 64
    with pytest.raises(ValueError):
        bind_search_row(search_row)


@pytest.mark.parametrize("failure", ["duplicate", "body_drift", "quality_drift", "version_drift"])
def test_authoritative_current_source_revalidation_rejects_drift(search_row, failure):
    item = bind_search_row(search_row)
    current = authoritative_row(search_row, item)
    if failure == "duplicate":
        current["social_source_observations"].append(deepcopy(current["social_source_observations"][0]))
    elif failure == "body_drift":
        current["social_source_observations"][0]["body"] += " changed"
    elif failure == "quality_drift":
        current["social_source_observations"][0]["quality_codes"] += ["unreviewed_quality"]
    else:
        current["version_id"] = "e" * 64
    with pytest.raises(ValueError):
        evidence_source(current, item)


def test_model_copy_cannot_bypass_strict_observation_offset_types(search_row):
    item = bind_search_row(search_row).model_copy(update={"start": False})
    with pytest.raises(ValueError):
        evidence_source(authoritative_row(search_row, item), item)


def test_native_null_fields_keep_legacy_evidence_defaults_and_exact_body():
    body = "Literal native source."
    row = {"evidence_id": "native-evidence", "record_id": "native:one", "version_id": "n-version",
           "dataset": "native", "title": "Native", "text": body, "start": 0, "end": len(body),
           "source_observation_id": None, "source_version_id": None, "source_body_hash": None,
           "observation_payload": None}
    item = bind_search_row(row)
    baseline = Evidence(**{key: value for key, value in row.items()
                           if key not in {"observation_payload", "source_observation_id", "source_version_id", "source_body_hash"}})
    assert item == baseline and item.source_observation_count == 1
    current = {"record_id": item.record_id, "version_id": item.version_id, "dataset": "native",
               "retrievable": True, "body": body, "body_hash": body_hash(body)}
    assert evidence_source(current, item) == {"body": body, "body_hash": body_hash(body)}


def test_initialize_applies_all_migrations_before_bootstrap_can_read_chunk_columns(monkeypatch):
    events = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            events.append("lock")
            return SimpleNamespace()

    monkeypatch.setattr(migrations, "run_migrations", lambda conn: events.append("migration0005"))

    def bootstrap(conn):
        assert events == ["lock", "migration0005"]
        events.append("bootstrap")

    monkeypatch.setattr(indexing, "bootstrap", bootstrap)
    monkeypatch.setattr(Database, "connect", lambda self: Connection())
    Database("unused").initialize()
    assert events == ["lock", "migration0005", "bootstrap"]


@pytest.mark.parametrize("failure", [None, "record_id", "dataset", "missing_version", "metadata", "body"])
def test_index_source_enclosing_identity_and_immutable_payload_are_checked(failure):
    payload = {"record_id": "native:one", "dataset": "native", "body": "Literal source."}
    row = {"record_id": payload["record_id"], "dataset": payload["dataset"],
           "version_id": source_version_id(payload), "body": payload["body"], "payload": payload}
    if failure in ("record_id", "dataset"):
        row[failure] = "different"
    elif failure == "missing_version":
        row.update(payload=None, body=None, version_id=None)
    elif failure == "metadata":
        row["payload"]["title"] = "Metadata changed in place"
    elif failure == "body":
        row["body"] = "Changed source outside payload"
    calls = []

    def execute(sql, params=()):
        calls.append(sql)
        return SimpleNamespace(fetchall=lambda: [row])

    conn = SimpleNamespace(execute=execute)
    if failure is None:
        assert indexing._sources(conn) == [row]
    else:
        with pytest.raises(ValueError):
            indexing._sources(conn)
    assert "r.dataset" in calls[0] and "v.record_id=r.record_id" in calls[0]
    assert "LEFT JOIN" in calls[0]  # Dangling active records are checked rather than silently omitted.
