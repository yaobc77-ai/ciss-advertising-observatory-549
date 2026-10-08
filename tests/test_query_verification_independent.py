"""Fresh independent P50 probes; no customer inputs, model calls or DB writes."""

import os
import runpy
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory.db import Database
from observatory.models import Filters
from observatory.original_metadata import metadata_origins_sql, project_record_metadata
from observatory.question_policy import statistics_request_preserves_question
from observatory.research_tools import FiltersRequest, ScopeConflict, ToolCatalog
from observatory.structured_queries import canonical_source_values

COMPANY = "Juniper Lantern Cooperative"
OUTLET = "Beacon Marsh Gazette"
CONTEXT = {"sponsors": [COMPANY], "publishers": [OUTLET], "accounts": []}


@pytest.mark.parametrize("words,bounds,expected", [
    ("in 2052", ("2052-01-01", "2052-12-31"), True),
    ("in 2052", (None, None), False),
    ("in 2052", ("2052-03-01", "2052-12-31"), False),
    ("after 2052", ("2053-01-01", None), True),
    ("after 2052", ("2052-01-01", None), False),
    ("before 2052", (None, "2051-12-31"), True),
    ("before 2052", (None, "2052-12-31"), False),
    ("since 2052", ("2052-01-01", None), True),
    ("between 2052 and 2053", ("2052-01-01", "2053-12-31"), True),
    ("in 2052-02-30", ("2052-02-30", "2052-02-30"), False),
    ("in August 2052", ("2052-01-01", "2052-12-31"), False),
])
def test_fresh_literal_date_guard(words, bounds, expected):
    args = {"filters": {"sponsors": [COMPANY], "date_from": bounds[0], "date_to": bounds[1]}}
    assert statistics_request_preserves_question(
        f"How many native ads sponsored by {COMPANY} {words}?", CONTEXT, args,
        {"dataset": "native"}) is expected


@pytest.mark.parametrize("words,ranges,expected", [
    ("2052 versus 2053", [("2052-01-01", "2052-12-31"), ("2053-01-01", "2053-12-31")], True),
    ("2052 versus 2053", [("2052-01-01", "2052-12-31"), ("2054-01-01", "2054-12-31")], False),
    ("2052 versus 2053", [("2052-01-01", "2053-12-31")], False),
    ("August 2052 versus September 2053", [("2052-01-01", "2052-12-31"), ("2053-01-01", "2053-12-31")], False),
    ("Q3 2052 versus Q4 2053", [("2052-01-01", "2052-12-31"), ("2053-01-01", "2053-12-31")], False),
    ("2052-03-04 versus 2053-05-06", [("2052-03-04", "2052-03-04"), ("2053-05-06", "2053-05-06")], True),
])
def test_fresh_comparison_guard(words, ranges, expected):
    args = {"filters": {"sponsors": [COMPANY]}, "periods": [
        {"label": str(i), "date_from": a, "date_to": b} for i, (a, b) in enumerate(ranges)]}
    assert statistics_request_preserves_question(
        f"Compare native ads sponsored by {COMPANY}: {words}.", CONTEXT, args,
        {"dataset": "native"}) is expected


def test_year_grouping_cannot_be_replaced_by_one_total():
    question = f"Which year has the most native ads sponsored by {COMPANY}?"
    assert statistics_request_preserves_question(question, CONTEXT,
        {"filters": {"sponsors": [COMPANY]}, "group_by": "years", "ranking": "highest"},
        {"dataset": "native"})
    assert not statistics_request_preserves_question(question, CONTEXT,
        {"filters": {"sponsors": [COMPANY]}, "group_by": "none"}, {"dataset": "native"})


class FakeDB:
    def __init__(self):
        self.calls = []
        self.title_rows = []
        self.total = 0

    def find_records(self, filters, title, limit):
        self.calls.append(("find_records", filters.model_copy(deep=True), title, limit))
        return {"rows": self.title_rows, "total_candidates": self.total, "match_type": "exact"}

    def research_statistics(self, filters, *, group_by, ranking, periods):
        self.calls.append(("research_statistics", filters.model_copy(deep=True), periods))
        names = ("native", "social") if filters.dataset == "all" else (filters.dataset,)
        collections = [{"dataset": name, "total": 1, "unknown_dates": 0} for name in names]
        return {"collections": collections, "groups": [], "records": [], "periods": [
            {"label": item["label"], "collections": collections} for item in periods]}


