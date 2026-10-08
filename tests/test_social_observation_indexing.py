"""Observation indexing and publication guards without database or provider calls."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from observatory import indexing, social_source_retrieval
from observatory.migrations import discover_migrations


def source_row(dataset="native", body="One source sentence. Another source sentence."):
    payload = {"dataset": dataset, "body": body, "retrievable": True}
    return {"record_id": dataset + ":one", "version_id": "a" * 64,
            "body": body, "payload": payload}


def observation(source_id, body):
    return {"source_observation_id": source_id,
            "source_version_id": indexing._digest(source_id + ":" + body),
            "source_body_hash": indexing._digest(body), "body": body,
            "quality_codes": [], "source_conflicts": [], "observation_count": 2}


def mock_observations(monkeypatch, *items):
    monkeypatch.setattr(social_source_retrieval, "validated_observations", lambda payload: list(items))


def test_existing_profile_definitions_retain_frozen_structure():
    for profile, strategy in ((indexing.LEGACY_PROFILE, "legacy"), (indexing.SENTENCE_PROFILE, "sentence")):
        spec = indexing.definition(profile)
        assert spec["strategy"] == strategy
        assert not any(key.startswith("social_") for key in spec)
    spec = indexing.definition(indexing.SOCIAL_OBSERVATION_PROFILE)
    assert spec["strategy"] == "sentence"
    assert spec["segmentation"] == indexing.SEGMENTATION_PROFILE
    assert spec["social_observation_strategy"] == "sentence_coverage"
    assert spec["social_observation_policy"] == social_source_retrieval.SCHEMA_VERSION
    assert spec["social_observation_segmentation"] == indexing.COVERAGE_SEGMENTATION_PROFILE


@pytest.mark.parametrize("body", [
    "A native sentence. More native evidence.",
    "中文🙂 café e\u0301 details " * 230,
])
def test_new_profile_keeps_native_chunks_and_ids_byte_identical(body):
    row = source_row(body=body)
    baseline = indexing.expected_chunks(row, indexing.definition(indexing.SENTENCE_PROFILE))
    current = indexing.expected_chunks(row, indexing.definition(indexing.SOCIAL_OBSERVATION_PROFILE))
    assert current == baseline
    assert indexing._manifest(current) == indexing._manifest(baseline)
    assert not any(field in chunk for chunk in current for field in indexing.OBSERVATION_FIELDS)


def test_each_observation_keeps_separate_source_positions_even_for_same_text(monkeypatch):
    body = "Shared source. Exact source observation."
    left, right = observation("junkipedia:101", body), observation("junkipedia:102", body)
    mock_observations(monkeypatch, left, right)
    row = source_row("social", "Canonical display differs and must not be sliced.")
    chunks = indexing.expected_chunks(row, indexing.definition(indexing.SOCIAL_OBSERVATION_PROFILE))
    assert len(chunks) == 2 and len({chunk["chunk_id"] for chunk in chunks}) == 2
    assert len({chunk["text_hash"] for chunk in chunks}) == 1
    for chunk, supplied in zip(chunks, (left, right), strict=True):
        assert chunk["text"] == supplied["body"][chunk["start_char"]:chunk["end_char"]]
        assert chunk["source_observation_id"] == supplied["source_observation_id"]
        assert chunk["source_version_id"] == supplied["source_version_id"]
        assert chunk["source_body_hash"] == supplied["source_body_hash"]
        assert chunk["chunk_id"] == indexing._digest(
            f"{row['version_id']}:{supplied['source_version_id']}:{chunk['start_char']}:{chunk['end_char']}"
        )


@pytest.mark.parametrize("body", [
    "♨️Where are new district heating networks being explored?",
    "Source without punctuation " * 80,
    "  First region.\r\n\r\n中文🙂 café e\u0301 <|endoftext|> " * 70,
])
def test_coverage_preserves_source_characters_and_token_limit(monkeypatch, body):
    supplied = observation("junkipedia:101", body)
    mock_observations(monkeypatch, supplied)
    chunks = indexing.expected_chunks(source_row("social"), indexing.definition(indexing.SOCIAL_OBSERVATION_PROFILE))
    covered = set()
    for chunk in chunks:
        start, end = chunk["start_char"], chunk["end_char"]
        assert chunk["text"] == body[start:end] and chunk["token_count"] <= 600
        covered.update(range(start, end))
    assert all(i in covered for i, char in enumerate(body) if not char.isspace())


def test_body_hash_drift_stops_observation_indexing(monkeypatch):
    supplied = observation("junkipedia:101", "Literal original source.")
    supplied["body"] += "changed"
    mock_observations(monkeypatch, supplied)
    with pytest.raises(ValueError, match="body hash changed"):
        indexing.expected_chunks(source_row("social"), indexing.definition(indexing.SOCIAL_OBSERVATION_PROFILE))


def test_disabled_source_is_not_indexed_or_validated(monkeypatch):
    monkeypatch.setattr(social_source_retrieval, "validated_observations",
                        lambda payload: pytest.fail("disabled source reached the helper"))
    row = source_row("social")
    row["payload"]["retrievable"] = False
    assert indexing.expected_chunks(row, indexing.definition(indexing.SOCIAL_OBSERVATION_PROFILE)) == []


def test_legacy_social_strategy_does_not_call_observation_helper(monkeypatch):
    monkeypatch.setattr(social_source_retrieval, "validated_observations",
                        lambda payload: pytest.fail("legacy profile used observation policy"))
    assert indexing.expected_chunks(source_row("social"), indexing.definition(indexing.SENTENCE_PROFILE))


def test_null_provenance_does_not_change_legacy_manifest_bytes():
    member = indexing.expected_chunks(source_row(), indexing.definition(indexing.SENTENCE_PROFILE))[0]
    stored = dict(member, **dict.fromkeys(indexing.OBSERVATION_FIELDS))
    assert indexing._source_fields(stored) == member
    assert indexing._manifest([indexing._source_fields(stored)]) == indexing._manifest([member])
    for field in indexing.OBSERVATION_FIELDS:
        bad = dict(stored, **{field: "present"})
        with pytest.raises(ValueError, match="Incomplete"):
            indexing._source_fields(bad)


class FakeConnection:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])


class StoreConnection(FakeConnection):
    def __init__(self, chunk, drift=None):
        super().__init__()
        self.stored = dict(chunk, **{field: chunk.get(field) for field in indexing.OBSERVATION_FIELDS})
        if drift:
            self.stored[drift] = "changed"

    def execute(self, sql, params=()):
        super().execute(sql, params)
        return SimpleNamespace(fetchone=lambda: self.stored, fetchall=lambda: [self.stored])


@pytest.mark.parametrize("dataset", ["native", "social"])
def test_store_and_reader_preserve_exact_optional_provenance(monkeypatch, dataset):
    mock_observations(monkeypatch, observation("junkipedia:101", "Exact supplied source."))
    row = source_row(dataset)
    spec = indexing.definition(indexing.SOCIAL_OBSERVATION_PROFILE)
    chunk = indexing.expected_chunks(row, spec)[0]
    conn = StoreConnection(chunk)
    indexing.store_record_chunks(conn, indexing.SOCIAL_OBSERVATION_PROFILE, row, spec)
    insertion = next(params for sql, params in conn.calls if sql.startswith("INSERT INTO chunks"))
    assert len(insertion) == 12
    assert insertion[-3:] == tuple(chunk.get(field) for field in indexing.OBSERVATION_FIELDS)
    assert indexing._members(conn, indexing.SOCIAL_OBSERVATION_PROFILE) == [chunk]
    assert sum(sql.startswith("INSERT INTO chunk_profile_membership") for sql, _ in conn.calls) == 1


@pytest.mark.parametrize("drift", ["text", "source_body_hash"])
def test_existing_observation_chunk_drift_blocks_membership_write(monkeypatch, drift):
    mock_observations(monkeypatch, observation("junkipedia:101", "Exact supplied source."))
    row, spec = source_row("social"), indexing.definition(indexing.SOCIAL_OBSERVATION_PROFILE)
    conn = StoreConnection(indexing.expected_chunks(row, spec)[0], drift)
    with pytest.raises(ValueError, match="immutable chunk differs"):
        indexing.store_record_chunks(conn, indexing.SOCIAL_OBSERVATION_PROFILE, row, spec)
    assert not any(sql.startswith("INSERT INTO chunk_profile_membership") for sql, _ in conn.calls)


def test_manifest_binds_observation_provenance(monkeypatch):
    mock_observations(monkeypatch, observation("junkipedia:101", "Exact supplied source."))
    members = indexing.expected_chunks(source_row("social"), indexing.definition(indexing.SOCIAL_OBSERVATION_PROFILE))
    before = indexing._manifest(members)
    changed = deepcopy(members)
    changed[0]["source_version_id"] = "b" * 64
    assert indexing._manifest(changed) != before


@pytest.fixture
def publication(monkeypatch):
    native, social = source_row(), source_row("social")
    mock_observations(monkeypatch, observation("junkipedia:101", "Exact supplied post."))
    rows = [native, social]
    expected = sorted([chunk for row in rows for chunk in indexing.expected_chunks(
        row, indexing.definition(indexing.SOCIAL_OBSERVATION_PROFILE),
    )], key=lambda chunk: chunk["chunk_id"])
    previous = indexing.expected_chunks(native, indexing.definition(indexing.SENTENCE_PROFILE))
    vectors = [{"text_hash": previous[0]["text_hash"], "model": "existing-model", "embedding": "[1,0]"}]
    state = {"active": indexing.SENTENCE_PROFILE, "source": "source-v1", "prepared": True,
             "members": expected, "previous": previous, "vectors": vectors, "previous_vectors": deepcopy(vectors)}
    conn = FakeConnection()
    manager = indexing.IndexManager(SimpleNamespace(connect=lambda: conn))
    monkeypatch.setattr(indexing, "_register", lambda conn, profile: indexing.definition(profile))
    monkeypatch.setattr(indexing, "active_profile", lambda conn: state["active"])
    monkeypatch.setattr(indexing, "source_version", lambda conn: state["source"])
    monkeypatch.setattr(indexing, "_sources", lambda conn: rows)
    monkeypatch.setattr(indexing, "_members", lambda conn, profile: state[
        "members" if profile == indexing.SOCIAL_OBSERVATION_PROFILE else "previous"
    ])
    monkeypatch.setattr(indexing, "_native_vectors", lambda conn, profile: state[
        "vectors" if profile == indexing.SOCIAL_OBSERVATION_PROFILE else "previous_vectors"
    ])
    monkeypatch.setattr(manager, "_status", lambda conn, profile: {
        "prepared_snapshot_matches": state["prepared"], "missing_embeddings": 100,
    })
    monkeypatch.setattr(indexing, "profile_snapshot", lambda conn: {
        "active_profile": indexing.SOCIAL_OBSERVATION_PROFILE,
        "source_data_version": state["source"], "index_version": "new-index",
    })
    return manager, conn, state


def activate(manager):
    return manager.activate_social_observation_profile(
        expected_source_data_version="source-v1", expected_previous_profile=indexing.SENTENCE_PROFILE,
    )


def test_explicit_unpaid_activation_preserves_existing_incomplete_native_vectors(publication):
    manager, conn, state = publication
    result = activate(manager)
    assert result["active_profile"] == indexing.SOCIAL_OBSERVATION_PROFILE
    assert state["vectors"] == state["previous_vectors"]
    assert sum(sql.startswith("UPDATE retrieval_state") for sql, _ in conn.calls) == 1
    assert sum(sql.startswith("INSERT INTO retrieval_publications") for sql, _ in conn.calls) == 1
    assert not any("INSERT INTO embeddings" in sql for sql, _ in conn.calls)


@pytest.mark.parametrize("failure,match", [
    ("active", "Active profile changed"), ("source", "Source snapshot changed"),
    ("prepared", "missing or stale"), ("members", "membership is incomplete"),
    ("previous", "Native chunks differ"), ("previous_vectors", "Native vector availability differs"),
])
def test_failed_publication_guard_never_changes_active_state(publication, failure, match):
    manager, conn, state = publication
    if failure == "active":
        state[failure] = indexing.LEGACY_PROFILE
    elif failure == "source":
        state[failure] = "changed"
    elif failure == "prepared":
        state[failure] = False
    else:
        state[failure] = []
    with pytest.raises(ValueError, match=match):
        activate(manager)
    assert not any(sql.startswith("UPDATE retrieval_state") for sql, _ in conn.calls)


def test_normal_activation_still_demands_all_embeddings(publication):
    manager, conn, _ = publication
    with pytest.raises(ValueError, match="embeddings are incomplete"):
        manager.activate(indexing.SOCIAL_OBSERVATION_PROFILE, expected_source_data_version="source-v1")
    assert not any(sql.startswith("UPDATE retrieval_state") for sql, _ in conn.calls)


def test_mid_publication_source_change_is_rejected(publication, monkeypatch):
    manager, conn, _ = publication
    versions = iter(["source-v1", "source-v2"])
    monkeypatch.setattr(indexing, "source_version", lambda conn: next(versions))
    with pytest.raises(ValueError, match="changed during publication"):
        activate(manager)
    assert not any(sql.startswith("UPDATE retrieval_state") for sql, _ in conn.calls)


def test_unpaid_activation_requires_explicit_previous_baseline_before_connection():
    manager = indexing.IndexManager(SimpleNamespace(connect=lambda: pytest.fail("Invalid transition connected")))
    with pytest.raises(ValueError, match="must be named"):
        manager.activate_social_observation_profile(
            expected_source_data_version="source-v1", expected_previous_profile=indexing.SOCIAL_OBSERVATION_PROFILE,
        )


def test_migration_preserves_baseline_and_adds_all_or_none_provenance():
    migration = next(m for m in discover_migrations() if m.version == 5)
    assert migration.name == "0005_social_observation_chunks.sql"
    assert "ALTER TABLE chunks" in migration.sql
    for field in indexing.OBSERVATION_FIELDS:
        assert f"ADD COLUMN {field} text" in migration.sql
        assert f"{field} IS NULL" in migration.sql and f"{field} IS NOT NULL" in migration.sql
    assert "CREATE INDEX chunks_source_observation" in migration.sql
    assert not any(word in migration.sql.upper() for word in ("DELETE ", "UPDATE ", "DROP "))
