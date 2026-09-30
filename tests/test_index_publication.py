"""Publication tests use only disposable obs_test and deterministic local vectors."""

import os

import numpy as np
import pytest
from psycopg.conninfo import conninfo_to_dict

from observatory.config import Settings
from observatory.db import Database
from observatory.indexing import (
    LEGACY_PROFILE,
    SENTENCE_PROFILE,
    IndexManager,
    definition,
    record_preparation,
)
from observatory.models import Filters, ImportBatch, RecordInput

pytestmark = pytest.mark.integration


@pytest.fixture
def db():
    Settings.from_env()
    url = os.environ.get("OBS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("OBS_TEST_DATABASE_URL is not configured")
    assert conninfo_to_dict(url).get("dbname", "").startswith("obs_test")
    result = Database(url)
    result.initialize()

    def reset():
        with result.connect() as conn:
            assert conn.execute("SELECT current_database() AS n").fetchone()["n"].startswith("obs_test")
            conn.execute(
                "TRUNCATE records,record_versions,chunks,annotations,imports,usage_ledger,"
                "embeddings,answer_runs,retrieval_preparations,retrieval_publications CASCADE"
            )
            conn.execute(
                "UPDATE retrieval_state SET active_profile=%s WHERE singleton", (LEGACY_PROFILE,),
            )

    reset()
    yield result
    reset()  # Publication tests never leak a sentence-active state into other modules.


def record(rid="n1"):
    return RecordInput(
        record_id=rid, dataset="native", title=f"Carbon projects for {rid}",
        body=" ".join(
            f"Project {i} proposes carbon capture facilities to reduce emissions while "
            "independent researchers question the cost and effectiveness of these promises."
            for i in range(100)
        ), retrievable=True,
    )


def embed_profile(db, profile):
    vector = np.array([1.0] + [0.0] * 1535)
    pending = db.pending_embeddings(profile_id=profile)
    with db.connect(vector=True) as conn:
        for row in pending:
            conn.execute(
                "INSERT INTO embeddings(text_hash,model,embedding) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (row["text_hash"], "text-embedding-3-small", vector),
            )


def member_ids(db, profile, rid=None):
    with db.connect() as conn:
        return {row["chunk_id"] for row in conn.execute(
            "SELECT m.chunk_id FROM chunk_profile_membership m JOIN chunks c USING(chunk_id) "
            "JOIN records r ON r.current_version=c.version_id "
            "WHERE r.active AND m.profile_id=%s AND (%s::text IS NULL OR r.record_id=%s)",
            (profile, rid, rid),
        ).fetchall()}


def test_prepare_publish_and_rollback_preserve_historical_evidence(db):
    db.import_batch(ImportBatch(records=[record()]))
    manager = IndexManager(db)
    before = db.health()
    old = db.search("carbon capture", Filters(), chunks_per_record=20)
    baseline_ids = {e.evidence_id for e in old}
    prepared = manager.prepare(SENTENCE_PROFILE)
    assert db.health() == before  # Prepared candidates are invisible to active health/search.
    assert prepared["missing_embeddings"] > 0
    assert member_ids(db, SENTENCE_PROFILE) != member_ids(db, LEGACY_PROFILE)
    with pytest.raises(ValueError, match="embeddings are incomplete"):
        manager.activate(SENTENCE_PROFILE, expected_source_data_version=prepared["source_data_version"])
    assert db.health() == before
    embed_profile(db, SENTENCE_PROFILE)
    published = manager.activate(SENTENCE_PROFILE, expected_source_data_version=prepared["source_data_version"])
    assert published["source_data_version"] == before["source_data_version"]
    assert published["data_version"] != before["data_version"]
    assert published["index_version"] != before["index_version"]
    assert published["active_profile"] == SENTENCE_PROFILE
    hits = db.search("carbon capture", Filters(), chunks_per_record=20)
    assert {e.evidence_id for e in hits} == member_ids(db, SENTENCE_PROFILE)
    vector_hits = db.search("carbon capture", Filters(), vector=[1.0] + [0.0] * 1535, chunks_per_record=20)
    assert {e.evidence_id for e in vector_hits} == member_ids(db, SENTENCE_PROFILE)
    assert all(db.validate_evidence(e) for e in old + hits)
    embed_profile(db, LEGACY_PROFILE)
    manager.activate(LEGACY_PROFILE, expected_source_data_version=prepared["source_data_version"])
    assert db.health() == before
    assert {e.evidence_id for e in db.search("carbon capture", Filters(), chunks_per_record=20)} == baseline_ids


def test_import_after_publication_creates_current_profile_and_requires_reprepare_for_rollback(db):
    a, b = record(), record("n2")
    db.import_batch(ImportBatch(records=[a]))
    manager = IndexManager(db)
    prepared = manager.prepare(SENTENCE_PROFILE)
    embed_profile(db, SENTENCE_PROFILE)
    manager.activate(SENTENCE_PROFILE, expected_source_data_version=prepared["source_data_version"])
    result = db.import_batch(ImportBatch(records=[a, b]))
    assert result["index_profile"] == SENTENCE_PROFILE
    assert member_ids(db, SENTENCE_PROFILE, "n2")
    assert not member_ids(db, LEGACY_PROFILE, "n2")
    assert db.search("carbon", Filters(record_ids=["n2"]))
    current = db.health()
    with pytest.raises(ValueError, match="stale"):
        manager.activate(LEGACY_PROFILE, expected_source_data_version=current["source_data_version"])
    assert db.health() == current
    prepared = manager.prepare(LEGACY_PROFILE)
    embed_profile(db, LEGACY_PROFILE)
    manager.activate(LEGACY_PROFILE, expected_source_data_version=prepared["source_data_version"])
    assert db.search("carbon", Filters(record_ids=["n2"]))


def test_reactivation_of_unchanged_old_source_populates_active_profile(db):
    a, b = record(), record("n2")
    db.import_batch(ImportBatch(records=[a, b]), snapshot_dataset="native")
    db.import_batch(ImportBatch(records=[a]), snapshot_dataset="native")
    manager = IndexManager(db)
    prepared = manager.prepare(SENTENCE_PROFILE)
    embed_profile(db, SENTENCE_PROFILE)
    manager.activate(SENTENCE_PROFILE, expected_source_data_version=prepared["source_data_version"])
    assert not member_ids(db, SENTENCE_PROFILE, "n2")
    result = db.import_batch(ImportBatch(records=[a, b]), snapshot_dataset="native")
    assert result["unchanged"] == 2
    assert member_ids(db, SENTENCE_PROFILE, "n2")
    assert db.search("carbon", Filters(record_ids=["n2"]))


def test_prepare_stale_source_and_wrong_expected_snapshot_never_publish(db):
    db.import_batch(ImportBatch(records=[record()]))
    manager = IndexManager(db)
    prepared = manager.prepare(SENTENCE_PROFILE)
    embed_profile(db, SENTENCE_PROFILE)
    db.import_batch(ImportBatch(records=[record("n2")]))
    before = db.health()
    with pytest.raises(ValueError, match="snapshot changed"):
        manager.activate(SENTENCE_PROFILE, expected_source_data_version=prepared["source_data_version"])
    with pytest.raises(ValueError, match="stale"):
        manager.activate(SENTENCE_PROFILE, expected_source_data_version=before["source_data_version"])
    assert db.health() == before


def test_membership_tampering_is_rejected_even_with_refreshed_manifest(db):
    db.import_batch(ImportBatch(records=[record()]))
    manager = IndexManager(db)
    prepared = manager.prepare(SENTENCE_PROFILE)
    extra = next(iter(member_ids(db, LEGACY_PROFILE) - member_ids(db, SENTENCE_PROFILE)))
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO chunk_profile_membership VALUES (%s,%s)", (SENTENCE_PROFILE, extra),
        )
        record_preparation(conn, SENTENCE_PROFILE)
    embed_profile(db, SENTENCE_PROFILE)
    before = db.health()
    with pytest.raises(ValueError, match="membership is incomplete"):
        manager.activate(SENTENCE_PROFILE, expected_source_data_version=prepared["source_data_version"])
    assert db.health() == before