class FakeService:
    def __init__(self):
        self.db = FakeDB()
        self.settings = SimpleNamespace(show_source_links=True)
        self.by_dataset = {
            "native": {"sponsors": [COMPANY, "CAPP", "Williams", "Williams Companies"],
                       "publishers": [OUTLET], "accounts": [], "platforms": [], "keywords": [], "labels": []},
            "social": {"sponsors": [COMPANY.lower(), "CAPP", "Canadian Association of Petroleum Producers"],
                       "publishers": [], "accounts": ["Lantern Channel"], "platforms": [], "keywords": [], "labels": []},
        }

    def facets(self, dataset):
        if dataset != "all":
            return self.by_dataset[dataset]
        return {key: list(dict.fromkeys(self.by_dataset["native"][key] + self.by_dataset["social"][key]))
                for key in self.by_dataset["native"]}

    def health(self):
        return {"status": "ok", "record_counts": {"native": 1, "social": 1},
                "data_version": "fresh-fixture-v1", "statistics_version": "fresh-stats-v1"}

    @staticmethod
    def _public_rows(rows):
        return rows


def test_cross_collection_guard_needs_union_and_both_exact_company_spellings():
    tool = ToolCatalog(FakeService(), Filters(dataset="all"))
    context = tool.statistics_validation_context()
    question = f"How many native ads and social posts belong to {COMPANY}?"
    assert statistics_request_preserves_question(question, context,
        {"filters": {"dataset": "all", "sponsors": [COMPANY]}}, {"dataset": "all"})
    assert not statistics_request_preserves_question(question, context,
        {"filters": {"dataset": "native", "sponsors": [COMPANY]}}, {"dataset": "all"})
    selected = tool.narrow(FiltersRequest(sponsors=[COMPANY]))
    assert set(selected.sponsors) == {COMPANY, COMPANY.lower()}
    assert selected.dataset == "all"


@pytest.mark.parametrize("base,requested", [
    (Filters(dataset="native"), FiltersRequest(dataset="social")),
    (Filters(dataset="all", sponsors=[COMPANY]), FiltersRequest(sponsors=["CAPP"])),
    (Filters(dataset="all", record_ids=["native:fresh-one"]), FiltersRequest(record_ids=["native:fresh-two"])),
    (Filters(dataset="native", date_from=date(2052, 1, 1), date_to=date(2052, 12, 31)),
     FiltersRequest(date_from=date(2053, 1, 1))),
    (Filters(dataset="native", include_unknown_dates=False), FiltersRequest(date_presence="missing")),
    (Filters(dataset="native"), FiltersRequest(include_inferred_dates=True)),
    (Filters(dataset="all"), FiltersRequest(accounts=["Lantern Channel"])),
])
def test_disjoint_or_expanded_trusted_scope_rejected(base, requested):
    with pytest.raises(ScopeConflict):
        ToolCatalog(FakeService(), base).narrow(requested)


def test_all_does_not_expand_trusted_native_and_unknown_dates_remain_excluded():
    tool = ToolCatalog(FakeService(), Filters(dataset="native", include_unknown_dates=False,
        date_from=date(2052, 4, 1), date_to=date(2052, 9, 30)))
    result = tool.narrow(FiltersRequest(dataset="all", date_from=date(2052, 1, 1),
        date_to=date(2052, 12, 31), include_unknown_dates=True))
    assert result.dataset == "native"
    assert (result.date_from, result.date_to) == (date(2052, 4, 1), date(2052, 9, 30))
    assert result.include_unknown_dates is False


def test_period_tool_executes_two_separate_intersected_scopes():
    service = FakeService()
    tool = ToolCatalog(service, Filters(dataset="all", date_from=date(2052, 4, 1),
        date_to=date(2053, 9, 30)))
    result = tool.call("record_statistics", {"filters": {"sponsors": [COMPANY]}, "periods": [
        {"label": "a", "date_from": "2052-01-01", "date_to": "2052-12-31"},
        {"label": "b", "date_from": "2053-01-01", "date_to": "2053-12-31"}]})
    assert result["status"] == "ok" and result["kind"] == "compare_periods"
    call, outer, periods = service.db.calls[0]
    assert call == "research_statistics" and outer.dataset == "all" and len(periods) == 2
    assert (periods[0]["filters"].date_from, periods[1]["filters"].date_to) == (
        date(2052, 4, 1), date(2053, 9, 30))
    assert all(not item["filters"].include_unknown_dates for item in periods)


