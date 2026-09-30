"""Retained article intervals must not create synthetic text across removed gaps."""

import os

import pytest
from psycopg.conninfo import conninfo_to_dict
from pydantic import ValidationError

from observatory.chunking import chunk_body, chunk_retrieval_body, retrieval_spans
from observatory.config import Settings
from observatory.db import Database
from observatory.evaluate import EvaluationCase, EvaluationInvalid, validate_gold
from observatory.models import Filters, ImportBatch, RecordInput


def record(body, **changes):
    return RecordInput(record_id="ranges-article", dataset="native", body=body,
                       title="Retained interval fixture", retrievable=True, **changes)


def case(quote):
    return EvaluationCase(
        id="range-case", suite="development", dataset="native", status="ready",
        case_type="retrieval", question="What does this article say?",
        filters=Filters(dataset="native"), required_record_ids=["ranges-article"],
        support_quote=[{"record_id": "ranges-article", "quote": quote}],
        rubric=["Use the retained original passage."], selection_note="Unit fixture only.",
    )


def gold_spans(body, quote, **payload):
    source = {"record_id": "ranges-article", "dataset": "native", "version_id": "fixture-v1",
              "body": body, "payload": {"retrievable": True, **payload}}
    return validate_gold([case(quote)], {"ranges-article": source},
                         {"range-case": [{"record_id": "ranges-article"}]})["range-case"]


def test_removed_navigation_does_not_join_text_or_renumber_original_paragraphs():
    first, nav, last = "First carbon paragraph.", "RELATED navigation only.", "Later biogas paragraph."
    body = "\r\n\r\n".join([first, nav, last])
    ranges = [(0, len(first)), (body.index(last), len(body))]
    chunks = chunk_retrieval_body(body, retrieval_ranges=ranges)
    assert [c["text"] for c in chunks] == [first, last]
    assert [c["paragraph_ids"] for c in chunks] == [["p1"], ["p3"]]
    assert [(c["start"], c["end"]) for c in chunks] == ranges
    assert all(body[c["start"]:c["end"]] == c["text"] for c in chunks)


def test_long_unicode_intervals_preserve_offsets_tokens_and_coverage():
    first = "中文与🙂 methane café e\u0301. " * 70
    omitted = "UNRELATED NAVIGATION"
    last = "后续正文 carbon capture and biogas. " * 80
    body = first + "\n\n" + omitted + "\n\n" + last
    ranges = [(4, len(first) - 3), (body.index(last) + 5, len(body) - 4)]
    chunks = chunk_retrieval_body(body, 73, 13, retrieval_ranges=ranges)
    assert len(chunks) > 4
    covered = set()
    for chunk in chunks:
        assert chunk["text"] == body[chunk["start"]:chunk["end"]]
        assert any(start <= chunk["start"] < chunk["end"] <= end for start, end in ranges)
        assert 0 < chunk["token_count"] <= 73
        assert chunk["paragraph_ids"] in [["p1"], ["p3"]]
        covered.update(range(chunk["start"], chunk["end"]))
    expected = {i for start, end in ranges for i in range(start, end) if not body[i].isspace()}
    assert expected.issubset(covered)
    assert not covered.intersection(range(len(first), body.index(last)))


def test_adjacent_intervals_are_still_chunked_separately_in_the_same_paragraph():
    chunks = chunk_retrieval_body("AlphaBeta", retrieval_ranges=[(0, 5), (5, 9)])
    assert [c["text"] for c in chunks] == ["Alpha", "Beta"]
    assert [c["paragraph_ids"] for c in chunks] == [["p1"], ["p1"]]


@pytest.mark.parametrize("end", [None, 0, 9, 24])
def test_none_ranges_preserve_legacy_prefix_chunks(end):
    body = "Alpha energy.\n\nBeta gas."
    assert len(body) == 24
    assert chunk_retrieval_body(body, retrieval_end=end) == chunk_body(body[:end])


@pytest.mark.parametrize("ranges", [
    [], [(0, 0)], [(4, 2)], [(-1, 3)], [(0, 11)],
    [(0, 5), (4, 8)], [(6, 8), (0, 2)], [(0, True)], [(0, 2.0)], [("0", 2)],
])
def test_invalid_ranges_are_rejected_at_the_input_boundary(ranges):
    with pytest.raises(ValidationError):
        record("0123456789", retrieval_ranges=ranges)


@pytest.mark.parametrize("rebuild_from_dict", [False, True])
def test_model_revalidation_rejects_invalid_assignment(rebuild_from_dict):
    rec = record("0123456789", retrieval_ranges=[(0, 5)])
    rec.retrieval_ranges = [(0, 5), (4, 8)]
    source = rec.model_dump() if rebuild_from_dict else rec
    with pytest.raises(ValidationError, match="non-overlapping"):
        RecordInput.model_validate(source)


