"""Independent fictional calendar and bound-record checks; no evaluation inputs."""

import json
from copy import deepcopy

import pytest

from observatory.models import Filters
from observatory.question_policy import statistics_request_preserves_question
from observatory.research_agent import ResearchAgent
from observatory.research_tools import ToolCatalog

IDENTITY = {"record_id": "fictional-reed-hexagon", "dataset": "native",
            "version_id": "fictional-revision-7", "body_hash": "d" * 64}
CONTEXT = {"sponsors": [{"value": "Blue Aster Cooperative"}], "publishers": [], "accounts": []}


class Catalog:
    def __init__(self, base, *, changed=None, candidates=1):
        self.base = base
        self.changed = changed or {}
        self.candidates = candidates
        self.reads = []

    def definitions(self):
        return ToolCatalog(None, self.base).definitions()

    def statistics_validation_context(self):
        return CONTEXT

    def call(self, name, arguments):
        self.reads.append((name, deepcopy(arguments)))
        common = {"status": "ok", "filters": self.base.model_dump(mode="json")}
        if name == "find_records":
            return {**common, "status": "ok" if self.candidates == 1 else "ambiguous",
                    "total_candidates": self.candidates, "records": [IDENTITY] * self.candidates}
        return {**common, "record": {**IDENTITY, **self.changed}, "fields": {"publisher": "Juniper Register"}}


class Agent(ResearchAgent):
    def __init__(self, catalog, calls):
        super().__init__(None, catalog, entity_context=CONTEXT)
        self.calls = iter(calls)
        self.schemas = []

    def _dispatch(self, inputs, definitions, visitor, step):
        self.schemas.append(deepcopy(definitions))
        name, arguments = next(self.calls)
        return {"status": "completed", "output": [{"type": "function_call", "name": name,
                "call_id": f"fictional-call-{step}", "arguments": json.dumps(arguments)}]}, {"synthetic": True}, 0.0


@pytest.mark.parametrize("tool,question", [
    ("get_record_metadata", "What is the stored publisher for the article titled 'Reed Hexagon'?"),
    ("get_record", "Read the article titled 'Reed Hexagon'."),
    ("get_record_sources", "Read the sources for the article titled 'Reed Hexagon'."),
])
def test_bound_id_is_executor_owned_and_absent_from_next_model_schema(tool, question):
    base = Filters(dataset="native")
    catalog = Catalog(base)
    agent = Agent(catalog, [("find_records", {"title": "Reed Hexagon"}), (tool, {})])
    run = agent.run(question, base, "fictional")
    assert not run.failure_reason
    assert catalog.reads[-1][1]["record_id"] == IDENTITY["record_id"]
    schema = next(item["parameters"] for item in agent.schemas[1] if item["name"] == tool)
    assert "record_id" not in schema["properties"]
    assert "record_id" not in schema.get("required", [])
    assert run.tool_trace[-1]["record_binding"]["source"] == "unique_title_match"
    assert "record_id" not in run.tool_trace[-1]["arguments"]
    assert run.tool_trace[-1]["execution_arguments"]["record_id"] == IDENTITY["record_id"]


def test_single_trusted_ui_record_needs_no_model_id_copy():
    base = Filters(dataset="native", record_ids=[IDENTITY["record_id"]])
    catalog = Catalog(base)
    run = Agent(catalog, [("get_record_metadata", {"fields": ["publisher"]})]).run(
        "What is the stored publisher?", base, "fictional")
    assert not run.failure_reason
    assert catalog.reads[-1][1]["record_id"] == IDENTITY["record_id"]
    assert run.tool_trace[-1]["record_binding"]["source"] == "active_record_filter"


@pytest.mark.parametrize("identifier", ["fictional-other-id", "fictional-reed-hexago", None, 7])
def test_attempted_record_substitution_is_still_refused(identifier):
    base = Filters(dataset="native")
    catalog = Catalog(base)
    run = Agent(catalog, [("find_records", {"title": "Reed Hexagon"}),
                         ("get_record_metadata", {"record_id": identifier})]).run(
        "What is the stored publisher for the article titled 'Reed Hexagon'?", base, "fictional")
    assert run.failure_reason == "research_record_binding_mismatch"
    assert len(catalog.reads) == 1


@pytest.mark.parametrize("change", [
    {"record_id": "fictional-other-id"}, {"version_id": "fictional-revision-8"},
    {"body_hash": "a" * 64}, {"dataset": "social"},
])
def test_executor_binding_does_not_weaken_current_source_validation(change):
    base = Filters(dataset="native")
    catalog = Catalog(base, changed=change)
    run = Agent(catalog, [("find_records", {"title": "Reed Hexagon"}),
                         ("get_record_metadata", {})]).run(
        "What is the stored publisher for the article titled 'Reed Hexagon'?", base, "fictional")
    assert run.failure_reason == "research_record_version_changed"
    assert not run.result


def test_ambiguous_title_does_not_bind_first_candidate():
    base = Filters(dataset="native")
    catalog = Catalog(base, candidates=2)
    run = Agent(catalog, [("find_records", {"title": "Reed Hexagon"})]).run(
        "What is the stored publisher for the article titled 'Reed Hexagon'?", base, "fictional")
    assert run.failure_reason == "original_source_record_unresolved"
    assert len(catalog.reads) == 1


