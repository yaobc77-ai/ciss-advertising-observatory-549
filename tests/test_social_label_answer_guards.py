"""Final public answer guards after synthetic downstream work, without APIs.

Fake tool costs exercise bookkeeping only; no model, SQL or network runs.
The injected agent delegates actual read-only tools to the independent row oracle.
"""

from copy import deepcopy

import pytest
from test_social_label_tools import TRUE, StateDB

from observatory.config import Settings
from observatory.models import Answer, Citation, Evidence, Filters
from observatory.research_agent import ResearchRun
from observatory.research_tools import ToolCatalog
from observatory.service import Service

PRIVATE = "PRIVATE-GUARD-ERROR-DSN"


class SavedStateDB(StateDB):
    def __init__(self):
        super().__init__()
        self.saved = []

    def save_answer(self, *args):
        self.saved.append(args)


def edit_source_state(db):
    row = db.rows[0]
    # The selected green-messaging state, count, record/body version and health
    # marker remain unchanged; a different historical source code changes.
    row["oracle_states"]["renewable_energy"] = "source_false"
    payload = row["annotations"][0]["payload"]
    payload["values"]["renewable_energy"] = False
    payload["labels"].remove("renewable_energy")


class BoundAgent:
    def __init__(self, route):
        self.route = route
        self.service = None
        self.after_tool = None
        self.mutate_result = None
        self.result = None

    def run(self, question, filters, _visitor):
        tool, arguments = {
            "statistics": ("record_statistics", {"filters": {"labels": [TRUE]}}),
            "evidence": ("search_records", {"query": "renewable", "filters": {"labels": [TRUE]}}),
            "record": ("get_record", {"record_id": "s1", "filters": {"labels": [TRUE]}}),
            "sources": ("get_record_sources", {"record_id": "s1", "filters": {"labels": [TRUE]}}),
        }[self.route]
        data = ToolCatalog(self.service, filters).call(tool, arguments)
        assert data["status"] == "ok", data
        self.result = ResearchRun(route=self.route, result=data, cost_usd=0.002,
            original_question=question, base_filters=filters.model_dump(mode="json"),
            model_calls=[{"test": "injected-cost-no-provider"}],
            tool_trace=[{"tool": tool, "status": "ok"}])
        if self.mutate_result:
            self.mutate_result(data)
        if self.after_tool:
            self.after_tool()
        return self.result


def setup(route="evidence"):
    db = SavedStateDB()
    agent = BoundAgent(route)
    service = Service(Settings(research_agent_enabled=True, web_search_enabled=True),
                      db=db, rag=object(), research_agent=agent)
    agent.service = service
    filters = Filters(dataset="social", include_inferred_dates=False)
    return service, db, agent, filters


def generated_answer(db):
    row = db.rows[0]
    passage = Evidence(evidence_id="synthetic-evidence", dataset="social", record_id="s1",
                       version_id=row["version_id"], title=row["title"], text=row["body"],
                       start=0, end=len(row["body"]))
    return Answer(status="answered", answer="Synthetic supported answer.", answer_mode="rag",
                  evidence=[passage], citations=[Citation(evidence_id=passage.evidence_id, quote=passage.text)],
                  cost_usd=0.015, research_trace={"downstream": "offline-hook"})


@pytest.mark.parametrize("route", ["evidence", "statistics"])
def test_same_health_body_and_count_state_edit_after_downstream_is_withheld(monkeypatch, route):
    service, db, agent, filters = setup(route)
    original_health = deepcopy(db.health())
    original_body = (db.rows[0]["version_id"], db.rows[0]["body_hash"], db.rows[0]["body"])
    original_count = len(db.selected(db.rows, Filters(dataset="social", labels=[TRUE])))
    web = []
    monkeypatch.setattr(service, "_search_external", lambda *_a, **_k: web.append("forbidden") or {})
    if route == "evidence":
        def downstream(*_args, **_kwargs):
            answer = generated_answer(db)
            edit_source_state(db)
            return answer

        monkeypatch.setattr(service, "_answer_evidence", downstream)
    else:
        render = service._tool_statistics_answer

        def downstream(data, **kwargs):
            answer = render(data, **kwargs)
            edit_source_state(db)
            return answer

        monkeypatch.setattr(service, "_tool_statistics_answer", downstream)
    result = service.answer("Read the selected historical source records", filters, "synthetic-visitor")
    assert result.status == "service_unavailable" and result.failure_reason == "historical_source_state_unverified"
    assert not result.evidence and not result.citations and result.structured_result is None
    assert "Synthetic supported answer" not in result.answer
    assert result.cost_usd == pytest.approx(0.017 if route == "evidence" else 0.002)
    assert result.research_trace["model_calls"] == agent.result.model_calls
    assert result.research_trace["tools"] == agent.result.tool_trace
    assert len(db.saved) == 1 and db.saved[0][2] is result and not web
    assert db.health() == original_health
    assert (db.rows[0]["version_id"], db.rows[0]["body_hash"], db.rows[0]["body"]) == original_body
    assert len(db.selected(db.rows, Filters(dataset="social", labels=[TRUE]))) == original_count
    assert len(db.state_reads) == 3  # Actual tool before/after, then final public boundary.


