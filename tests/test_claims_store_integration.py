"""Publication uses synthetic reviewer assertions in an isolated test schema only."""

import json
import os
from uuid import uuid4

import psycopg
import pytest
from claims_publication_fixtures import (
    approve_synthetic,
    publication_files,
    read_review,
    write_review,
)
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from observatory.claims_publication import prepare_claims_import
from observatory.claims_store import ClaimsStore
from observatory.config import Settings
from observatory.db import Database
from observatory.models import Filters, ImportBatch, RecordInput

pytestmark = pytest.mark.integration


@pytest.fixture()
def db():
    Settings.from_env()
    url = os.environ.get("OBS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("OBS_TEST_DATABASE_URL is not configured")
    assert conninfo_to_dict(url).get("dbname", "").startswith("obs_test"), "Refusing non-test database"
    schema = f"obs_claims_test_{uuid4().hex}"
    with psycopg.connect(url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        repository = Database(make_conninfo(url, options=f"-c search_path={schema},public"))
        repository.initialize()
        yield repository
    finally:
        with psycopg.connect(url) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def prepare(files):
    return prepare_claims_import(files["audit_dir"], files["bundle_dir"], files["review_path"])


def seed(db, files):
    with db.connect() as conn:
        for record in files["records"]:
            payload = RecordInput(**{key: value for key, value in record.items()
                                     if key not in {"version_id", "body_hash"}}, retrievable=True).model_dump(mode="json")
            conn.execute("INSERT INTO records(record_id,dataset,current_version) VALUES (%s,%s,%s)",
                         (record["record_id"], record["dataset"], record["version_id"]))
            conn.execute("INSERT INTO record_versions(version_id,record_id,body,body_hash,payload) VALUES (%s,%s,%s,%s,%s)",
                         (record["version_id"], record["record_id"], record["body"], record["body_hash"], Jsonb(payload)))


def stored_counts(db):
    with db.connect() as conn:
        return {table: conn.execute(f"SELECT count(*) AS count FROM {table}").fetchone()["count"]
                for table in ("claims2_taxonomies", "claims2_runs", "claims2_imports", "claims2_results", "claims2_reviews")}


def prepared_store(db, tmp_path):
    files = publication_files(tmp_path)
    seed(db, files)
    approve_synthetic(files)
    return files, ClaimsStore(db)


def test_pending_review_does_not_open_a_database(tmp_path):
    files = publication_files(tmp_path)
    # Deliberately unusable connection; all-held review must remain offline.
    report = ClaimsStore(Database("")).import_prepared(prepare(files), apply=True)
    assert not report["publication_applied"] and report["inserted"] == 0
    assert report["model_calls"] == 0


def test_dry_run_and_repeat_preserve_article_and_index_versions(db, tmp_path):
    files, store = prepared_store(db, tmp_path)
    before = db.health()
    dry = store.import_prepared(prepare(files))
    assert dry["planned_insertions"] == 3 and dry["planned_review_revisions"] == 3
    assert set(stored_counts(db).values()) == {0}
    applied = store.import_prepared(prepare(files), apply=True)
    assert applied["inserted"] == 3 and applied["review_revisions"] == 3
    repeated = store.import_prepared(prepare(files), apply=True)
    assert repeated["inserted"] == 0 and repeated["unchanged"] == 3
    assert stored_counts(db) == {"claims2_taxonomies": 1, "claims2_runs": 1, "claims2_imports": 1,
                                 "claims2_results": 3, "claims2_reviews": 3}
    after = db.health()
    for field in ("data_version", "source_data_version", "index_version", "active_profile", "chunks", "record_counts"):
        assert after.get(field) == before.get(field)
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM annotations").fetchone()["count"] == 0


def test_queries_count_records_separately_and_paginate_all_matching_labels(db, tmp_path):
    files, store = prepared_store(db, tmp_path)
    store.import_prepared(prepare(files), apply=True)
    page = store.matches(Filters(), limit=1)
    assert page["total_records"] == 2 and page["total_matches"] == 3
    assert len(page["records"]) == 1 and len(page["records"][0]["claims"]) == 2
    second = store.matches(Filters(), limit=1, offset=1)
    assert len(second["records"][0]["claims"]) == 1
    assert store.matches(Filters(), sc_ids=["SC_1"])["total_matches"] == 2
    assert store.matches(Filters(), nc_ids=["NC_1"])["total_records"] == 1
    assert store.matches(Filters(dataset="social"))["total_records"] == 0
    assert store.matches(Filters(sponsors=[files["records"][1]["sponsor"]]))["total_matches"] == 1
    assert store.matches(Filters(publishers=["Unmatched outlet"]))["total_records"] == 0
    claim = page["records"][0]["claims"][0]
    assert claim["quote"] == files["body"][claim["start"]:claim["end"]]
    serialized = json.dumps(page)
    for private in ("Synthetic source reviewer", "raw_content_pointer", "input_file_sha256", str(tmp_path)):
        assert private not in serialized
    assert "unmatched records are not classified negatives" in page["coverage"]


@pytest.mark.parametrize("mutation", ["stale", "body", "excluded"])
def test_one_invalid_source_rejects_the_entire_batch_before_writes(db, tmp_path, mutation):
    files, store = prepared_store(db, tmp_path)
    with db.connect() as conn:
        record = files["records"][1]
        if mutation == "stale":
            conn.execute("UPDATE records SET active=false WHERE record_id=%s", (record["record_id"],))
        elif mutation == "body":
            conn.execute("UPDATE record_versions SET body='Changed' WHERE version_id=%s", (record["version_id"],))
        else:
            conn.execute("UPDATE record_versions SET payload=jsonb_set(payload,'{retrievable}','false') WHERE version_id=%s",
                         (record["version_id"],))
    with pytest.raises(ValueError):
        store.import_prepared(prepare(files), apply=True)
    assert set(stored_counts(db).values()) == {0}


def test_incremental_approval_and_semantic_promotion_append_reviews(db, tmp_path):
    files = publication_files(tmp_path)
    seed(db, files)
    store = ClaimsStore(db)
    approve_synthetic(files, candidate_keys=[files["candidate_keys"][0]])
    first = store.import_prepared(prepare(files), apply=True)
    assert first["inserted"] == 1
    approve_synthetic(files, candidate_keys=[files["candidate_keys"][1]])
    second = store.import_prepared(prepare(files), apply=True)
    assert second["inserted"] == 1 and second["unchanged"] == 1 and second["review_revisions"] == 1
    prior_version = store.matches(Filters())["claims_version"]
    approve_synthetic(files, semantic="supported", candidate_keys=[files["candidate_keys"][0]])
    promoted = store.import_prepared(prepare(files), apply=True)
    assert promoted["inserted"] == 0 and promoted["review_revisions"] == 1
    current = store.matches(Filters())
    assert current["total_records"] == 1 and current["total_matches"] == 2
    assert current["claims_version"] != prior_version
    assert store.matches(Filters(), review_state="human_supported")["total_matches"] == 1
    assert store.matches(Filters(), review_state="automatic_unverified")["total_matches"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) AS count FROM claims2_results").fetchone()["count"] == 2
        assert conn.execute("SELECT count(*) AS count FROM claims2_reviews").fetchone()["count"] == 3


def test_retraction_is_append_only_idempotent_and_reimport_never_reactivates(db, tmp_path):
    files, store = prepared_store(db, tmp_path)
    store.import_prepared(prepare(files), apply=True)
    initial = store.matches(Filters())
    kwargs = {"reviewer": "Synthetic retraction reviewer", "reason": "Synthetic correction",
              "reviewed_at": "2026-09-30T12:00:00Z"}
    assert store.retract(files["candidate_keys"][0], **kwargs)["recorded"]
    assert not store.retract(files["candidate_keys"][0], **kwargs)["recorded"]
    assert store.matches(Filters())["total_matches"] == 2
    assert store.matches(Filters())["claims_version"] != initial["claims_version"]
    store.import_prepared(prepare(files), apply=True)
    assert store.matches(Filters())["total_matches"] == 2
    approve_synthetic(files, semantic="supported")
    store.import_prepared(prepare(files), apply=True)
    assert store.matches(Filters(), review_state="human_supported")["total_matches"] == 2
    assert stored_counts(db)["claims2_results"] == 3
    with pytest.raises(ValueError, match="unknown"):
        store.retract("f" * 64, **kwargs)


@pytest.mark.parametrize("decision", ["hold", "reject", "unsupported"])
def test_later_review_file_is_not_an_implicit_retraction(db, tmp_path, decision):
    files, store = prepared_store(db, tmp_path)
    store.import_prepared(prepare(files), apply=True)
    if decision == "unsupported":
        approve_synthetic(files, semantic="unsupported")
    else:
        review = read_review(files)
        for item in review["decisions"].values():
            item["publication"] = decision
        write_review(files, review)
    result = store.import_prepared(prepare(files), apply=True)
    assert not result["publication_applied"]
    assert store.matches(Filters())["total_matches"] == 3


def test_supported_review_cannot_be_downgraded_by_an_older_file(db, tmp_path):
    files, store = prepared_store(db, tmp_path)
    older = files["review_path"].read_bytes()
    store.import_prepared(prepare(files), apply=True)
    approve_synthetic(files, semantic="supported")
    store.import_prepared(prepare(files), apply=True)
    files["review_path"].write_bytes(older)
    before = stored_counts(db)
    for apply in (False, True):
        with pytest.raises(ValueError, match="downgraded"):
            store.import_prepared(prepare(files), apply=apply)
    assert store.matches(Filters(), review_state="human_supported")["total_matches"] == 3
    assert stored_counts(db) == before


def test_body_update_hides_old_labels_without_deleting_analysis(db, tmp_path):
    files, store = prepared_store(db, tmp_path)
    store.import_prepared(prepare(files), apply=True)
    changed = dict(files["records"][0])
    changed.pop("version_id")
    changed.pop("body_hash")
    changed["body"] += " A newly supplied paragraph."
    db.import_batch(ImportBatch(records=[RecordInput(**changed, retrievable=True)]))
    assert store.matches(Filters())["total_records"] == 1
    assert store.matches(Filters())["total_matches"] == 1
    assert stored_counts(db)["claims2_results"] == 3
    with pytest.raises(ValueError, match="current version"):
        store.import_prepared(prepare(files), apply=True)


@pytest.mark.parametrize("table,key_column,hash_column", [
    ("claims2_taxonomies", "bundle_fingerprint", "payload_sha256"),
    ("claims2_runs", "run_key", "metadata_sha256"),
    ("claims2_results", "candidate_key", "payload_sha256"),
])
def test_identity_conflicts_reject_dry_run_and_apply_atomically(db, tmp_path, table, key_column, hash_column):
    files, store = prepared_store(db, tmp_path)
    store.import_prepared(prepare(files), apply=True)
    with db.connect() as conn:
        conn.execute(sql.SQL("UPDATE {} SET {}=%s WHERE {}=(SELECT {} FROM {} LIMIT 1)").format(
            sql.Identifier(table), sql.Identifier(hash_column), sql.Identifier(key_column),
            sql.Identifier(key_column), sql.Identifier(table)), ("0" * 64,))
    before = stored_counts(db)
    for apply in (False, True):
        with pytest.raises(ValueError, match="conflicting"):
            store.import_prepared(prepare(files), apply=apply)
    assert stored_counts(db) == before


def test_database_foreign_key_rejects_a_version_belonging_to_another_record(db, tmp_path):
    files, store = prepared_store(db, tmp_path)
    store.import_prepared(prepare(files), apply=True)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db.connect() as conn:
            conn.execute("UPDATE claims2_results SET record_id=%s WHERE version_id=%s",
                         (files["records"][1]["record_id"], files["records"][0]["version_id"]))
