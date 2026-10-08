"""Independent fictional fixtures for the opt-in interpretation transport."""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from observatory.budget import LimitReached
from observatory.config import Settings
from observatory.models import Filters
from observatory.research_agent import ResearchAgent, ResearchRun

QUESTION = "Count Aurora Utilities records."


def ready_intent():
    return {"schema_version": "question-intent-v1", "status": "ready",
            "tasks": [{"route": "statistics", "question_part": QUESTION,
                       "dataset": "native", "request": {"filters": {"sponsors": ["Aurora Utilities"]},
                                                           "measure": "count"}}],
            "shared_constraints": [], "unresolved_constraints": [], "message": None}


class Budget:
    def __init__(self, limited=False):
        self.limited = limited
        self.reservations, self.settlements, self.uncertain_calls = [], [], []

    def reserve(self, estimate, visitor, purpose, model):
        if self.limited:
            raise LimitReached("Synthetic budget limit")
        self.reservations.append((estimate, visitor, purpose, model))
        return "reservation-fixture"

    def settle(self, reservation, cost, usage):
        self.settlements.append((reservation, cost, usage))

    def uncertain(self, reservation, reason):
        self.uncertain_calls.append((reservation, reason))

    def reservation_cost(self, reservation):
        return 0.01


def transport(payload=None, *, raw=None, status="completed", limited=False, provider_failure=False):
    sent = []

    def create(**kwargs):
        sent.append(kwargs)
        if provider_failure:
            raise RuntimeError("Synthetic provider failure")
        return {"status": status, "id": "response-fixture", "model": "gpt-5.6-luna",
                "usage": {"input_tokens": 100, "output_tokens": 30,
                          "input_tokens_details": {"cached_tokens": 0}},
                "output": [{"type": "function_call", "name": "interpret_question",
                            "call_id": "call-fixture", "arguments": raw or json.dumps(payload or ready_intent())}]}

    rag = SimpleNamespace(settings=Settings(), budget=Budget(limited),
                          client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    agent = ResearchAgent(rag, SimpleNamespace(), entity_context={}, intent_enabled=True)
    return agent, sent


def test_single_interpretation_compiles_and_reuses_accounted_dispatch(monkeypatch):
    agent, sent = transport()
    observed = []

    def execute(run, compiled, catalog, progress):
        observed.append(compiled)
        run.route, run.result = "statistics", {"status": "ok"}
        return run

    monkeypatch.setattr("observatory.intent_execution.execute_intent", execute)
    run = agent.run(QUESTION, Filters(dataset="native"), "visitor")
    assert run.route == "statistics"
    assert len(sent) == len(run.model_calls) == len(agent.rag.budget.settlements) == 1
    assert observed[0].tasks[0].arguments["filters"]["sponsors"] == ["Aurora Utilities"]
    assert sent[0]["tools"][0]["name"] == "interpret_question"
    assert len(sent[0]["tools"]) == 1
    assert "question_contract" not in json.loads(sent[0]["input"][1]["content"])
    assert run.audit()["interpretation"]["schema_version"] == "question-intent-v1"
    assert agent.rag.budget.settlements[0][2]["observatory_request"]["policy"] == "question-intent-v1"
    assert isinstance(agent.rag.budget.settlements[0][1], Decimal)


@pytest.mark.parametrize("raw", [
    '{"status":"ready","status":"clarify"}',
    '{"schema_version":"question-intent-v1","status":"ready","sql":"SELECT 1"}',
    '{"schema_version":"question-intent-v1","status":"ready","tasks":NaN}',
    '[]',
])
def test_invalid_interpretation_never_executes_reads(monkeypatch, raw):
    agent, sent = transport(raw=raw)
    monkeypatch.setattr("observatory.intent_execution.execute_intent", lambda *args: pytest.fail("Unexpected read"))
    run = agent.run(QUESTION, Filters(dataset="native"), "visitor")
    assert run.route == "unavailable" and run.failure_reason == "research_intent_invalid"
    assert len(sent) == len(agent.rag.budget.settlements) == 1
    assert run.tool_trace == []


def test_explicit_unresolved_scope_remains_clarification(monkeypatch):
    payload = {"status": "clarify", "tasks": [], "message": "Which collection?",
               "unresolved_constraints": [QUESTION]}
    agent, _ = transport(payload)
    monkeypatch.setattr("observatory.intent_execution.execute_intent", lambda *args: pytest.fail("Unexpected read"))
    run = agent.run(QUESTION, Filters(), "visitor")
    assert run.route == "clarify" and run.result["message"] == "Which collection?"


def test_budget_limit_does_not_dispatch_or_become_missing_evidence():
    agent, sent = transport(limited=True)
    run = agent.run(QUESTION, Filters(), "visitor")
    assert run.route == "limited" and run.failure_reason == "research_budget_limit"
    assert sent == [] and run.cost_usd == 0


def test_provider_failure_preserves_uncertain_exposure():
    agent, sent = transport(provider_failure=True)
    run = agent.run(QUESTION, Filters(), "visitor")
    assert len(sent) == 1 and run.route == "unavailable"
    assert run.failure_reason == "research_provider_unavailable" and run.cost_usd == 0.01
    assert agent.rag.budget.uncertain_calls == [("reservation-fixture", "RuntimeError")]


def test_incomplete_provider_output_is_not_executed(monkeypatch):
    agent, _ = transport(status="incomplete")
    monkeypatch.setattr("observatory.intent_execution.execute_intent", lambda *args: pytest.fail("Unexpected read"))
    run = agent.run(QUESTION, Filters(), "visitor")
    assert run.route == "unavailable" and run.failure_reason == "research_response_incomplete"


def test_environment_opt_in_is_default_off(monkeypatch):
    monkeypatch.setattr("observatory.config.load_dotenv", lambda **kwargs: None)
    monkeypatch.delenv("OBS_QUESTION_INTENT_ENABLED", raising=False)
    assert Settings.from_env().question_intent_enabled is False
    monkeypatch.setenv("OBS_QUESTION_INTENT_ENABLED", "true")
    assert Settings.from_env().question_intent_enabled is True


def test_service_selects_intent_entry_without_legacy_agent_flag(monkeypatch):
    from observatory.service import Service

    service = Service.__new__(Service)
    service.settings = Settings(question_intent_enabled=True)
    calls = []
    answer = SimpleNamespace(latency_ms=0)
    service._answer_with_tools = lambda *args, **kwargs: calls.append("intent") or answer
    service._answer_legacy = lambda *args, **kwargs: pytest.fail("Unexpected legacy route")
    service._ensure_answer = lambda q, f, v, result, progress: result
    service._record_web_trace = lambda *args: None
    service._save_supplement = lambda *args: None
    assert service.answer(QUESTION, Filters(), "visitor") is answer
    assert calls == ["intent"]


def test_legacy_audit_shape_is_preserved():
    assert "interpretation" not in ResearchRun(route="statistics").audit()


def test_real_catalog_and_service_render_program_count_without_generation():
    """Exercise the actual adapter/catalog/renderer using a fictional snapshot."""
    from observatory.service import Service

    agent, sent = transport()
    agent.rag.settings.question_intent_enabled = True
    saved = []
    service = Service(agent.rag.settings,
                      db=SimpleNamespace(save_answer=lambda *args: saved.append(args)), rag=agent.rag)
    service.health = lambda: {"status": "ok", "data_version": "data-fixture",
                              "statistics_version": "stats-fixture",
                              "countable_record_counts": {"native": 3}}
    service.facets = lambda dataset: {"publishers": ["Beacon Gazette"],
                                      "sponsors": ["Aurora Utilities"], "accounts": [],
                                      "platforms": [], "keywords": [], "labels": []}
    service.dashboard = lambda filters, **kwargs: {
        "stats": {"total": 3, "retrievable": 2, "unknown_dates": 1,
                  "inferred_dates": {}}, "page": {"rows": []},
    }
    result = service.answer(QUESTION, Filters(dataset="native"), "visitor")
    assert result.status == "answered" and result.answer_mode == "statistics"
    assert result.structured_result["collections"] == [
        {"dataset": "native", "total": 3, "retrievable": 2, "unknown_dates": 1},
    ]
    assert len(sent) == 1 and len(saved) == 1
    assert result.research_trace["tools"][0]["tool"] == "record_statistics"
    assert result.research_trace["interpretation"]["intent"]["tasks"][0]["route"] == "statistics"
    assert result.cost_usd > 0  # Understanding still has an accounted model call.
