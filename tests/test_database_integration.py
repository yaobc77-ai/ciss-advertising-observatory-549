"""Run against a disposable PostgreSQL database, never the user's imported corpus."""

import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import numpy as np
import pytest
from psycopg.conninfo import conninfo_to_dict

from observatory.budget import Budget, LimitReached
from observatory.config import Settings
from observatory.db import Database, digest
from observatory.models import Filters, ImportBatch, RecordInput
from observatory.rag import Rag

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def db():
    Settings.from_env()
    url = os.environ.get("OBS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("OBS_TEST_DATABASE_URL is not configured")
    name = conninfo_to_dict(url).get("dbname", "")
    assert name.startswith("obs_test"), "Refusing to modify a non-test database"
    testdb = Database(url)
    testdb.initialize()
    yield testdb


@pytest.fixture(autouse=True)
def empty(db):
    with db.connect() as conn:
        conn.execute(
            "TRUNCATE records,record_versions,chunks,annotations,imports,usage_ledger,embeddings,answer_runs CASCADE"
        )


def record(rid="n1", dataset="native", sponsor="Company A"):
    return RecordInput(
        record_id=rid,
        dataset=dataset,
        url=f"https://example.org/{rid}",
        title="Carbon capture proposal",
        publisher="Publisher A",
        sponsor=sponsor,
        body="The company proposes carbon capture to reduce emissions. " * 30,
        retrievable=True,
        raw={"secret_internal_field": "not public"},
        disclosure="SPONSORED",
    )


def test_idempotency_and_versioned_evidence_survives_update(db):
    rec = record()
    batch = ImportBatch(records=[rec])
    assert db.import_batch(batch)["new_versions"] == 1
    assert db.import_batch(batch)["unchanged"] == 1
    original = db.search("carbon capture", Filters())[0]
    assert db.validate_evidence(original)
    rec.body = "The company proposes a different emissions policy. " * 30
    assert db.import_batch(ImportBatch(records=[rec]))["new_versions"] == 1
    assert db.validate_evidence(original)
    assert db.public_rows(Filters())[0]["version_id"] != original.version_id
    assert "secret_internal_field" not in str(db.public_rows(Filters()))
    assert "SPONSORED" not in str(db.public_rows(Filters()))


def test_filters_isolate_datasets_unknowns_labels_and_quotes(db):
    a = record()
    a.annotations = [
        {"version": "claims-calibrated", "labels": ["green_labels.false_solutions"]}
    ]
    b = record("s1", "social", "Company B")
    b.platform = "Twitter"
    db.import_batch(ImportBatch(records=[a, b]))
    assert len(db.public_rows(Filters(dataset="all"))) == 2
    assert len(db.public_rows(Filters(labels=["green_labels.false_solutions"]))) == 1
    assert db.public_rows(Filters(sponsors=["x' OR 1=1 --"])) == []
    assert db.public_rows(Filters(include_unknown_dates=False)) == []
    hits = db.search("carbon", Filters(dataset="social"))
    assert hits and all(e.dataset == "social" and e.record_id == "s1" for e in hits)


def test_blank_body_countable_without_search_chunks(db):
    rec = record()
    rec.body = "video"
    rec.retrievable = False
    db.import_batch(ImportBatch(records=[rec]))
    assert len(db.public_rows(Filters())) == 1
    assert db.search("video", Filters()) == []


def test_budget_atomic_concurrent_reservations(db):
    budget = Budget(db, Settings(monthly_budget_usd=Decimal("1")))

    def attempt(i):
        try:
            return budget.reserve("0.60", str(i), "embedding", "text-embedding-3-small")
        except LimitReached:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert sum(x is not None for x in results) == 1
    assert budget.summary()[0]["exposure_usd"] == Decimal(".60")


def test_rate_limit_and_concurrency_and_reconciliation(db):
    budget = Budget(db, Settings(requests_per_minute=1, concurrency=2))
    one = budget.reserve(".04", "alice", "generation", "gpt-5.6-luna")
    with pytest.raises(LimitReached):
        budget.reserve(".04", "alice", "generation", "gpt-5.6-luna")
    two = budget.reserve(".04", "bob", "generation", "gpt-5.6-luna")
    with pytest.raises(LimitReached):
        budget.reserve(".04", "carol", "generation", "gpt-5.6-luna")
    budget.settle(one, Decimal(".001"), {"input_tokens": 1})
    with pytest.raises(ValueError):
        budget.settle(one, Decimal(".001"), {})
    budget.uncertain(two, "APITimeoutError")
    budget.reserve(".04", "carol", "generation", "gpt-5.6-luna")
    assert budget.summary()[0]["exposure_usd"] == Decimal(".081")


def test_expired_reservation_never_releases_spend(db):
    budget = Budget(db, Settings(monthly_budget_usd=Decimal(".05")))
    one = budget.reserve(".04", "alice", "generation", "gpt-5.6-luna")
    with db.connect() as conn:
        conn.execute(
            "UPDATE usage_ledger SET expires_at=now()-interval '1 hour' WHERE reservation_id=%s",
            (one,),
        )
    with pytest.raises(LimitReached):
        budget.reserve(".04", "bob", "generation", "gpt-5.6-luna")
    assert budget.summary()[0]["exposure_usd"] == Decimal(".04")


def test_embedding_cache_roundtrip_and_filtered_exact_vector_search(db):
    db.import_batch(ImportBatch(records=[record(), record("s1", "social")]))
    text = "cached query"
    vector = np.array([1.0] + [0.0] * 1535)
    with db.connect(vector=True) as conn:
        conn.execute(
            "INSERT INTO embeddings(text_hash,model,embedding) VALUES (%s,%s,%s)",
            (digest(text), "text-embedding-3-small", vector),
        )
        chunks = conn.execute("SELECT DISTINCT text_hash FROM chunks").fetchall()
        for c in chunks:
            conn.execute(
                "INSERT INTO embeddings(text_hash,model,embedding) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (c["text_hash"], "text-embedding-3-small", vector),
            )
    rag = Rag(db, Settings(), client=object())
    assert rag.embed([text])[0] == [1.0] + [0.0] * 1535
    hits = db.search("carbon", Filters(dataset="social"), vector=vector)
    assert len(hits) == 1 and hits[0].record_id == "s1"


def test_explicit_snapshot_retires_missing_rows_but_preserves_old_citations(db):
    a, b = record("n1"), record("n2")
    db.import_batch(ImportBatch(records=[a, b]), snapshot_dataset="native")
    old = db.search("carbon", Filters(record_ids=["n2"]))[0]
    result = db.import_batch(ImportBatch(records=[a]), snapshot_dataset="native")
    assert result["deactivated"] == ["n2"]
    assert [r["record_id"] for r in db.public_rows(Filters())] == ["n1"]
    assert db.validate_evidence(old)
    with pytest.raises(ValueError):
        db.import_batch(ImportBatch(), snapshot_dataset="native")
    db.import_batch(ImportBatch(records=[a, b]), snapshot_dataset="native")
    assert len(db.public_rows(Filters())) == 2


def test_multiple_passages_preserve_record_diversity_and_late_evidence(db):
    rec = record()
    rec.body = (
        "Carbon capture water treatment introduction. " * 80
        + "\n\n"
        + "The bacteria and microalgae produce biogas and offset CO2 emissions. " * 40
    )
    db.import_batch(ImportBatch(records=[rec, record("n2")]))
    hits = db.search(
        "carbon bacteria microalgae biogas", Filters(), limit=2, chunks_per_record=3
    )
    assert {e.record_id for e in hits} == {"n1", "n2"}
    assert sum(e.record_id == "n1" for e in hits) > 1
    assert any("microalgae produce biogas" in e.text for e in hits)
    assert all(db.validate_evidence(e) for e in hits)