def test_capp_acronym_is_not_an_automatic_full_name_identity_merge():
    tool = ToolCatalog(FakeService(), Filters(dataset="all"))
    result = tool.call("resolve_entity", {"entity_type": "sponsor", "query": "CAPP"})
    assert result["status"] == "ok" and result["candidate_count"] == 2
    assert {item["source_value"] for item in result["candidates"]} == {"CAPP"}
    assert {item["dataset"] for item in result["candidates"]} == {"native", "social"}
    assert len({item["entity_id"] for item in result["candidates"]}) == 2
    assert all(item["identity_status"] == "source_candidate_not_resolved" for item in result["candidates"])


def test_williams_short_name_remains_ambiguous_until_exact_user_selection():
    service = FakeService()
    result = ToolCatalog(service, Filters(dataset="native")).call("resolve_entity",
        {"entity_type": "sponsor", "query": "Williams"})
    assert result["status"] == "clarify" and result["candidate_count"] == 2
    with pytest.raises(ScopeConflict):
        ToolCatalog(service, Filters(dataset="native")).narrow(FiltersRequest(sponsors=["Williams"]))
    scoped = ToolCatalog(service, Filters(dataset="native", sponsors=["Williams"]))
    assert scoped.narrow(FiltersRequest(sponsors=["Williams"])).sponsors == ["Williams"]


def test_exact_capp_source_alias_is_lookup_only_and_namespace_bound():
    source = "Canadian Association of Petroleum Producers (CAPP)"
    assert canonical_source_values("CAPP", "sponsors", [source]) == [source]
    assert canonical_source_values("CAPP", "accounts", [source]) == []
    assert canonical_source_values("CAPP", "publishers", [source]) == []
    assert canonical_source_values("CAPP", "sponsors", ["Canadian Association of Petroleum Producers"]) == []
    service = FakeService()
    service.by_dataset["social"]["sponsors"] = [source]
    service.by_dataset["social"]["accounts"] = ["CAPP Oil Gas Canada"]
    scoped = ToolCatalog(service, Filters(dataset="social"))
    result = scoped.call("resolve_entity", {"entity_type": "sponsor", "query": "CAPP"})
    assert result["status"] == "ok" and result["candidates"][0]["source_value"] == source
    narrowed = scoped.narrow(FiltersRequest(sponsors=["CAPP"]))
    assert narrowed.sponsors == [source] and len(scoped.alias_resolutions) == 1
    # The literal account fragment is a candidate lookup, not a filter alias.
    result = scoped.call("resolve_entity", {"entity_type": "account", "query": "CAPP"})
    assert result["candidates"][0]["source_value"] == "CAPP Oil Gas Canada"
    assert result["candidates"][0]["match"] == "partial"
    result = scoped.call("find_records", {"title": "fresh", "filters": {"accounts": ["CAPP"]}})
    assert result["status"] == "clarify"


def test_capp_alias_cannot_choose_between_different_exact_source_categories():
    service = FakeService()
    service.by_dataset["social"]["sponsors"] = ["Canadian Association of Petroleum Producers (CAPP)"]
    tool = ToolCatalog(service, Filters(dataset="all"))
    result = tool.call("resolve_entity", {"entity_type": "sponsor", "query": "CAPP"})
    assert result["status"] == "clarify" and result["candidate_count"] == 2


def test_description_alias_is_in_bounded_sql_projection():
    sql = metadata_origins_sql()
    assert "disclosure description" in sql and "disclosure location" in sql
    assert "jsonb_build_object('field','disclosure_location'" in sql


def test_description_alias_preserves_path_and_blank_alternate():
    item = metadata_row([("disclosure_location", "Above headline"), ("disclosure_location", "")])
    cells = item["metadata_origins"][0]["cells"]
    cells[0]["payload_field_path"] = ["raw", "metadata", "disclosure description"]
    cells[1]["payload_field_path"] = ["raw", "metadata", "disclosure location"]
    result = project_record_metadata(item, ["disclosure_location"], public_url=lambda u: u)
    field = result["original_fields"]["disclosure_location"]
    assert field["status"] == "recorded" and field["value"] == "Above headline"
    assert field["payload_field_path"][-1] == "disclosure description"
    assert any(ref["payload_field_path"][-1] == "disclosure description" for ref in result["source_refs"])


