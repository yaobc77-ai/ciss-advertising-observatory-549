"""Published-assignment reads through shared service/tool/model transports.

All evidence and review identities here are synthetic. These checks establish
scope, projection and routing, not classification or model-language accuracy.
"""

from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
from test_research_agent import FakeCatalog, harness, response
from test_research_service import Database, setup_service
from test_research_tools import make_catalog

from observatory.config import Settings
from observatory.models import Filters
from observatory.research_agent import ResearchRun
from observatory.service import Service

QUOTE = "甲😀Company discusses emissions."
VERSION = "a" * 64


def published_result():
    claim = {
        "candidate_key": "b" * 64, "record_id": "r1", "version_id": "version-r1",
        "body_hash": "c" * 64, "dataset": "native", "nc_id": "NC_1", "sc_id": "SC_2",
        "nc_definition": "A synthetic emissions claim.",
        "sc_definition": "A synthetic umbrella category.",
        "review_state": "automatic_unverified", "review_version": "d" * 64,
        "quote": QUOTE, "start": 3, "end": 3 + len(QUOTE),
        "run_id": "synthetic-run", "taxonomy_version": "e" * 64,
        "title": "Synthetic record", "publisher": "The Washington Post",
        "sponsor": "exxonmobil", "date": "2020-03-01", "url": "https://example.org/r1",
        "archive_url": "https://archive.example.org/r1",
    }
    return {"available": True, "claims_version": VERSION, "total_records": 1, "total_matches": 1,
            "records": [{"record_id": "r1", "claims": [claim]}], "offset": 0, "limit": 20,
            "meaning": "Published taxonomy assignments; not independent fact checking.",
            "coverage": "Records with published matches only; unmatched records are not classified negatives."}


class Store:
    def __init__(self, result=None):
        self.result = result if result is not None else published_result()
        self.calls = []

    def matches(self, filters, **kwargs):
        self.calls.append((filters.model_copy(deep=True), deepcopy(kwargs)))
        return self.result


def claims_catalog(base=None, *, result=None, links=True):
    catalog, db = make_catalog(base, links=links)
    store = Store(result)
    catalog.service.claims_store = store
    return catalog, db, store


def test_service_keeps_exact_evidence_versions_definitions_and_source_projection():
    store = Store()
    service = Service(Settings(), db=object(), rag=object(), claims_store=store)
    filters = Filters(record_ids=["r1"])
    result = service.claims_matches(filters, nc_ids=["NC_1"], sc_ids=["SC_2"],
                                    taxonomy="e" * 64, review_state="automatic_unverified")
    claim = result["records"][0]["claims"][0]
    assert result["status"] == "ok" and result["claims_version"] == VERSION
    assert result["total_records"] == result["total_matches"] == 1
    assert result["filters"]["record_ids"] == ["r1"]
    assert result["records"][0]["title"] == "Synthetic record"
    assert claim["quote"] == QUOTE and claim["end"] - claim["start"] == len(QUOTE)
    assert claim["nc_definition"] == "A synthetic emissions claim."
    assert claim["review_state"] == "automatic_unverified"
    assert result["source_refs"][0]["body_hash"] == "c" * 64
    assert result["source_refs"][0]["version_id"] == "version-r1"
    assert result["source_refs"][0]["review_version"] == "d" * 64
    assert result["model_calls"] == 0
    assert "not classified negatives" in result["coverage"]
    assert store.calls[0][0] == filters
    assert store.calls[0][1]["taxonomy"] == "e" * 64
    # A public URL policy never mutates the store's snapshot object.
    claim["quote"] = "modified caller view"
    assert store.result["records"][0]["claims"][0]["quote"] == QUOTE


@pytest.mark.parametrize("url", ["file:///private.pdf", "javascript:alert(1)",
                                  "https://user:password@example.org", "//example.org"])
@pytest.mark.parametrize("field", ["url", "archive_url"])
def test_service_claims_link_policy_rejects_private_or_unsafe_sources(url, field):
    result = published_result()
    result["records"][0]["claims"][0][field] = url
    service = Service(Settings(), db=object(), rag=object(), claims_store=Store(result))
    assert service.claims_matches(Filters())["records"][0]["claims"][0][field] == ""


