"""Search transparency: location/lexeme coverage is not semantic confidence."""

import os
from datetime import date
from types import SimpleNamespace

import pytest
from psycopg.conninfo import conninfo_to_dict

from observatory.config import Settings
from observatory.db import Database
from observatory.models import Evidence, Filters, ImportBatch, RecordInput
from observatory.service import Service


def test_evidence_is_backward_compatible_with_historical_answer_payloads():
    result = Evidence(
        evidence_id="e1", record_id="r1", version_id="v1", dataset="native",
        title="Historical result", text="An old passage.", start=0, end=15,
    )
    assert result.published_at is None
    assert result.retrieval_rank is None
    assert result.retrieval_sources == result.matched_terms == result.literal_matched_terms == []


def test_search_report_preserves_title_scope_source_toggle_and_free_contract():
    seen = []
    evidence = Evidence(
        evidence_id="e1", record_id="r1", version_id="v1", dataset="native",
        title="A specifically named article", text="Source.", start=0, end=7,
        url="https://example.org", archive_url="javascript:bad", published_at="2024-01-01",
    )

    def report(query, filters, limit):
        seen.append((filters, limit))
        return {"evidence": [evidence], "diagnostics": {"operator": "OR"}}

    service = Service(
        Settings(show_source_links=False),
        db=SimpleNamespace(search_report=report, public_rows=lambda _: [
            {"title": evidence.title, "record_id": evidence.record_id}
        ]), rag=object(),
    )
    result = service.search_report("What does A specifically named article claim?", Filters(), limit=30)
    assert result["evidence"][0].published_at == date(2024, 1, 1)
    assert result["evidence"][0].url == result["evidence"][0].archive_url == ""
    assert seen[0][0].record_ids == ["r1"] and seen[0][1] == 10
    assert result["diagnostics"] == {"operator": "OR"}
    assert service.search_report("", Filters())["diagnostics"]["status"] == "unavailable"
    assert len(seen) == 1  # Invalid input cannot query the database or paid services.


@pytest.fixture
def coverage_db():
    Settings.from_env()
    url = os.environ.get("OBS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("OBS_TEST_DATABASE_URL is not configured")
    assert conninfo_to_dict(url).get("dbname", "").startswith("obs_test")
    db = Database(url)
    db.initialize()

    def reset():
        with db.connect() as conn:
            assert conn.execute("SELECT current_database() AS n").fetchone()["n"].startswith("obs_test")
            conn.execute(
                "TRUNCATE records,record_versions,chunks,annotations,imports,usage_ledger,"
                "embeddings,answer_runs,retrieval_preparations,retrieval_publications CASCADE"
            )
            conn.execute("UPDATE retrieval_state SET active_profile='legacy600-v1' WHERE singleton")

    reset()
    yield db
    reset()


def record(rid, body, **kwargs):
    return RecordInput(
        record_id=rid, dataset=kwargs.pop("dataset", "native"), title=f"Article {rid}",
        body=body, retrievable=True, **kwargs,
    )


@pytest.mark.integration
def test_term_outside_top_five_is_not_reported_as_absent_from_corpus(coverage_db):
    # More than 50 strong hits also puts the rare term outside the retrieval
    # candidate cap: diagnostics must scan the whole eligible index scope.
    rows = [record(f"strong-{i}", "Carbon capture storage. " * 20) for i in range(60)]
    rows.append(record("outside", "A brief biogas mention."))
    coverage_db.import_batch(ImportBatch(records=rows))
    report = coverage_db.search_report("carbon capture and storage biogas", Filters())
    diagnostic = report["diagnostics"]
    assert len(report["evidence"]) == 5
    assert all("biogas" not in row.text for row in report["evidence"])
    assert "biogas" in diagnostic["missing_from_results"]
    assert "biogas" not in diagnostic["missing_from_scope"]
    assert diagnostic["scope_records"] == 61
    assert next(row for row in diagnostic["term_details"] if row["term"] == "biogas")["matching_records"] == 1
    assert diagnostic["operator"] == "OR"
    assert all(row.retrieval_sources == ["keyword"] for row in report["evidence"])
    assert [row.retrieval_rank for row in report["evidence"]] == [1, 2, 3, 4, 5]


@pytest.mark.integration
def test_absence_is_limited_to_filtered_current_index_and_not_metadata(coverage_db):
    coverage_db.import_batch(ImportBatch(records=[
        record("eligible", "Carbon capture.", sponsor="a", keyword="biogas", published_at=date(2024, 5, 1)),
        record("other-sponsor", "Biogas capture.", sponsor="b", published_at=date(2024, 5, 1)),
        record("other-dataset", "Biogas capture.", sponsor="a", dataset="social", published_at=date(2024, 5, 1)),
        record("old-date", "Biogas capture.", sponsor="a", published_at=date(2020, 5, 1)),
    ]))
    result = coverage_db.search_report(
        "capture biogas", Filters(sponsors=["a"], date_from=date(2024, 1, 1)),
    )
    assert result["diagnostics"]["missing_from_scope"] == ["biogas"]
    assert result["diagnostics"]["scope_records"] == 1
    assert result["evidence"][0].published_at == date(2024, 5, 1)


@pytest.mark.integration
def test_stem_match_is_not_described_as_literal_match(coverage_db):
    coverage_db.import_batch(ImportBatch(records=[record("a", "Capturing emissions is the proposal.")]))
    result = coverage_db.search_report("capture", Filters())
    ev = result["evidence"][0]
    assert ev.matched_terms == ["capture"]
    assert ev.literal_matched_terms == []
    assert result["diagnostics"]["missing_from_scope"] == []
    assert result["diagnostics"]["term_details"][0]["lexemes"] == ["captur"]


@pytest.mark.integration
def test_old_versions_and_chunks_outside_active_profile_are_not_counted(coverage_db):
    coverage_db.import_batch(ImportBatch(records=[record("old", "Biogas is here.")]))
    coverage_db.import_batch(ImportBatch(records=[
        record("old", "Carbon is here."), record("unindexed", "Biogas is here."),
    ]))
    with coverage_db.connect() as conn:
        conn.execute(
            "DELETE FROM chunk_profile_membership m USING chunks c,retrieval_state s "
            "WHERE m.chunk_id=c.chunk_id AND c.record_id='unindexed' AND m.profile_id=s.active_profile"
        )
    result = coverage_db.search_report("carbon biogas", Filters())
    assert result["diagnostics"]["missing_from_scope"] == ["biogas"]
    assert result["diagnostics"]["scope_records"] == 1


@pytest.mark.integration
@pytest.mark.parametrize("question", ["the and is", "were being", "中文问题碳捕集"])
def test_unsupported_or_stopword_query_does_not_make_absence_claims(coverage_db, question):
    result = coverage_db.search_report(question, Filters())
    assert result["diagnostics"]["status"] == "unavailable"
    assert result["diagnostics"]["missing_from_scope"] == []
    assert result["diagnostics"]["scope_records"] is None


@pytest.mark.integration
def test_mixed_language_diagnostic_is_explicitly_partial(coverage_db):
    coverage_db.import_batch(ImportBatch(records=[record("a", "Carbon capture.")]))
    result = coverage_db.search_report("中文 carbon", Filters())
    assert result["diagnostics"]["status"] == "partial"
    assert result["diagnostics"]["terms"] == ["carbon"]
    assert result["diagnostics"]["ignored_terms"] == ["中文"]
    assert result["diagnostics"]["missing_from_scope"] == []
