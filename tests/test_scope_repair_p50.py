"""Fresh fictional scope contracts; no database, provider or held-out catalog."""

import json

import pytest

from observatory.models import Filters
from observatory.question_policy import (
    question_contract,
    statistics_request_preserves_question,
)
from observatory.research_agent import ResearchAgent
from observatory.research_tools import FiltersRequest, ToolCatalog

CONTEXT = {
    "sponsors": [{"value": "Harbor Pavilion League"}, {"value": "Cinder Orchard Trust"}],
    "publishers": [{"value": "Copper Finch Review"}, {"value": "Silver Fen Gazette"}],
    "accounts": [],
}


def scope_ok(question, filters, *, base=None, context=CONTEXT, **operation):
    return statistics_request_preserves_question(
        question, context, {"measure": "count", "filters": filters, **operation},
        (base or Filters(dataset="all")).model_dump(mode="json"))


@pytest.mark.parametrize("wording,lower,upper", [
    ("from 2034 to 2036", "2034-01-01", "2036-12-31"),
    ("from 2034 through 2036", "2034-01-01", "2036-12-31"),
    ("between 2034 and 2036", "2034-01-01", "2036-12-31"),
    ("during 2034-2036", "2034-01-01", "2036-12-31"),
    ("from 2034-03-02 through 2036-04-08", "2034-03-02", "2036-04-08"),
    ("from 2034 through 2036-04-08", "2034-01-01", "2036-04-08"),
    ("in 2034", "2034-01-01", "2034-12-31"),
    ("in 2034-02-28", "2034-02-28", "2034-02-28"),
    ("since 2034", "2034-01-01", None),
    ("before 2036", None, "2035-12-31"),
    ("after 2034", "2035-01-01", None),
    ("before 2036-04-08", None, "2036-04-07"),
    ("after 2034-03-02", "2034-03-03", None),
    ("through 2036-04-08", None, "2036-04-08"),
])
def test_explicit_calendar_scope_survives(wording, lower, upper):
    question = f"Count native ads sponsored by Harbor Pavilion League {wording}."
    filters = {"dataset": "native", "sponsors": ["Harbor Pavilion League"],
               "date_from": lower, "date_to": upper}
    assert scope_ok(question, filters)
    for key in ("date_from", "date_to"):
        if filters[key]:
            assert not scope_ok(question, {**filters, key: None})
    assert not scope_ok(question, {**filters, "sponsors": ["Cinder Orchard Trust"]})


@pytest.mark.parametrize("question", [
    "Count native ads from 2034-02-30 through 2036-01-01.",
    "Count native ads from 2036 through 2034.",
    "Count native ads from 2034 through 2036 and during 2038.",
])
def test_invalid_or_uncovered_dates_do_not_get_a_convenient_subset(question):
    assert not scope_ok(question, {"dataset": "native", "date_from": "2034-01-01",
                                   "date_to": "2036-12-31"})


def test_unsupported_date_wording_requires_clarification_even_when_model_drops_dates():
    assert not scope_ok("Count native ads during April 2034.", {"dataset": "native"})


def test_explicit_scope_intersects_ui_dates_and_preserves_the_open_end():
    base = Filters(dataset="all", date_from="2035-04-02", date_to="2036-07-03")
    assert scope_ok("Count native ads from 2034 through 2037.",
                    {"dataset": "native", "date_from": "2034-01-01", "date_to": "2037-12-31"}, base=base)
    assert not scope_ok("Count native ads since 2034.",
                        {"dataset": "native", "date_from": "2034-01-01", "date_to": "2036-02-01"}, base=base)
    assert not scope_ok("Count native ads.", {"dataset": "native", "date_from": "2034-01-01"})


@pytest.mark.parametrize("base_dataset,requested,expected", [
    ("all", "native", True), ("all", "social", False), ("all", "all", False),
    ("all", None, False), ("native", "all", True), ("native", None, True),
    ("native", "social", False), ("social", "native", False),
])
def test_literal_collection_uses_ui_intersection(base_dataset, requested, expected):
    assert scope_ok("Count native ads.", {"dataset": requested},
                    base=Filters(dataset=base_dataset)) is expected