def test_in_place_source_metadata_edit_cannot_be_published(db):
    db.import_batch(ImportBatch(records=[record()]))
    manager = IndexManager(db)
    prepared = manager.prepare(SENTENCE_PROFILE)
    embed_profile(db, SENTENCE_PROFILE)
    with db.connect() as conn:
        conn.execute("UPDATE record_versions SET payload=jsonb_set(payload,'{title}','\"Changed title\"')")
    with pytest.raises(ValueError, match="modified in place"):
        manager.activate(SENTENCE_PROFILE, expected_source_data_version=prepared["source_data_version"])
    assert db.health()["active_profile"] == LEGACY_PROFILE


def test_migration_reproduces_existing_legacy_chunks_without_rewriting(db):
    db.import_batch(ImportBatch(records=[record()]))
    old = db.search("carbon", Filters(), chunks_per_record=20)
    before = db.health()
    with db.connect() as conn:
        original = conn.execute("SELECT * FROM chunks ORDER BY chunk_id").fetchall()
        conn.execute("TRUNCATE retrieval_profiles CASCADE")
    db.initialize()
    assert db.health() == before
    assert all(db.validate_evidence(e) for e in old)
    with db.connect() as conn:
        assert conn.execute("SELECT * FROM chunks ORDER BY chunk_id").fetchall() == original


