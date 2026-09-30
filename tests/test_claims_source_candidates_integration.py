"""Literal native-source candidates retain accepted character ranges."""

import pytest
from test_pipeline_integration import db as db

from observatory.models import Filters, ImportBatch, RecordInput

pytestmark = pytest.mark.integration

EXCERPT = "The company describes a proposed carbon capture project."


def source(rid, **changes):
    return RecordInput(record_id=rid, dataset="native", body=EXCERPT, retrievable=True,
                       url="https://example.org/" + rid, **changes)


def test_local_candidates_preserve_original_positions_and_need_source_review(db):
    db.import_batch(ImportBatch(records=[source("a"), source("b"),
                                         RecordInput(record_id="social", dataset="social", body=EXCERPT, retrievable=True)]))
    result = db.claims_source_candidates(EXCERPT)
    assert len(result["candidates"]) == 2 and result["scan_complete"]
    assert result["literal_record_matches"] == 2
    for candidate in result["candidates"]:
        assert candidate["locations"] == [{"start": 0, "end": len(EXCERPT)}]
        assert candidate["quote"] == EXCERPT and candidate["source_association"] == "needs_review"
        assert "body" not in candidate and "payload" not in candidate
    assert not db.claims_source_candidates(EXCERPT.lower())["candidates"]


def test_candidate_limits_are_explicit_and_excluded_ranges_are_not_sources(db):
    body = "Navigation. " + EXCERPT + " Original ending."
    db.import_batch(ImportBatch(records=[
        RecordInput(record_id="excluded", dataset="native", body=body, retrievable=True,
                    retrieval_ranges=[(0, 10)]),
        source("first"), source("second"),
        source("uncountable", countable=False), source("inactive"),
    ]))
    with db.connect() as conn:
        conn.execute("UPDATE records SET active=false WHERE record_id='inactive'")
    result = db.claims_source_candidates(EXCERPT, limit=1)
    assert [item["record_id"] for item in result["candidates"]] == ["first"]
    assert result["literal_record_matches"] == 3 and result["scanned_records"] == 2
    assert not result["scan_complete"]


def test_repeated_literal_locations_are_bounded_with_ambiguity_preserved(db):
    db.import_batch(ImportBatch(records=[RecordInput(record_id="repeated", dataset="native",
                                                    body=(EXCERPT + "\n") * 12, retrievable=True)]))
    candidate = db.claims_source_candidates(EXCERPT)["candidates"][0]
    assert candidate["location_count"] == 12 and len(candidate["locations"]) == 10
    assert not candidate["locations_complete"] and candidate["source_association"] == "needs_review"


def test_source_text_updates_retire_earlier_local_candidates(db):
    db.import_batch(ImportBatch(records=[source("a")]))
    assert db.claims_source_candidates(EXCERPT)["candidates"]
    db.import_batch(ImportBatch(records=[RecordInput(record_id="a", dataset="native",
                                                    body="A different currently stored article.", retrievable=True)]))
    assert not db.claims_source_candidates(EXCERPT)["candidates"]


def test_local_discovery_respects_the_trusted_native_selection(db):
    db.import_batch(ImportBatch(records=[source("a", publisher="Outlet A"), source("b", publisher="Outlet B")]))
    filters = Filters(publishers=["Outlet B"])
    selected = db.claims_source_candidates(EXCERPT, filters=filters)
    assert [candidate["record_id"] for candidate in selected["candidates"]] == ["b"]
    assert selected["filters"] == filters.model_dump(mode="json")
    assert selected["literal_record_matches"] == 1