def test_period_comparison_retains_complete_periods_and_active_dates():
    base = Filters(dataset="all", date_from="2035-06-01", date_to="2036-10-01")
    arguments = {"filters": {"dataset": "native"}, "periods": [
        {"date_from": "2035-01-01", "date_to": "2035-12-31"},
        {"date_from": "2036-01-01", "date_to": "2036-12-31"},
    ]}
    assert scope_ok("Compare native ad counts in 2035 versus 2036.", base=base, **arguments)
    arguments["periods"][1]["date_to"] = "2036-07-01"
    assert not scope_ok("Compare native ad counts in 2035 versus 2036.", base=base, **arguments)


@pytest.mark.parametrize("wording", [
    "in May 2044 and June 2045", "in Jan. 2044 versus Feb. 2045",
    "in Q1 2044 versus Q2 2045", "in the first quarter of 2044 and the second quarter of 2045",
    "in 2044-05 versus 2045-06", "in 05/2044 versus 06/2045",
    "in 2044/05/02 versus 2045/06/03", "in 2044-5-2 versus 2045-6-3",
    "before 2044 and after 2045", "since 2044 versus until 2045",
    "from 2044 versus through 2045", "in 2044年5月 versus 2045年6月",
])
def test_unsupported_period_scope_cannot_be_replaced_by_whole_years(wording):
    assert not scope_ok(f"Compare native ad counts {wording}.", {"dataset": "native"}, periods=[
        {"date_from": "2044-01-01", "date_to": "2044-12-31"},
        {"date_from": "2045-01-01", "date_to": "2045-12-31"},
    ])


@pytest.mark.parametrize("wording,periods", [
    ("in 2044 versus 2045", [("2044-01-01", "2044-12-31"), ("2045-01-01", "2045-12-31")]),
    ("on 2044-05-02 versus 2045-06-03", [("2044-05-02", "2044-05-02"), ("2045-06-03", "2045-06-03")]),
    ("from 2044-05-02 to 2044-05-31 versus from 2045-06-03 through 2045-06-30",
     [("2044-05-02", "2044-05-31"), ("2045-06-03", "2045-06-30")]),
])
def test_supported_annual_and_full_iso_periods_still_preserve_scope(wording, periods):
    assert scope_ok(f"Compare native ad counts {wording}.", {"dataset": "native"}, periods=[
        {"date_from": lower, "date_to": upper} for lower, upper in periods
    ])


def test_source_controlled_acronym_and_display_alias_share_executor_rules(monkeypatch):
    from observatory import structured_queries

    display = {"HPL": "Harbor Pavilion League", "COT": "Cinder Orchard Trust"}
    monkeypatch.setattr(structured_queries, "sponsor_display", lambda value: display.get(value, value))
    context = {"sponsors": [{"value": "HPL", "display": display["HPL"]},
                            {"value": "COT", "display": display["COT"]}]}
    assert scope_ok("Count native ads sponsored by Harbor Pavilion League.",
                    {"dataset": "native", "sponsors": ["HPL"]}, context=context)
    assert scope_ok("Count native ads sponsored by HPL.",
                    {"dataset": "native", "sponsors": ["Harbor Pavilion League"]}, context=context)
    assert not scope_ok("Count native ads sponsored by Harbor Pavilion League.",
                        {"dataset": "native", "sponsors": ["COT"]}, context=context)
    display["HPL2"] = "Harbor Pavilion League"
    ambiguous = {"sponsors": [*context["sponsors"], {"value": "HPL2", "display": display["HPL2"]}]}
    assert not scope_ok("Count native ads sponsored by Harbor Pavilion League.",
                        {"dataset": "native", "sponsors": ["HPL"]}, context=ambiguous)


def test_entity_name_years_do_not_become_publication_dates():
    context = {"sponsors": [{"value": "Harbor Pavilion League (2034-2036)"}]}
    question = "Count native ads sponsored by Harbor Pavilion League (2034-2036)."
    filters = {"dataset": "native", "sponsors": [context["sponsors"][0]["value"]]}
    assert scope_ok(question, filters, context=context)
    assert not scope_ok(question, {**filters, "date_from": "2034-01-01", "date_to": "2036-12-31"}, context=context)


@pytest.mark.parametrize("question", [
    "What is the stored publisher and what is the stored date for the article titled 'Moss Lantern' ?",
    "For the article titled 'Moss Lantern', what is the stored publisher and what is the stored date?",
    "Count native ads that were from Copper Finch Review and which were published in 2034.",
    "Count native ads during 2034 and show breakdown by publisher.",
])
def test_one_read_constraints_and_fields_do_not_require_a_plan(question):
    assert not question_contract(question)["compound_read_request"]


