"""Synthetic composite execution and web-display boundaries; no held-out input."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from observatory.app import _render_answer_result
from observatory.models import Answer, Filters
from observatory.research_agent import ResearchAgent, ResearchRun, _CallFailure
from observatory.service import Service

QUESTION = "Count native records and count social records"
PARTS = [
    {"question_part": "Count native records", "dataset": "native", "route": "statistics"},
    {"question_part": "count social records", "dataset": "social", "route": "statistics"},
]


def statistics(dataset, total, *, filters=None):
    return {
        "status": "ok", "kind": "count", "method": "database", "group_by": "none",
        "filters": (filters or Filters(dataset=dataset, include_inferred_dates=True)).model_dump(mode="json"),
        "collections": [{"dataset": dataset, "total": total, "retrievable": total, "unknown_dates": 0}],
        "groups": [],
    }


class Catalog:
    def __init__(self, bad_result=None):
        self.bad_result = bad_result

    def definitions(self):
        return [{"type": "function", "name": "record_statistics", "strict": True,
                 "description": "Synthetic read", "parameters": {"type": "object",
                 "additionalProperties": False, "properties": {}, "required": []}}]

    def call(self, name, args):
        dataset = args["filters"]["dataset"]
        if dataset == "social" and self.bad_result is not None:
            return deepcopy(self.bad_result)
        return statistics(dataset, 6 if dataset == "native" else 10)


class Agent(ResearchAgent):
    def __init__(self, failure=None, bad_result=None):
        super().__init__(None, Catalog(bad_result))
        self.failure = failure

    def _dispatch(self, inputs, definitions, visitor, step):
        if step == 3 and self.failure:
            raise _CallFailure(self.failure, limited=self.failure == "research_budget_limit")
        name, args = ("set_research_plan", {"tasks": PARTS}) if step == 1 else (
            "record_statistics", {"filters": {"dataset": "native" if step == 2 else "social"}},
        )
        return {"status": "completed", "output": [{"type": "function_call", "name": name,
                "call_id": f"synthetic-step-{step}", "arguments": json.dumps(args)}]}, {
                    "state": "synthetic", "step": step,
                }, 0.02


class SavedDB:
    def __init__(self):
        self.answers = []

    def save_answer(self, question, filters, result, version):
        self.answers.append(result.model_dump(mode="json"))


def service(agent):
    db = SavedDB()
    item = Service(SimpleNamespace(research_agent_enabled=True), db=db, rag=object(), research_agent=agent)
    item.health = lambda: {"status": "ok", "data_version": "synthetic-version"}
    item.web_calls = []

    def web(*args, **kwargs):
        item.web_calls.append(args)
        return {"status": "ok", "source_kind": "external_web", "cost_usd": 0.03,
                "summary": "A web page describes a separate public project. [W1]",
                "passages": [{"text": "A web page describes a separate public project. [W1]", "source_ids": ["W1"]}],
                "sources": [{"source_id": "W1", "url": "https://example.org/cedar-project",
                             "title": "Cedar project", "supports_generated_paragraph": True}]}

    item._search_external = web
    return item, db


@pytest.mark.parametrize("reason,status", [
    ("research_budget_limit", "limited"),
    ("research_budget_unavailable", "service_unavailable"),
    ("research_provider_unavailable", "service_unavailable"),
    ("research_model_configuration", "service_unavailable"),
    ("research_context_limit", "service_unavailable"),
    ("unclassified_future_outage", "service_unavailable"),
])
def test_paid_or_operational_failure_after_a_completed_part_never_searches_web(reason, status):
    item, db = service(Agent(failure=reason))
    result = item.answer(QUESTION, Filters(dataset="all"), "synthetic-reader")
    assert result.status == status and result.failure_reason == reason
    assert not item.web_calls and not result.external_research
    assert not result.structured_result["complete"]
    assert result.structured_result["parts"][0]["answer"]["structured_result"]["collections"][0]["total"] == 6
    assert result.cost_usd == pytest.approx(0.04) and len(db.answers) == 1
    shown = str(_render_answer_result(result.model_dump(mode="json"), True, item))
    assert "Partial answer" in shown
    assert ("Paid answers temporarily limited" if status == "limited" else "Answer service unavailable") in shown


def test_wrong_result_collection_after_completed_part_never_becomes_a_web_gap():
    item, _ = service(Agent(bad_result=statistics("native", 10)))
    result = item.answer(QUESTION, Filters(dataset="all"), "synthetic-reader")
    assert result.status == "service_unavailable"
    assert result.failure_reason == "research_plan_result_scope_mismatch"
    assert not item.web_calls and len(result.structured_result["parts"]) == 1


@pytest.mark.parametrize("reason", [
    "research_record_version_changed", "research_title_binding_invalid",
    "research_plan_result_predicate_missing", "statistics_scope_validation_unavailable",
    "research_tool_unavailable", "research_tool_invalid_result", "research_response_incomplete",
    "research_step_limit", "research_intermediate_limit", "research_plan_interrupted",
])
def test_composite_integrity_or_execution_failure_keeps_its_reason(reason):
    run = ResearchRun(route="composite", failure_reason=reason, result={
        "status": "ok", "kind": "composite", "complete": False, "parts": [], "pending_parts": PARTS,
    })
    item, _ = service(SimpleNamespace(run=lambda *args, **kwargs: run))
    result = item.answer(QUESTION, Filters(dataset="all"), "synthetic-reader")
    assert result.status == "service_unavailable" and result.failure_reason == reason
    assert not item.web_calls


@pytest.mark.parametrize("boundary", ["steps", "intermediate"])
def test_actual_execution_cap_keeps_the_completed_part_without_searching_web(boundary, monkeypatch):
    agent = Agent()
    if boundary == "steps":
        agent.max_steps = 2
        reason = "research_step_limit"
    else:
        monkeypatch.setattr("observatory.research_agent.MAX_INTERMEDIATE_BYTES", 1)
        reason = "research_intermediate_limit"
    item, _ = service(agent)
    result = item.answer(QUESTION, Filters(dataset="all"), "synthetic-reader")
    assert result.status == "service_unavailable" and result.failure_reason == reason
    assert not item.web_calls
    assert len(result.structured_result["parts"]) == 1
    assert result.structured_result["pending_parts"] == [PARTS[1]]
    assert result.cost_usd == pytest.approx(0.04)


def test_actual_tool_clarification_keeps_its_specific_message_and_can_search_web():
    message = "The social collection requires a platform selection."
    item, _ = service(Agent(bad_result={"status": "clarify", "message": message}))
    result = item.answer(QUESTION, Filters(dataset="all"), "synthetic-reader")
    assert result.status == "answered" and result.answer_mode == "web_supplement"
    assert result.failure_reason == "research_plan_clarification"
    assert len(item.web_calls) == 1 and not result.structured_result["complete"]
    assert result.structured_result["pending_parts"] == [PARTS[1]]
    assert message in result.answer
    shown = str(_render_answer_result(result.model_dump(mode="json"), True, item))
    assert message in shown and "Still unresolved" in shown


@pytest.mark.parametrize("reason", [
    "research_plan_invalid", "research_plan_incomplete", "explicit_collection_scope_missing",
    "statistics_question_scope_missing", "original_source_record_unresolved",
])
def test_real_scope_or_planning_gaps_still_attempt_one_labeled_web_supplement(reason):
    run = ResearchRun(route="composite", failure_reason=reason, result={
        "status": "ok", "kind": "composite", "complete": False,
        "parts": [{**PARTS[0], "result": statistics("native", 6)}], "pending_parts": [PARTS[1]],
    })
    item, _ = service(SimpleNamespace(run=lambda *args, **kwargs: run))
    result = item.answer(QUESTION, Filters(dataset="all"), "synthetic-reader")
    assert result.status == "answered" and result.answer_mode == "web_supplement"
    assert result.failure_reason == reason and len(item.web_calls) == 1
    assert result.cost_usd == pytest.approx(0.03)
    assert not result.structured_result["complete"] and not result.citations
    assert result.structured_result["parts"][0]["answer"]["structured_result"]["collections"][0]["total"] == 6
    shown = str(_render_answer_result(result.model_dump(mode="json"), True, item))
    assert "Partial answer" in shown and "count social records" in shown
    assert "Additional web sources" in shown and "Cedar project" in shown
    assert "Still unresolved" in shown


def test_partial_original_fields_are_visible_without_web_replacing_the_missing_cell():
    data = {"status": "ok", "record": {"record_id": "cedar-ledger", "dataset": "native"},
            "original_fields": {
                "disclosure_language": {"status": "recorded", "value": "Cedar paid notice"},
                "disclosure_location": {"status": "unknown", "value": None},
            }}
    result = Service._tool_metadata_answer(data)
    assert result.status == "answered" and not result.structured_result["complete"]
    item, _ = service(None)
    original_fields = deepcopy(result.structured_result["original_fields"])
    result = item._ensure_answer("Read both original disclosure fields", Filters(), "synthetic-reader", result)
    assert len(item.web_calls) == 1 and result.answer_mode == "web_supplement"
    assert result.structured_result["original_fields"] == original_fields
    assert result.structured_result["missing_fields"] == ["disclosure_location"]
    assert result.external_research["collection_answer"]["answer_mode"] == "tools"
    assert not result.citations and not result.structured_result["complete"]
    shown = str(_render_answer_result(result.model_dump(mode="json"), True, item))
    assert "Cedar paid notice" in shown and "Unknown" in shown and "Original source fields" in shown
    assert "Why the collection could not answer" in shown
    assert "missing and review fields remain unresolved" in shown
    assert "Additional web sources" in shown


def test_composite_keeps_partial_original_fields_and_exact_collection_count_beside_web():
    metadata = {"status": "ok", "record": {"record_id": "cedar-ledger", "dataset": "native"},
                "original_fields": {
                    "disclosure_language": {"status": "recorded", "value": "Cedar paid notice"},
                    "disclosure_location": {"status": "not_recorded", "value": None},
                }}
    declared = [
        {"question_part": "Read stored native fields", "dataset": "native", "route": "metadata"},
        {"question_part": "Count social records", "dataset": "social", "route": "statistics"},
    ]
    run = ResearchRun(route="composite", result={
        "status": "ok", "kind": "composite", "complete": True, "pending_parts": [],
        "parts": [{**declared[0], "result": metadata}, {**declared[1], "result": statistics("social", 10)}],
    })
    item, _ = service(SimpleNamespace(run=lambda *args, **kwargs: run))
    result = item.answer("Read stored native fields and count social records", Filters(dataset="all"), "synthetic-reader")
    assert result.status == "answered" and result.answer_mode == "web_supplement"
    assert not result.structured_result["complete"] and len(item.web_calls) == 1
    parts = result.structured_result["parts"]
    assert parts[0]["answer"]["structured_result"]["original_fields"] == metadata["original_fields"]
    assert parts[0]["answer"]["structured_result"]["missing_fields"] == ["disclosure_location"]
    assert parts[1]["answer"]["structured_result"]["collections"][0]["total"] == 10
    assert result.structured_result["incomplete_parts"] == [{
        **declared[0], "failure_reason": "original_source_metadata_missing",
    }]
    shown = str(_render_answer_result(result.model_dump(mode="json"), True, item))
    assert "Cedar paid notice" in shown and "Unknown" in shown
    assert "Collection statistics" in shown and "Additional web sources" in shown


def test_unavailable_published_claims_in_one_part_never_becomes_a_web_gap():
    run = ResearchRun(route="composite", result={
        "status": "ok", "kind": "composite", "complete": True, "pending_parts": [],
        "parts": [{"question_part": "Read published native assignments", "dataset": "native", "route": "claims",
                   "result": {"status": "ok", "available": False}}],
    })
    item, _ = service(SimpleNamespace(run=lambda *args, **kwargs: run))
    result = item.answer("Read published native assignments", Filters(), "synthetic-reader")
    assert result.status == "service_unavailable" and result.failure_reason == "claims_results_unavailable"
    assert not result.structured_result["complete"] and not item.web_calls


def test_complete_original_field_answer_does_not_search_web():
    result = Service._tool_metadata_answer({"status": "ok", "original_fields": {
        "title": {"status": "recorded", "value": "Cedar ledger"},
    }})
    item, _ = service(None)
    assert item._ensure_answer("Read its stored title", Filters(), "synthetic-reader", result) is result
    assert not item.web_calls


def test_existing_unsuccessful_web_attempt_is_not_retried_for_partial_fields():
    result = Answer(status="answered", answer="One stored field is missing", answer_mode="tools",
                    failure_reason="original_source_metadata_missing",
                    structured_result={"kind": "original_metadata", "complete": False},
                    external_research={"status": "limited", "reason": "web_budget_limit"})
    item, _ = service(None)
    assert item._ensure_answer("Read both stored fields", Filters(), "synthetic-reader", result) is result
    assert not item.web_calls and result.external_research["status"] == "limited"