@pytest.mark.parametrize("ids", [["fictional-other-id"], [IDENTITY["record_id"], "fictional-other-id"]])
def test_record_filter_cannot_override_bound_identity(ids):
    base = Filters(dataset="native")
    catalog = Catalog(base)
    run = Agent(catalog, [("find_records", {"title": "Reed Hexagon"}),
                         ("get_record_metadata", {"filters": {"record_ids": ids}})]).run(
        "What is the stored publisher for the article titled 'Reed Hexagon'?", base, "fictional")
    assert run.failure_reason == "research_record_binding_mismatch"
    assert len(catalog.reads) == 1


def test_unbound_read_keeps_id_required_and_bound_schema_does_not_mutate_catalog():
    from observatory.research_agent import _step_definitions

    catalog = Catalog(Filters(dataset="native"))
    definitions = catalog.definitions()
    _step_definitions(definitions, {}, [], [], IDENTITY)
    unbound = _step_definitions(definitions, {}, [], [])
    schema = next(item["parameters"] for item in unbound if item["name"] == "get_record")
    assert "record_id" in schema["properties"]
    assert "record_id" in schema["required"]


@pytest.mark.parametrize("case,question,arguments,reason", [
    ("entity", "Count native ads sponsored by Blue Aster Cooperative.",
     {"filters": {"dataset": "native"}}, "entity_scope_mismatch"),
    ("date", "Count native ads in 2039.",
     {"filters": {"dataset": "native", "date_from": "2040-01-01", "date_to": "2040-12-31"}},
     "date_endpoint_mismatch"),
    ("unsupported", "Count native ads in 2039-05.",
     {"filters": {"dataset": "native"}}, "unsupported_date_expression"),
    ("group", "Count native ads by publisher.",
     {"filters": {"dataset": "native"}, "group_by": "none"}, "grouping_mismatch"),
])
def test_scope_rejection_diagnostic_names_the_failed_constraint(case, question, arguments, reason):
    base = Filters(dataset="all")
    catalog = Catalog(base)
    run = Agent(catalog, [("record_statistics", arguments)]).run(question, base, "fictional")
    assert not catalog.reads
    assert run.tool_trace[-1]["scope_validation"]["stage"] == "request"
    assert run.tool_trace[-1]["scope_validation"]["reason"] == reason


def scope_ok(question, date_from=None, date_to=None, **arguments):
    return statistics_request_preserves_question(question, {}, {
        "measure": "count", "filters": {"dataset": "native", "date_from": date_from,
                                          "date_to": date_to}, **arguments,
    }, Filters(dataset="all").model_dump(mode="json"))


@pytest.mark.parametrize("wording", ["from 2039-2041", "from 2039 - 2041", "in 2039–2041"])
def test_explicit_year_range_spelling_preserves_both_endpoints(wording):
    question = f"Count native ads {wording}."
    assert scope_ok(question, "2039-01-01", "2041-12-31")
    assert not scope_ok(question, "2039-01-01", "2040-12-31")
    assert not scope_ok(question)


@pytest.mark.parametrize("question", [
    "在2039年5月统计原生广告", "Count native ads in 2039-05.",
    "Count native ads in 2039/05.", "Count native ads in 2039 Q2.",
    "Count native ads in 2039 May.", "Count native ads in 2039-5-2.",
])
@pytest.mark.parametrize("bounds", [(None, None), ("2039-01-01", "2039-12-31")])
def test_unsupported_subyear_expression_cannot_be_dropped_or_broadened(question, bounds):
    assert not scope_ok(question, *bounds)


def test_comparison_of_entities_over_one_range_is_not_a_period_comparison():
    assert scope_ok("Compare native ads by publisher from 2039 through 2041.",
                    "2039-01-01", "2041-12-31", group_by="publishers")


def test_comparison_without_another_axis_cannot_become_one_total():
    assert not scope_ok("Compare native ad counts from 2039 through 2041.",
                        "2039-01-01", "2041-12-31")


@pytest.mark.parametrize("connector", ["and", "versus", "vs"])
def test_listed_years_with_grouping_cannot_become_a_continuous_total(connector):
    assert not scope_ok(f"Compare counts by publisher in 2039 {connector} 2041.",
                        "2039-01-01", "2041-12-31", group_by="publishers")


def test_explicit_between_range_preserves_group_comparison():
    assert scope_ok("Compare native ads by publisher between 2039 and 2041.",
                    "2039-01-01", "2041-12-31", group_by="publishers")


def test_actual_period_comparison_still_requires_distinct_periods():
    assert not scope_ok("Compare native ad counts in 2039 versus 2041.",
                        "2039-01-01", "2041-12-31")
    assert scope_ok("Compare native ad counts in 2039 versus 2041.", periods=[
        {"date_from": "2039-01-01", "date_to": "2039-12-31"},
        {"date_from": "2041-01-01", "date_to": "2041-12-31"},
    ])