@pytest.mark.parametrize("route", ["record", "sources", "statistics"])
def test_final_guard_covers_non_evidence_routes_after_agent_returns(route):
    service, db, agent, filters = setup(route)
    agent.after_tool = lambda: edit_source_state(db)
    result = service.answer("Read selected historical records", filters, "synthetic-visitor")
    assert result.status == "service_unavailable" and result.structured_result is None
    assert result.cost_usd == pytest.approx(0.002)
    assert result.research_trace["tools"] == agent.result.tool_trace


@pytest.mark.parametrize("mutation", ["missing", "bad_hash", "extra", "native", "outside_account", "bad_id"])
def test_invalid_or_outside_original_guard_is_withheld_without_details(monkeypatch, mutation):
    service, db, agent, filters = setup()
    filters.accounts = ["Shared Name"]

    def mutate(data):
        if mutation == "missing":
            data.pop("historical_source_state_guard")
            return
        guard = data["historical_source_state_guard"]
        if mutation == "bad_hash":
            guard["source_state_version"] = PRIVATE
        elif mutation == "extra":
            guard["private"] = PRIVATE
        elif mutation == "native":
            guard["filters"]["dataset"] = "native"
        elif mutation == "outside_account":
            guard["filters"]["accounts"] = []
        else:
            guard["filters"]["labels"] = ["claims-social-export-v1:bad:source_true"]

    agent.mutate_result = mutate
    monkeypatch.setattr(service, "_answer_evidence", lambda *_a, **_k: generated_answer(db))
    web = []
    monkeypatch.setattr(service, "_search_external", lambda *_a, **_k: web.append("forbidden") or {})
    result = service.answer("Read historical source records", filters, "synthetic-visitor")
    assert result.status == "service_unavailable" and result.failure_reason == "historical_source_state_unverified"
    assert result.cost_usd == pytest.approx(0.017) and not result.evidence
    assert result.structured_result is None and not web
    assert PRIVATE not in result.answer and PRIVATE not in str(result.external_research)
    assert len(db.state_reads) == 2  # Invalid scope/hash is rejected before a final source query.


def test_db_guard_failure_is_sanitized_and_costs_preserved(monkeypatch):
    service, db, _, filters = setup()

    def downstream(*_args, **_kwargs):
        answer = generated_answer(db)

        def unavailable(_scope):
            raise RuntimeError(PRIVATE)

        db.social_source_state_version = unavailable
        return answer

    monkeypatch.setattr(service, "_answer_evidence", downstream)
    result = service.answer("Read source records", filters, "synthetic-visitor")
    assert result.status == "service_unavailable" and result.cost_usd == pytest.approx(0.017)
    assert PRIVATE not in result.answer and not result.evidence and result.structured_result is None


def test_original_trusted_scope_is_frozen_before_downstream_mutation(monkeypatch):
    service, db, agent, filters = setup()
    filters.accounts = ["Shared Name"]

    def downstream(*_args, **_kwargs):
        filters.accounts.clear()  # Caller mutation cannot erase the original selection.
        agent.result.result["historical_source_state_guard"]["filters"]["accounts"] = []
        return generated_answer(db)

    monkeypatch.setattr(service, "_answer_evidence", downstream)
    result = service.answer("Read source records", filters, "synthetic-visitor")
    assert result.status == "service_unavailable" and not result.evidence


@pytest.mark.parametrize("route", ["evidence", "statistics", "record", "sources"])
def test_current_guarded_answers_remain_available_and_audited(monkeypatch, route):
    service, db, agent, filters = setup(route)
    if route == "evidence":
        monkeypatch.setattr(service, "_answer_evidence", lambda *_a, **_k: generated_answer(db))
    result = service.answer("Read the selected historical records", filters, "synthetic-visitor")
    assert result.status == "answered" and len(db.state_reads) == 3
    assert result.cost_usd == pytest.approx(0.017 if route == "evidence" else 0.002)
    assert db.saved[-1][2] is result
    if route == "statistics":
        assert result.structured_result["historical_source_state_guard"] == agent.result.result["historical_source_state_guard"]
        assert result.structured_result["data_version"] == db.version


def test_combined_original_scope_accepts_explicit_social_guard_without_widening(monkeypatch):
    service, db, agent, _ = setup()
    filters = Filters(dataset="all", sponsors=["Company affiliation"], include_inferred_dates=False)
    # A real tool explicitly narrows the all scope before it uses social IDs.
    original = agent.run

    def social_run(question, scope, visitor):
        run = original(question, scope.model_copy(update={"dataset": "social"}), visitor)
        run.base_filters = scope.model_dump(mode="json")
        return run

    monkeypatch.setattr(agent, "run", social_run)
    monkeypatch.setattr(service, "_answer_evidence", lambda *_a, **_k: generated_answer(db))
    result = service.answer("Read historical social source records", filters, "synthetic-visitor")
    assert result.status == "answered" and db.state_reads[-1].dataset == "social"
    assert db.state_reads[-1].sponsors == filters.sponsors