@pytest.mark.parametrize("question", [
    "Count native ads and count social posts.",
    "Count native ads and which were from Copper Finch Review?",
    "What is the stored date for the article titled 'Moss Lantern' and what is the stored date for the article titled 'Birch Prism'?",
    "What is the stored publisher for the article titled 'Moss Lantern' and what is the stored date for a second article?",
    "What is the stored publisher for the article titled 'Moss Lantern' and what does its body say?",
    "Count native ads during 2034 and show breakdown by publisher and list their titles.",
])
def test_distinct_reads_keep_complete_plan_obligation(question):
    assert question_contract(question)["compound_read_request"]


class _Service:
    def facets(self, dataset):
        return {name: [item["value"] for item in items] for name, items in CONTEXT.items()}


class _Catalog:
    def __init__(self, base, override=None):
        self.executor = ToolCatalog(_Service(), base)
        self.override = override
        self.reads = []

    def definitions(self):
        return self.executor.definitions()

    def statistics_validation_context(self):
        return CONTEXT

    def call(self, name, arguments):
        self.reads.append((name, arguments))
        if self.override:
            return self.override(name, arguments)
        scope = self.executor.narrow(FiltersRequest.model_validate(arguments.get("filters") or {}))
        return {"status": "ok", "filters": scope.model_dump(mode="json"), "count": 4, "kind": "count"}


class _ScriptedAgent(ResearchAgent):
    def __init__(self, catalog, calls):
        super().__init__(None, catalog, entity_context=CONTEXT)
        self.calls = iter(calls)
        self.dispatched_tools = []

    def _dispatch(self, inputs, definitions, visitor, step):
        self.dispatched_tools.append({item["name"] for item in definitions})
        name, arguments = next(self.calls)
        return {"status": "completed", "output": [{"type": "function_call", "name": name,
                 "call_id": f"synthetic-{step}", "arguments": json.dumps(arguments)}]}, {"synthetic": True}, 0.0


def test_single_statistics_uses_active_dataset_when_model_requests_all():
    base = Filters(dataset="native")
    catalog = _Catalog(base)
    run = _ScriptedAgent(catalog, [("record_statistics", {"filters": {"dataset": "all"}, "measure": "count"})]).run(
        "Count native ads.", base, "fictional")
    assert run.route == "statistics" and not run.failure_reason
    assert run.result["filters"]["dataset"] == "native"
    assert len(catalog.reads) == 1


@pytest.mark.parametrize("mutation,reason", [
    ({"dataset": "social"}, "research_result_scope_mismatch"),
    ({"dataset": "all"}, "research_result_scope_mismatch"),
    ({"sponsors": []}, "statistics_result_scope_missing"),
    ({"sponsors": ["Cinder Orchard Trust"]}, "statistics_result_scope_missing"),
    ({"sponsors": ["Harbor Pavilion League", "Cinder Orchard Trust"]}, "statistics_result_scope_missing"),
    ({"date_from": "2033-01-01"}, "statistics_result_scope_missing"),
    ({"date_to": "2037-12-31"}, "statistics_result_scope_missing"),
    ({"include_inferred_dates": True}, "statistics_result_scope_missing"),
    ({"include_unknown_dates": True}, "statistics_result_scope_missing"),
    ({"platforms": ["Garden Channel"]}, "statistics_result_scope_missing"),
    ({"keywords": ["fictional extra selection"]}, "statistics_result_scope_missing"),
    ({"record_ids": ["fictional-extra-record"]}, "statistics_result_scope_missing"),
])
def test_actual_statistics_cannot_widen_or_replace_trusted_scope(mutation, reason):
    base = Filters(dataset="all", sponsors=["Harbor Pavilion League"],
                   date_from="2035-02-01", date_to="2036-08-01", include_unknown_dates=False)
    correct = base.model_copy(update={"dataset": "native"}).model_dump(mode="json")
    catalog = _Catalog(base, lambda *_: {"status": "ok", "filters": {**correct, **mutation}, "count": 4})
    run = _ScriptedAgent(catalog, [("record_statistics", {"measure": "count", "filters": {
        "dataset": "native", "sponsors": ["Harbor Pavilion League"],
        "date_from": "2034-01-01", "date_to": "2037-12-31"}})]).run(
            "Count native ads sponsored by Harbor Pavilion League from 2034 through 2037.", base, "fictional")
    assert run.failure_reason == reason
    assert not run.result  # Failed scope reads never publish the returned count.
    assert run.tool_trace[-1]["status"] == "invalid_result"


