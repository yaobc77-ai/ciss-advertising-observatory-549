"""Fresh non-advertising planner contracts; no database or provider requests."""

import copy
import json
from typing import get_args

import pytest

from observatory.models import Filters
from observatory.question_policy import question_contract
from observatory.research_agent import (
    INTERMEDIATE_TOOLS,
    PLAN_ROUTES,
    TERMINAL_ROUTES,
    ResearchAgent,
    _CallFailure,
    _validate_plan,
)

CATALOGUE = "Read the Pineglass exhibition catalogue"
TRAIL = "read the Fenridge trail notice"
QUESTION = CATALOGUE + " and " + TRAIL


def _plan(*parts, dataset="native", routes=None):
    routes = routes or ["record"] * len(parts)
    return {"tasks": [{"question_part": part, "dataset": dataset, "route": route}
                      for part, route in zip(parts, routes, strict=True)]}


class _Catalog:
    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def definitions(self):
        return [{"type": "function", "name": name, "strict": True,
                 "parameters": {"type": "object", "properties": {}, "required": [],
                                "additionalProperties": False}}
                for name in sorted(set(TERMINAL_ROUTES) | INTERMEDIATE_TOOLS)]

    def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        return copy.deepcopy(self.results.get(name) or {
            "status": "ok", "filters": args.get("filters") or {"dataset": "native"},
            "record": {"record_id": args.get("record_id", "synthetic-exhibit"),
                       "dataset": "native"},
        })


class _ScriptedAgent(ResearchAgent):
    def __init__(self, calls, catalog=None):
        super().__init__(None, catalog or _Catalog(), entity_context={})
        self.script = iter(calls)
        self.dispatched = []

    def _dispatch(self, inputs, definitions, visitor, step):
        self.dispatched.append({"inputs": copy.deepcopy(inputs),
                                "names": {item["name"] for item in definitions}})
        call = next(self.script)
        if isinstance(call, Exception):
            raise call
        name, args = call
        return ({"status": "completed", "output": [{
            "type": "function_call", "name": name, "call_id": f"synthetic-{step}",
            "arguments": json.dumps(args),
        }]}, {"step": step, "state": "offline_stub"}, 0.0)


def test_plan_routes_exclude_evidence_until_compound_answer_support_exists():
    assert set(get_args(PLAN_ROUTES)) == set(TERMINAL_ROUTES.values()) - {"evidence"}
    with pytest.raises(ValueError):
        _validate_plan(_plan(CATALOGUE, TRAIL, routes=["record", "evidence"]), QUESTION, Filters())


def test_literal_plan_preserves_original_order():
    tasks = _validate_plan(_plan(CATALOGUE, TRAIL), QUESTION, Filters())
    assert [task.question_part for task in tasks] == [CATALOGUE, TRAIL]


@pytest.mark.parametrize("args,question,scope", [
    (_plan(TRAIL, CATALOGUE), QUESTION, Filters()),
    (_plan(CATALOGUE.upper(), TRAIL), QUESTION, Filters()),
    (_plan(CATALOGUE, "Pineglass exhibition catalogue"), QUESTION, Filters()),
    (_plan(CATALOGUE, TRAIL), QUESTION + " and archive the factory inspection", Filters()),
    (_plan(CATALOGUE, CATALOGUE), CATALOGUE + " and " + CATALOGUE, Filters()),
    (_plan(CATALOGUE, TRAIL, dataset="all"), QUESTION, Filters()),
    (_plan(CATALOGUE, TRAIL, dataset="social"), QUESTION, Filters()),
    (_plan("Read native records of the exhibit", "read social records of the trail"),
     "Read native records of the exhibit and read social records of the trail", Filters(dataset="all")),
])
def test_invalid_plans_keep_literal_order_overlap_and_scope_guards(args, question, scope):
    with pytest.raises(ValueError):
        _validate_plan(args, question, scope)


@pytest.mark.parametrize("args", [
    _plan(CATALOGUE),
    _plan(CATALOGUE.upper(), TRAIL),
    _plan(TRAIL, CATALOGUE),
    {"tasks": [{"question_part": QUESTION, "dataset": "native", "route": "invented"}]},
])
def test_invalid_model_plan_is_clarification_with_original_failure(args):
    agent = _ScriptedAgent([("set_research_plan", args)])
    run = agent.run(QUESTION, Filters(), "synthetic")
    assert run.route == "clarify"
    assert run.failure_reason == "research_plan_invalid"
    assert run.result["status"] == "clarify"
    assert run.tool_trace[0]["status"] == "invalid_request"
    assert not agent.catalog.calls
    assert len(run.model_calls) == 1


def test_provider_failure_stays_unavailable():
    agent = _ScriptedAgent([_CallFailure("research_provider_unavailable")])
    run = agent.run(CATALOGUE, Filters(), "synthetic")
    assert run.route == "unavailable"
    assert run.failure_reason == "research_provider_unavailable"
    assert not agent.catalog.calls


def test_two_distinct_reads_require_a_plan():
    agent = _ScriptedAgent([("get_record", {"record_id": "synthetic-exhibit"})])
    run = agent.run(QUESTION, Filters(), "synthetic")
    assert run.route == "clarify"
    assert run.failure_reason == "research_plan_required"
    assert not agent.catalog.calls