def test_service_claims_source_toggle_applies_to_top_record_and_assignment():
    service = Service(Settings(show_source_links=False), db=object(), rag=object(), claims_store=Store())
    row = service.claims_matches(Filters())["records"][0]
    assert row["url"] == row["claims"][0]["url"] == ""
    assert row["archive_url"] == row["claims"][0]["archive_url"] == ""


def test_missing_schema_is_unavailable_and_preserves_positive_only_coverage():
    unavailable = {"available": False, "claims_version": None, "total_records": 0,
                   "total_matches": 0, "records": [], "offset": 0, "limit": 20}
    catalog, _, _ = claims_catalog(result=unavailable)
    result = catalog.call("get_claims_matches", {})
    assert result["status"] == "unavailable" and not result["available"]
    assert result["records"] == result["source_refs"] == []
    assert result["claims_version"] is None
    assert "not classified negatives" in result["coverage"]
    assert "not independent fact checking" in result["meaning"]


def test_claims_driver_unavailable_never_exposes_diagnostics_or_fakes_evidence():
    catalog, _, store = claims_catalog()

    def fail(*_args, **_kwargs):
        raise RuntimeError("postgres://secret:password@private-host SELECT confidential")

    store.matches = fail
    result = catalog.call("get_claims_matches", {})
    assert result["status"] == "unavailable"
    assert result["records"] == [] and result["claims_version"] is None
    assert "secret" not in str(result) and "confidential" not in str(result)


@pytest.mark.parametrize("args", [
    {"nc_ids": ["false_solutions"]}, {"nc_ids": ["SC_1"]}, {"sc_ids": ["NC_1"]},
    {"nc_ids": ["NC_0"]}, {"nc_ids": ["NC_01"]}, {"nc_ids": [1]},
    {"nc_ids": "NC_1"}, {"sc_ids": ["SC_1 OR 1=1"]},
    {"nc_ids": ["NC_1"] * 21}, {"sc_ids": ["SC_1"] * 21},
    {"taxonomy": "latest"}, {"taxonomy": "A" * 64},
    {"review_state": "greenwashing"}, {"review_state": "verified"},
    {"offset": -1}, {"offset": True}, {"offset": 100001},
    {"limit": 0}, {"limit": 21}, {"limit": True}, {"limit": "5"},
    {"sql": "SELECT secret"}, {"source_path": "C:/private"},
    {"filters": {"claims": ["NC_1"]}},
])
def test_claims_tool_strict_inputs_fail_before_any_source_read(args):
    catalog, db, store = claims_catalog()
    assert catalog.call("get_claims_matches", args)["status"] == "invalid_request"
    assert store.calls == db.reads == []


def test_claims_tool_narrows_every_trusted_filter_without_reinterpreting_legacy_labels():
    original = Filters(publishers=["The Washington Post"], sponsors=["exxonmobil"],
                       record_ids=["r1", "r2"], date_from=date(2020, 1, 1),
                       date_to=date(2020, 12, 31), include_unknown_dates=False)
    catalog, _, store = claims_catalog(original)
    result = catalog.call("get_claims_matches", {
        "filters": {"dataset": "all", "publishers": [], "record_ids": ["r1"],
                    "date_from": "2010-01-01", "date_to": "2030-01-01", "include_unknown_dates": True},
        "nc_ids": ["NC_1"], "sc_ids": ["SC_2"], "taxonomy": "e" * 64,
        "review_state": "human_supported", "offset": 0, "limit": 1,
    })
    assert result["status"] == "ok"
    narrowed, params = store.calls[0]
    assert narrowed == original.model_copy(update={"record_ids": ["r1"]})
    assert params == {"nc_ids": ["NC_1"], "sc_ids": ["SC_2"], "taxonomy": "e" * 64,
                      "review_state": "human_supported", "offset": 0, "limit": 1}
    assert not narrowed.labels


@pytest.mark.parametrize("filters", [{"dataset": "social"}, {"publishers": ["The New York Times"]},
                                    {"record_ids": ["r3"]}, {"date_from": "2030-01-01"}])
def test_claims_tool_disjoint_scope_is_clarified_before_read(filters):
    catalog, _, store = claims_catalog(Filters(publishers=["The Washington Post"],
                                              record_ids=["r1"], date_to=date(2020, 12, 31)))
    assert catalog.call("get_claims_matches", {"filters": filters})["status"] == "clarify"
    assert not store.calls