def test_unknown_or_redefined_profile_is_rejected(db):
    manager = IndexManager(db)
    with pytest.raises(ValueError, match="Unknown"):
        manager.prepare("arbitrary")
    assert definition(SENTENCE_PROFILE)["segmentation"]
    manager.prepare(SENTENCE_PROFILE)
    with db.connect() as conn:
        conn.execute(
            "UPDATE retrieval_profiles SET definition_hash='changed' WHERE profile_id=%s", (SENTENCE_PROFILE,),
        )
    try:
        with pytest.raises(ValueError, match="definition changed"):
            manager.prepare(SENTENCE_PROFILE)
    finally:
        with db.connect() as conn:
            conn.execute("DELETE FROM retrieval_preparations WHERE profile_id=%s", (SENTENCE_PROFILE,))
            conn.execute("DELETE FROM retrieval_profiles WHERE profile_id=%s", (SENTENCE_PROFILE,))


def test_excluded_source_gaps_are_not_indexed_by_candidate(db):
    rec = record()
    split = rec.body.index("Project 50")
    rec.retrieval_ranges = [(0, split), (split + 1000, len(rec.body))]
    db.import_batch(ImportBatch(records=[rec]))
    manager = IndexManager(db)
    prepared = manager.prepare(SENTENCE_PROFILE)
    embed_profile(db, SENTENCE_PROFILE)
    manager.activate(SENTENCE_PROFILE, expected_source_data_version=prepared["source_data_version"])
    hits = db.search("carbon", Filters(), chunks_per_record=20)
    assert hits and all(e.end <= split or e.start >= split + 1000 for e in hits)


def test_identical_chunks_can_be_shared_by_profiles_without_search_duplicates(db):
    rec = record()
    rec.body = "The company proposes carbon capture facilities."
    db.import_batch(ImportBatch(records=[rec]))
    manager = IndexManager(db)
    prepared = manager.prepare(SENTENCE_PROFILE)
    assert member_ids(db, LEGACY_PROFILE) == member_ids(db, SENTENCE_PROFILE)
    assert len(db.pending_embeddings(profile_id=SENTENCE_PROFILE)) == 1
    embed_profile(db, SENTENCE_PROFILE)
    assert not db.pending_embeddings(profile_id=LEGACY_PROFILE)
    manager.activate(SENTENCE_PROFILE, expected_source_data_version=prepared["source_data_version"])
    assert len(db.search("carbon", Filters(), chunks_per_record=20)) == 1
