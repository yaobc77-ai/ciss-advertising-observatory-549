"""Fresh fictional compiled-intent executor checks; no evaluation fixtures."""

from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest

from observatory.intent_execution import execute_intent
from observatory.models import Filters
from observatory.question_intent import CompiledIntent, CompiledTask
from observatory.research_agent import MAX_RESULT_BYTES, ResearchRun
from observatory.research_tools import ToolCatalog

_BINDING = {"record_id": "fiction:cedar1", "dataset": "native", "version_id": "fiction-version-1",
            "body_hash": "b" * 64}


class FictionService:
    def facets(self, dataset):
        return {"sponsors": ["Aurora Loom", "Cedar Current"], "publishers": ["Fable Ledger"],
                "platforms": ["fiction-wire"], "accounts": ["Fiction Channel"], "labels": []}


class FixtureCatalog:
    def __init__(self, base, responses):
        self.scope = ToolCatalog(FictionService(), base)
        self.responses = list(responses)
        self.calls = []

    @property
    def base_filters(self):
        return self.scope.base_filters

    def narrow(self, requested, *, base=None):
        return self.scope.narrow(requested, base=base)

    def call(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


def envelope(base, **payload):
    return {"status": "ok", "filters": base.model_dump(mode="json"),
            "data_version": "fiction-data-1", "statistics_version": "fiction-stats-1", **payload}


def counts(base, totals=None, **extra):
    totals = totals or [(base.dataset, 7)]
    return envelope(base, kind="count", method="database", group_by=None,
                    collections=[{"dataset": dataset, "total": total} for dataset, total in totals],
                    groups=[], records=[], **extra)


def task(route="statistics", part="Count fictional records", dataset="native", **arguments):
    names = {"statistics": "record_statistics", "evidence": "search_records",
             "metadata": "get_record_metadata", "record": "get_record", "sources": "get_record_sources"}
    return CompiledTask(route, part, dataset, names[route],
                        {"filters": {"dataset": dataset}, **arguments})


def perform(tasks, responses, base=None):
    base = base or Filters()
    run = ResearchRun("unavailable", original_question="Fictional independent task",
                      base_filters=base.model_dump(mode="json"), cost_usd=0.123,
                      model_calls=[{"stage": "fiction-interpretation"}])
    catalog = FixtureCatalog(base, responses)
    return execute_intent(run, CompiledIntent("ready", tuple(tasks)), catalog), catalog


def title_task(part="Read Cedar Lantern's fields", route="metadata"):
    options = {"fields": ["publisher", "disclosure_language"]} if route == "metadata" else {}
    return CompiledTask(route, part, "native", {"metadata": "get_record_metadata",
                        "record": "get_record", "sources": "get_record_sources"}[route],
                        {"filters": {"dataset": "native"}, **options},
                        {"title": "Cedar Lantern", "filters": {"dataset": "native"}})


def title_result(base, **extra):
    return envelope(base, records=[dict(_BINDING)], total_candidates=1,
                    selection_required=False, truncated=False, **extra)


def metadata_result(base):
    return envelope(base, record=dict(_BINDING), original_fields={
        "publisher": {"status": "recorded", "value": "Fable Ledger"},
        "disclosure_language": {"status": "not_recorded", "value": None},
    })


def test_zero_counts_are_valid_and_execution_uses_no_new_model_call():
    base = Filters()
    result, catalog = perform([task()], [counts(base, [("native", 0)])], base)
    assert result.route == "statistics" and result.result["collections"][0]["total"] == 0
    assert result.cost_usd == 0.123 and result.model_calls == [{"stage": "fiction-interpretation"}]
    assert len(catalog.calls) == len(result.tool_trace) == 1
    assert result.tool_trace[0]["result_sha256"]


def test_combined_collections_keep_separate_native_and_social_units():
    base = Filters(dataset="all")
    result, _ = perform([task(dataset="all")], [counts(base, [("native", 3), ("social", 11)])], base)
    assert result.route == "statistics"
    assert result.result["collections"] == [{"dataset": "native", "total": 3}, {"dataset": "social", "total": 11}]


@pytest.mark.parametrize("key", tuple(Filters.model_fields))
def test_missing_serialized_filter_keys_are_rejected(key):
    base = Filters()
    payload = counts(base)
    payload["filters"].pop(key)
    result, _ = perform([task()], [payload], base)
    assert result.failure_reason == "research_result_scope_mismatch"
    assert result.route == "unavailable"


@pytest.mark.parametrize("field,value", [
    ("include_unknown_dates", 1), ("include_inferred_dates", 0), ("labels", None),
    ("unexpected", "SELECT fictional FROM forbidden"), ("date_from", "2034-01-01"),
    ("sponsors", ["Aurora Loom"]), ("dataset", "all"),
])
def test_scope_mutation_or_coercion_is_rejected(field, value):
    base = Filters()
    payload = counts(base)
    payload["filters"][field] = value
    result, _ = perform([task()], [payload], base)
    assert result.failure_reason == "research_result_scope_mismatch"


@pytest.mark.parametrize("rows", [
    [{"dataset": "native", "total": True}], [{"dataset": "native", "total": -1}],
    [{"dataset": "native", "total": 3.0}], [{"dataset": "social", "total": 3}],
    [{"dataset": "native", "total": 3}, {"dataset": "native", "total": 3}], [],
    [{"dataset": "native", "total": 3, "retrievable": 4}],
])
def test_invalid_counts_cannot_reach_the_statistics_renderer(rows):
    base = Filters()
    payload = counts(base)
    payload["collections"] = rows
    result, _ = perform([task()], [payload], base)
    assert result.failure_reason == "research_tool_invalid_result"


@pytest.mark.parametrize("field,value", [
    ("data_version", None), ("data_version", "unavailable"), ("statistics_version", None),
    ("statistics_version", True), ("failure_reason", "fiction-read-failed"),
    ("reason", "fiction-source-missing"),
])
def test_success_with_failed_or_missing_version_markers_is_rejected(field, value):
    base = Filters()
    payload = counts(base)
    payload[field] = value
    result, _ = perform([task()], [payload], base)
    assert result.route == "unavailable"
    assert result.failure_reason in {"research_record_version_changed", "research_tool_invalid_result"}


def test_second_task_outage_preserves_first_result_and_pending_task():
    base = Filters()
    first = task(part="Count Cedar Current records")
    second = task(part="Count Aurora Loom records")
    result, catalog = perform([first, second], [counts(base), RuntimeError("fiction failure")], base)
    assert len(catalog.calls) == 2 and result.route == "composite"
    assert result.result["complete"] is False
    assert result.result["failure_status"] == "service_unavailable"
    assert result.result["parts"][0]["question_part"] == first.question_part
    assert result.result["pending_parts"][0]["question_part"] == second.question_part
    assert result.failure_reason == "research_tool_unavailable"


@pytest.mark.parametrize("version", ["data_version", "statistics_version"])
def test_versions_cannot_change_between_independent_tasks(version):
    base = Filters()
    second = counts(base)
    second[version] += "-changed"
    result, _ = perform([task(part="Count Cedar records"), task(part="Count Aurora records")],
                        [counts(base), second], base)
    assert result.route == "composite" and len(result.result["parts"]) == 1
    assert result.failure_reason == "research_record_version_changed"
    assert result.result["failure_status"] == "service_unavailable"


def test_scope_conflict_stops_before_read_and_is_a_clarification():
    base = Filters(sponsors=["Cedar Current"])
    requested = task(filters={"dataset": "native", "sponsors": ["Aurora Loom"]})
    result, catalog = perform([requested], [], base)
    assert result.route == "clarify" and catalog.calls == []
    assert result.result["message"]


def test_title_read_preserves_record_binding_and_partial_metadata_fields():
    base = Filters()
    result, catalog = perform([title_task()], [title_result(base), metadata_result(base)], base)
    assert result.route == "metadata"
    assert catalog.calls[1][1]["record_id"] == "fiction:cedar1"
    assert catalog.calls[1][1]["fields"] == ["publisher", "disclosure_language"]
    assert result.result["original_fields"]["publisher"]["value"] == "Fable Ledger"
    assert result.result["original_fields"]["disclosure_language"]["status"] == "not_recorded"


@pytest.mark.parametrize("status", ["ambiguous", "not_found"])
def test_unresolved_titles_stop_without_guessing_an_id(status):
    base = Filters()
    payload = title_result(base)
    payload["status"] = status
    payload["message"] = "Select a fictional stored title candidate."
    result, catalog = perform([title_task()], [payload], base)
    assert result.route == "clarify" and result.failure_reason == "original_source_record_unresolved"
    assert len(catalog.calls) == 1


@pytest.mark.parametrize("change", ["version_id", "body_hash", "record_id", "dataset"])
def test_title_selection_cannot_read_a_changed_record_binding(change):
    base = Filters()
    payload = metadata_result(base)
    payload["record"][change] = "social" if change == "dataset" else "fiction-changed"
    result, _ = perform([title_task()], [title_result(base), payload], base)
    assert result.route == "unavailable" and result.failure_reason == "research_record_version_changed"


def test_boolean_title_count_is_not_a_unique_candidate():
    base = Filters()
    payload = title_result(base)
    payload["total_candidates"] = True
    result, catalog = perform([title_task()], [payload], base)
    assert result.failure_reason == "research_title_binding_invalid" and len(catalog.calls) == 1


def test_missing_requested_metadata_field_is_rejected():
    base = Filters()
    payload = metadata_result(base)
    payload["original_fields"].pop("disclosure_language")
    result, _ = perform([title_task()], [title_result(base), payload], base)
    assert result.failure_reason == "research_tool_invalid_result"


def test_three_title_tasks_take_six_deterministic_reads():
    base = Filters()
    tasks = [title_task(part=f"Read fictional metadata part {index}") for index in range(3)]
    responses = [payload for _ in tasks for payload in (title_result(base), metadata_result(base))]
    result, catalog = perform(tasks, responses, base)
    assert result.route == "composite" and result.result["complete"] is True, (result.failure_reason, result.tool_trace)
    assert len(result.result["parts"]) == 3 and len(catalog.calls) == 6
    assert result.cost_usd == 0.123 and len(result.model_calls) == 1


def test_single_evidence_keeps_compiled_query_and_original_scope():
    base = Filters()
    payload = envelope(base, evidence=[], source_refs=[], search_status="empty")
    result, catalog = perform([task("evidence", query="Cedar fictional poem")], [payload], base)
    assert result.route == "evidence" and result.result["search_query"] == "Cedar fictional poem"
    assert catalog.calls[0][1]["query"] == "Cedar fictional poem"


@pytest.mark.parametrize("payload", [None, [], {"status": "ok", "value": float("nan")},
                                    {"status": "ok", "value": "x" * (MAX_RESULT_BYTES + 1)}])
def test_malformed_or_unbounded_results_fail_closed(payload):
    result, _ = perform([task()], [payload])
    assert result.route == "unavailable" and result.failure_reason == "research_tool_invalid_result"


def test_compiled_tasks_cannot_select_an_unapproved_tool_or_mixed_evidence():
    wrong = CompiledTask("statistics", "Count fiction", "native", "execute_sql", {})
    result, catalog = perform([wrong], [])
    assert result.failure_reason == "research_tool_configuration" and catalog.calls == []
    result, catalog = perform([task(), task("evidence", query="fiction")], [])
    assert result.failure_reason == "research_tool_configuration" and catalog.calls == []


def share_fixture(base):
    numerator = base.model_copy(update={"sponsors": ["Aurora Loom"]})
    payload = envelope(numerator, kind="share", method="database", group_by=None,
                       denominator_filters=base.model_dump(mode="json"),
                       denominator_basis="current_selection_before_question_targets",
                       collections=[{"dataset": "native", "total": 2, "numerator": 2,
                                     "denominator": 8, "percentage": 25.0, "percentage_status": "defined"}],
                       groups=[], records=[])
    requested = task(measure="share", filters={"dataset": "native", "sponsors": ["Aurora Loom"]})
    return requested, payload


def test_share_uses_the_trusted_active_denominator():
    base = Filters(date_from=date(2031, 1, 1), date_to=date(2032, 12, 31))
    requested, payload = share_fixture(base)
    result, _ = perform([requested], [payload], base)
    assert result.route == "statistics"
    assert result.result["denominator_filters"] == base.model_dump(mode="json")


@pytest.mark.parametrize("change", ["scope", "basis", "arithmetic"])
def test_share_denominator_and_arithmetic_cannot_drift(change):
    base = Filters()
    requested, payload = share_fixture(base)
    if change == "scope":
        payload["denominator_filters"]["sponsors"] = ["Aurora Loom"]
    elif change == "basis":
        payload["denominator_basis"] = "question_comparison_group"
    else:
        payload["collections"][0]["percentage"] = 100.0
    result, _ = perform([requested], [payload], base)
    assert result.failure_reason == "research_tool_invalid_result"


def test_year_distribution_keeps_every_tied_highest_year():
    base = Filters()
    payload = envelope(base, kind="top_years", method="database", group_by="years", ranking="highest",
                       collections=[{"dataset": "native", "total": 8}],
                       groups=[{"dataset": "native", "name": "2031", "count": 4},
                               {"dataset": "native", "name": "2032", "count": 4}], records=[])
    result, _ = perform([task(group_by="years", ranking="highest")], [payload], base)
    assert result.route == "statistics" and len(result.result["groups"]) == 2


def test_a_year_distribution_cannot_be_replaced_by_a_total():
    base = Filters()
    result, _ = perform([task(group_by="years")], [counts(base)], base)
    assert result.failure_reason == "research_tool_invalid_result"


def test_period_comparison_checks_each_scope_and_collection_count():
    base = Filters(dataset="all")
    periods = [{"label": "earlier", "date_from": "2031-01-01", "date_to": "2031-12-31"},
               {"label": "later", "date_from": "2032-01-01", "date_to": "2032-12-31"}]
    payload = envelope(base, kind="compare_periods", method="database", group_by=None, ranking="all",
                       collections=[{"dataset": "native", "total": 2}, {"dataset": "social", "total": 5}],
                       groups=[], records=[], periods=[{
                           "label": period["label"],
                           "filters": {**base.model_dump(mode="json"), "date_from": period["date_from"],
                                       "date_to": period["date_to"], "include_unknown_dates": False},
                           "collections": [{"dataset": "native", "total": 1}, {"dataset": "social", "total": 2}],
                       } for period in periods])
    requested = task(dataset="all", periods=periods)
    result, _ = perform([requested], [payload], base)
    assert result.route == "statistics" and len(result.result["periods"]) == 2
    payload["periods"][1]["filters"]["date_from"] = "2031-01-01"
    result, _ = perform([requested], [payload], base)
    assert result.failure_reason == "research_tool_invalid_result"


def test_real_catalog_count_and_title_metadata_contract_without_a_database():
    base = Filters()

    class FixtureDatabase:
        def find_records(self, filters, title, limit):
            return {"rows": [dict(_BINDING)], "total_candidates": 1, "match_type": "exact"}

        def original_record_metadata(self, filters, record_id):
            return {**_BINDING, "metadata_origins": []}

    class ExistingCatalogService(FictionService):
        settings = SimpleNamespace(show_source_links=False)
        db = FixtureDatabase()

        def health(self):
            return {"status": "ok", "data_version": "fiction-data-1", "statistics_version": "fiction-stats-1",
                    "countable_record_counts": {"native": 7}}

        def _statistics_answer(self, plan):
            return SimpleNamespace(structured_result=counts(plan.filters))

        def _public_rows(self, rows):
            return deepcopy(rows)

    catalog = ToolCatalog(ExistingCatalogService(), base)
    compiled = CompiledIntent("ready", (task(part="Count fictional records"), title_task()))
    run = ResearchRun("unavailable", base_filters=base.model_dump(mode="json"))
    result = execute_intent(run, compiled, catalog)
    assert result.route == "composite" and result.result["complete"] is True, (result.failure_reason, result.tool_trace)
    assert len(result.tool_trace) == 3
    metadata = result.result["parts"][1]["result"]["original_fields"]
    assert set(metadata) == {"publisher", "disclosure_language"}
    assert all(field["status"] == "not_recorded" for field in metadata.values())


def test_unknown_explicit_social_count_unit_is_rejected():
    base = Filters(dataset="social")
    payload = counts(base)
    payload["collections"][0]["count_unit"] = "verified_paid_ads"
    result, _ = perform([task(dataset="social")], [payload], base)
    assert result.failure_reason == "research_tool_invalid_result"


def test_record_result_with_invalid_record_shape_fails_without_crashing():
    base = Filters()
    payload = envelope(base, record="fiction-invalid-record")
    result, _ = perform([task("record", record_id="fiction:cedar1")], [payload], base)
    assert result.failure_reason == "research_record_binding_mismatch"