def test_two_valid_narrow_collection_reads_complete_without_widening():
    base = Filters(dataset="all", date_from="2035-04-01", date_to="2036-10-01")
    native = "Count native ads from 2034 through 2037"
    social = "count social posts from 2034 through 2037"
    question = native + " and " + social + "."
    plan = {"tasks": [{"question_part": native, "dataset": "native", "route": "statistics"},
                      {"question_part": social, "dataset": "social", "route": "statistics"}]}
    calls = [("set_research_plan", plan)] + [("record_statistics", {"measure": "count", "filters": {
        "dataset": dataset, "date_from": "2034-01-01", "date_to": "2037-12-31"}})
        for dataset in ("native", "social")]
    catalog = _Catalog(base)
    run = _ScriptedAgent(catalog, calls).run(question, base, "fictional")
    assert run.result["complete"] and not run.failure_reason
    assert [part["result"]["filters"]["dataset"] for part in run.result["parts"]] == ["native", "social"]
    assert all(part["result"]["filters"]["date_from"] == "2035-04-01" for part in run.result["parts"])


def test_planned_missing_predicate_blocks_before_any_read():
    base = Filters(dataset="all")
    native = "Count native ads from 2034 through 2037"
    social = "count social posts"
    plan = {"tasks": [{"question_part": native, "dataset": "native", "route": "statistics"},
                      {"question_part": social, "dataset": "social", "route": "statistics"}]}
    catalog = _Catalog(base)
    run = _ScriptedAgent(catalog, [("set_research_plan", plan),
        ("record_statistics", {"measure": "count", "filters": {"dataset": "native"}})]).run(
            native + " and " + social + ".", base, "fictional")
    assert run.failure_reason == "research_plan_predicate_missing"
    assert not catalog.reads and not run.result["complete"]


def test_same_record_fields_keep_one_selection_and_version_binding():
    base = Filters(dataset="native")
    selected = {"record_id": "fictional-native-lantern", "dataset": "native", "version_id": "fictional-version-3", "body_hash": "c" * 64}

    def read(name, arguments):
        common = {"status": "ok", "filters": base.model_dump(mode="json")}
        if name == "find_records":
            return {**common, "total_candidates": 1, "records": [selected]}
        return {**common, "record": selected, "fields": {"publisher": "Copper Finch Review", "publication_date": "2035-04-01"}}

    catalog = _Catalog(base, read)
    agent = _ScriptedAgent(catalog, [("find_records", {"title": "Moss Lantern"}),
                                    ("get_record_metadata", {"record_id": selected["record_id"], "fields": ["publisher", "publication_date"]})])
    run = agent.run("What is the stored publisher and what is the stored date for the article titled 'Moss Lantern'?", base, "fictional")
    assert run.route == "metadata" and not run.failure_reason
    assert "set_research_plan" not in agent.dispatched_tools[0]
    assert len(catalog.reads) == 2


@pytest.mark.parametrize("changed", [{"record_id": "fictional-other-record"}, {"version_id": "fictional-version-4"}])
def test_same_record_field_exemption_preserves_record_and_version_checks(changed):
    base = Filters(dataset="native")
    selected = {"record_id": "fictional-native-lantern", "dataset": "native", "version_id": "fictional-version-3", "body_hash": "c" * 64}

    def read(name, arguments):
        common = {"status": "ok", "filters": base.model_dump(mode="json")}
        if name == "find_records":
            return {**common, "total_candidates": 1, "records": [selected]}
        return {**common, "record": {**selected, **changed}}

    metadata_id = changed.get("record_id", selected["record_id"])
    catalog = _Catalog(base, read)
    run = _ScriptedAgent(catalog, [("find_records", {"title": "Moss Lantern"}),
                                 ("get_record_metadata", {"record_id": metadata_id, "fields": ["publisher", "publication_date"]})]).run(
        "What is the stored publisher and what is the stored date for the article titled 'Moss Lantern'?", base, "fictional")
    assert run.failure_reason in {"research_record_binding_mismatch", "research_record_version_changed"}