def row(record_id="native:fresh-one", dataset="native"):
    return {"record_id": record_id, "dataset": dataset, "version_id": "v-fresh-one",
            "body_hash": "a" * 64, "title": "Lantern Plan 17%_A", "url": "",
            "retrievable": True}


@pytest.mark.parametrize("total,expected", [(0, "not_found"), (1, "ok"), (2, "ambiguous")])
def test_title_candidate_selection_is_explicit_and_keeps_binding(total, expected):
    service = FakeService()
    service.db.total = total
    service.db.title_rows = [row(f"native:fresh-{i}") for i in range(total)]
    result = ToolCatalog(service, Filters(dataset="native")).call("find_records", {"title": "Lantern Plan 17%_A"})
    assert result["status"] == expected
    assert result["selection_required"] is (total > 1)
    assert all(item["body_hash"] == "a" * 64 for item in result["records"])


def test_title_candidate_wrong_collection_rejected():
    service = FakeService()
    service.db.total, service.db.title_rows = 1, [row("social:fresh-one", "social")]
    assert ToolCatalog(service, Filters(dataset="native")).call(
        "find_records", {"title": "Lantern Plan 17%_A"})["status"] == "unavailable"


def test_title_sql_uses_literal_contains_and_parameterization():
    captured = {}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params):
            captured.update(sql=sql, params=params)
            return SimpleNamespace(fetchall=lambda: [])

    stub = SimpleNamespace(_public_query=lambda f: ("SELECT 1", ["trusted-filter"]), connect=Connection)
    Database.find_records(stub, Filters(dataset="native"), "  17%_A  ", 3)
    assert "strpos(" in captured["sql"] and "LIKE" not in captured["sql"]
    assert "17%_A" not in captured["sql"]
    assert captured["params"] == ["trusted-filter", "17%_A", "17%_A", "17%_A", "17%_A", 3]


def metadata_row(values):
    return {**row(), "metadata_origins": [{"origin_kind": "original_metadata", "priority": 0,
        "cells": [{"field": field, "present": True, "payload_field_path": ["raw", "metadata", field],
                   "value": value} for field, value in values],
        "provenance": [{"row": 19, "row_basis": "logical_record_including_header", "sha256": "b" * 64}]}]}


def test_known_wording_survives_unknown_location_with_original_binding():
    result = project_record_metadata(metadata_row([("disclosure_language", "Paid partnership"),
        ("disclosure_location", None)]), ["disclosure_language", "disclosure_location"], public_url=lambda u: u)
    fields = result["original_fields"]
    assert fields["disclosure_language"]["value"] == "Paid partnership"
    assert fields["disclosure_language"]["status"] == "recorded"
    assert fields["disclosure_location"]["status"] == "unknown"
    assert result["record"]["version_id"] == "v-fresh-one"
    assert len(result["source_refs"]) == 2 and result["online_truth"] == "not_established"


def test_conflicting_equal_priority_disclosure_locations_require_review():
    result = project_record_metadata(metadata_row([("disclosure_location", "above headline"),
        ("disclosure_location", "page footer")]), ["disclosure_location"], public_url=lambda u: u)
    assert result["original_fields"]["disclosure_location"]["status"] == "needs_review"
    assert result["original_fields"]["disclosure_location"]["value"] is None
    assert result["review_required"] is True


@pytest.mark.skipif(os.getenv("OBS_INDEPENDENT_READONLY_DB") != "1", reason="Explicit read-only source-cell audit opt-in")
def test_readonly_actual_metadata_and_p50_receipt_crosscheck(tmp_path):
    script = Path(__file__).resolve().parents[1] / ".runtime/query_verification_20261008/independent/verify_receipts_and_metadata.py"
    # Every run keeps an immutable new receipt; never overwrite the original.
    result = runpy.run_path(str(script))["verify"](output_directory=tmp_path)
    assert result["native_rows_projected"] == 275
    assert result["original_field_statuses"]["disclosure_language"]["recorded"] == 267
    assert result["original_field_statuses"]["disclosure_location"] == {
        "recorded": 267, "not_recorded": 7, "unknown": 1}
    assert result["recorded_disclosure_location_source_columns"] == {"disclosure description": 267}