def test_explicit_ranges_override_even_an_outdated_legacy_prefix_end():
    rec = record("before retained", retrieval_ranges=[(7, 15)], retrieval_end=99)
    chunks = chunk_retrieval_body(rec.body, retrieval_ranges=rec.retrieval_ranges,
                                  retrieval_end=rec.retrieval_end)
    assert [c["text"] for c in chunks] == ["retained"]
    assert retrieval_spans(rec.body, retrieval_ranges=rec.retrieval_ranges, retrieval_end=2) == [(7, 15)]


def test_gold_quote_in_later_retained_interval_uses_its_original_offset():
    body = "intro\n\nRELATED\n\nLate biogas evidence."
    start = body.index("Late")
    spans = gold_spans(body, "Late biogas evidence.", retrieval_ranges=[[0, 5], [start, len(body)]], retrieval_end=5)
    assert spans[0]["start"] == start and spans[0]["end"] == len(body)


def test_repeated_quote_uses_accepted_occurrence_instead_of_excluded_first_match():
    body = "same quote NAV same quote"
    start = body.rindex("same quote")
    spans = gold_spans(body, "same quote", retrieval_ranges=[[start, len(body)]])
    assert spans[0]["start"] == start


@pytest.mark.parametrize("ranges", [[[0, 5], [8, 13]], [[0, 5], [5, 13]]])
def test_gold_cannot_span_multiple_intervals_even_when_present_in_original_body(ranges):
    body = "AlphaNAVOmega"
    with pytest.raises(EvaluationInvalid, match="within one retained interval"):
        gold_spans(body, body, retrieval_ranges=ranges)


def test_gold_rejects_quote_wholly_in_excluded_navigation():
    with pytest.raises(EvaluationInvalid, match="accepted retrieval boundary"):
        gold_spans("keep NAV tail", "NAV", retrieval_ranges=[[0, 4], [9, 13]])


def test_gold_rejects_invalid_stored_ranges_instead_of_ignoring_them():
    with pytest.raises(EvaluationInvalid, match="invalid retrieval ranges"):
        gold_spans("original body", "original", retrieval_ranges=[[0, 50]])


@pytest.fixture
def range_db():
    Settings.from_env()
    url = os.environ.get("OBS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("OBS_TEST_DATABASE_URL is not configured")
    assert conninfo_to_dict(url).get("dbname") == "obs_test", "Only the disposable obs_test database is permitted"
    db = Database(url)
    with db.connect() as conn:
        assert conn.execute("SELECT current_database() AS name").fetchone()["name"] == "obs_test"
    db.initialize()

    def clear():
        with db.connect() as conn:
            assert conn.execute("SELECT current_database() AS name").fetchone()["name"] == "obs_test"
            conn.execute("TRUNCATE records,record_versions,chunks,annotations,imports,usage_ledger,embeddings,answer_runs,generation_outputs RESTART IDENTITY CASCADE")

    clear()
    try:
        yield db
    finally:
        clear()


@pytest.mark.integration
def test_database_indexes_late_body_excludes_navigation_and_retains_old_evidence(range_db):
    first, nav, last = "The carbon project is planned.", "NavigationUnique unrelated advertisement.", "Bacteria produce biogas in the proposed treatment."
    body = "\n\n".join([first, nav, last])
    old = record(body)
    range_db.import_batch(ImportBatch(records=[old]))
    old_evidence = range_db.search("NavigationUnique", Filters())[0]
    ranges = [(0, len(first)), (body.index(last), len(body))]
    updated = record(body, retrieval_ranges=ranges, retrieval_end=len(first))
    assert range_db.import_batch(ImportBatch(records=[updated]))["new_versions"] == 1
    assert range_db.import_batch(ImportBatch(records=[updated]))["unchanged"] == 1
    assert range_db.search("NavigationUnique", Filters()) == []
    current = range_db.search("biogas", Filters())[0]
    assert current.text == last and current.start == body.index(last)
    assert current.paragraph_ids == ["p3"]
    assert current.version_id != old_evidence.version_id
    assert range_db.validate_evidence(current) and range_db.validate_evidence(old_evidence)
    with range_db.connect() as conn:
        versions = conn.execute("SELECT body,body_hash,payload FROM record_versions ORDER BY created_at").fetchall()
        assert len(versions) == 2 and {v["body"] for v in versions} == {body}
        assert len({v["body_hash"] for v in versions}) == 1
        assert versions[-1]["payload"]["retrieval_ranges"] == [list(pair) for pair in ranges]


@pytest.mark.integration
def test_mutated_invalid_ranges_roll_back_entire_import_even_if_nonretrievable(range_db):
    good = record("Valid carbon evidence.")
    bad = RecordInput(record_id="bad-ranges", dataset="native", body="0123456789",
                      retrieval_ranges=[(0, 5)], retrievable=False)
    batch = ImportBatch(records=[good, bad])
    bad.retrieval_ranges = [(0, 5), (4, 8)]
    with pytest.raises(ValueError, match="non-overlapping"):
        range_db.import_batch(batch)
    with range_db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM records").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM record_versions").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM imports").fetchone()["n"] == 0