def test_claims_tool_historical_label_filter_remains_an_independent_source_filter():
    catalog, db, store = claims_catalog()
    facets = db.facets
    db.facets = lambda dataset: {**facets(dataset), "labels": ["viable_solutions"]}
    result = catalog.call("get_claims_matches", {"filters": {"labels": ["viable_solutions"]},
                                                "nc_ids": ["NC_1"]})
    assert result["status"] == "ok"
    assert store.calls[0][0].labels == ["viable_solutions"]
    assert store.calls[0][1]["nc_ids"] == ["NC_1"]


def test_claims_tool_empty_positive_set_does_not_make_unmatched_records_negative():
    empty = {**published_result(), "total_records": 0, "total_matches": 0, "records": []}
    catalog, _, _ = claims_catalog(result=empty)
    result = catalog.call("get_claims_matches", {})
    assert result["status"] == "ok" and result["available"]
    assert result["records"] == [] and "negative" in result["coverage"]
    assert "greenwashing" not in result["meaning"]


def test_claims_tool_omits_optional_category_aggregate_without_truncating_evidence():
    stored = published_result()
    stored["category_counts"] = [{"nc_id": "NC_1", "definition": "long definition " * 20000}]
    catalog, _, store = claims_catalog(result=stored)
    result = catalog.call("get_claims_matches", {})
    assert "category_counts" not in result
    assert store.calls[0][1]["limit"] == 5
    assert result["records"][0]["claims"][0]["quote"] == QUOTE
    assert "category_counts" in store.result  # Direct UI reads retain the aggregate.
    catalog.call("get_claims_matches", {"limit": 20})
    assert store.calls[1][1]["limit"] == 20


def test_claims_model_route_keeps_store_result_and_independent_version_trace():
    catalog, _, _ = claims_catalog()
    agent, _, _, calls = harness([response("get_claims_matches", {"nc_ids": ["NC_1"]})], catalog=catalog)
    run = agent.run("Show the published NC_1 assignments", Filters(), "synthetic-visitor")
    assert run.route == "claims"
    assert run.result["records"][0]["claims"][0]["quote"] == QUOTE
    assert run.tool_trace[0]["claims_version"] == VERSION
    assert run.tool_trace[0]["data_refs"][0]["body_hash"] == "c" * 64
    policy = calls[0]["input"][0]["content"]
    assert "Do not guess a category ID" in policy
    assert "unmatched record" in policy
    assert len(calls) == 1  # Synthetic routing fixture, no downstream generation.


def test_claims_route_unavailable_does_not_fall_back_to_unfiltered_statistics():
    agent, _, catalog, _ = harness([response("get_claims_matches", {})], catalog=FakeCatalog({
        "get_claims_matches": {"status": "unavailable", "available": False},
    }))
    run = agent.run("Which records have published claims?", Filters(), "synthetic-visitor")
    assert run.route == "unavailable" and run.failure_reason == "research_tool_unavailable"
    assert [name for name, _ in catalog.called] == ["get_claims_matches"]


@pytest.mark.parametrize("total,matches", [(1, 2), (0, 0)])
def test_claims_service_answer_is_deterministic_positive_only_and_uses_no_rag(total, matches):
    result = published_result()
    result.update(status="ok", total_records=total, total_matches=matches)
    run = ResearchRun(route="claims", result=result, cost_usd=0.001)
    service, db, agent = setup_service(run)
    answer = service.answer("Show published CLAIMS2 assignments", Filters(), "synthetic-visitor")
    assert answer.status == "answered" and answer.answer_mode == "tools"
    assert answer.structured_result == {"kind": "claims", **result}
    assert "not independent fact checking or verified greenwashing findings" in answer.answer
    assert "Unmatched records are not classified negatives" in answer.answer
    assert not db.searches and len(agent.calls) == len(db.saved) == 1
    assert answer.cost_usd == 0.001


def test_claims_service_cannot_publish_missing_results_as_success():
    answer = Service._tool_claims_answer({"status": "ok", "available": False})
    assert answer.status == "service_unavailable" and answer.structured_result is None
    assert "does not establish" in answer.answer


def test_service_default_store_is_lazy_and_bound_to_its_existing_database(monkeypatch):
    store = Store()
    db = Database()
    seen = []

    def construct(selected_db):
        seen.append(selected_db)
        return store

    monkeypatch.setattr("observatory.claims_store.ClaimsStore", construct)
    service = Service(Settings(), db=db, rag=SimpleNamespace())
    assert seen == []
    service.claims_matches(Filters())
    assert seen == [db]