def test_checked_plan_executes_original_parts_in_order_without_rewrite():
    agent = _ScriptedAgent([
        ("set_research_plan", _plan(CATALOGUE, TRAIL)),
        ("get_record", {"record_id": "synthetic-exhibit", "filters": {"dataset": "native"}}),
        ("get_record", {"record_id": "synthetic-trail", "filters": {"dataset": "native"}}),
    ])
    run = agent.run(QUESTION, Filters(), "synthetic")
    assert run.route == "composite" and run.result["complete"]
    assert [part["question_part"] for part in run.result["parts"]] == [CATALOGUE, TRAIL]
    assert run.original_question == QUESTION
    payload = json.loads(agent.dispatched[0]["inputs"][1]["content"])
    assert payload["question"] == QUESTION
    assert [args["record_id"] for _, args in agent.catalog.calls] == [
        "synthetic-exhibit", "synthetic-trail",
    ]
    assert len(run.model_calls) == 3 and run.cost_usd == 0


def test_wrong_plan_route_does_not_read_or_mark_complete():
    agent = _ScriptedAgent([
        ("set_research_plan", _plan(CATALOGUE, TRAIL)),
        ("get_record_sources", {"record_id": "synthetic-exhibit"}),
    ])
    run = agent.run(QUESTION, Filters(), "synthetic")
    assert run.route == "composite" and not run.result["complete"]
    assert run.failure_reason == "research_plan_scope_mismatch"
    assert len(run.result["pending_parts"]) == 2
    assert not agent.catalog.calls


def test_wrong_task_dataset_does_not_read_or_mark_complete():
    agent = _ScriptedAgent([
        ("set_research_plan", _plan(CATALOGUE, TRAIL)),
        ("get_record", {"record_id": "synthetic-exhibit", "filters": {"dataset": "social"}}),
    ])
    run = agent.run(QUESTION, Filters(dataset="all"), "synthetic")
    assert run.route == "composite" and not run.result["complete"]
    assert run.failure_reason == "research_plan_scope_mismatch"
    assert len(run.result["pending_parts"]) == 2
    assert not agent.catalog.calls


def test_tool_result_cannot_change_declared_task_dataset():
    catalog = _Catalog({"get_record": {"status": "ok", "record": {
        "record_id": "synthetic-exhibit", "dataset": "social",
    }}})
    agent = _ScriptedAgent([
        ("set_research_plan", _plan(CATALOGUE, TRAIL)),
        ("get_record", {"record_id": "synthetic-exhibit", "filters": {"dataset": "native"}}),
    ], catalog)
    run = agent.run(QUESTION, Filters(dataset="all"), "synthetic")
    assert run.route == "composite" and not run.result["complete"]
    assert run.failure_reason == "research_plan_result_scope_mismatch"
    assert run.tool_trace[-1]["status"] == "invalid_result"
    assert not run.result["parts"] and len(run.result["pending_parts"]) == 2


@pytest.mark.parametrize("change_id", [True, False])
def test_selected_source_record_and_version_remain_bound(change_id):
    selected = {"record_id": "synthetic-exhibit", "dataset": "native",
                "version_id": "pineglass-version", "body_hash": "pineglass-body"}
    changed = {**selected, "version_id": "replacement-version"}
    catalog = _Catalog({
        "find_records": {"status": "ok", "total_candidates": 1, "records": [selected]},
        "get_record": {"status": "ok", "record": changed},
    })
    agent = _ScriptedAgent([
        ("set_research_plan", _plan(CATALOGUE, TRAIL)),
        ("find_records", {"title": "Pineglass exhibition catalogue"}),
        ("get_record", {"record_id": "replacement-record" if change_id else "synthetic-exhibit"}),
    ], catalog)
    run = agent.run(QUESTION, Filters(), "synthetic")
    assert run.route == "composite" and not run.result["complete"]
    assert run.failure_reason == ("research_record_binding_mismatch" if change_id
                                  else "research_record_version_changed")
    assert len(catalog.calls) == (1 if change_id else 2)
    assert not run.result["parts"] and len(run.result["pending_parts"]) == 2


def test_single_retrieval_with_conjoined_object_words_keeps_search_available():
    question = "Find the Cedar display notice and the Willow display notice."
    assert not question_contract(question)["compound_read_request"]
    agent = _ScriptedAgent([("search_records", {"query": "display notice"})])
    run = agent.run(question, Filters(), "synthetic")
    assert run.route == "evidence"
    assert run.original_question == question
    assert "search_records" in agent.dispatched[0]["names"]


@pytest.mark.parametrize("question", [
    "The Fenridge trail is closed; what does its maintenance notice say?",
    "The Pineglass exhibition opens tomorrow. Which notice describes access?",
    "The Larkspur inspection is pending! What does its workshop notice say?",
    "Find the Fenridge trail closure notice and show its wording.",
    "Locate the Pineglass visitor notice then show its original text.",
])
def test_single_retrieval_is_not_forced_into_a_compound_plan(question):
    assert not question_contract(question)["compound_read_request"]
    agent = _ScriptedAgent([("search_records", {"query": "maintenance notice"})])
    run = agent.run(question, Filters(), "synthetic")
    assert run.route == "evidence"
    assert run.original_question == question


@pytest.mark.parametrize("question", [
    "Count the Pineglass exhibit records and show their original source URLs.",
    "Read the Pineglass catalogue and read the Fenridge closure notice.",
    "Find native records of the exhibit and find social records of the trail.",
    "Find the Larkspur inspection notice and show the Fenridge maintenance schedule.",
    "Find the Larkspur inspection notice and show its wording and count workshop records.",
    "Describe the Pineglass opening arrangements and read its stored source metadata.",
    "Give me the exhibit record and show the original trail source.",
    "How many exhibit records are stored? Then show their original URLs.",
])
def test_genuinely_distinct_reads_keep_the_plan_requirement(question):
    assert question_contract(question)["compound_read_request"]
    agent = _ScriptedAgent([("search_records", {"query": "synthetic material"})])
    run = agent.run(question, Filters(dataset="all"), "synthetic")
    assert run.route == "clarify" and run.failure_reason == "research_plan_required"
    assert not agent.catalog.calls

