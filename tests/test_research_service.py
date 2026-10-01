"""Service handoff checks for paid intent tools plus the existing grounded RAG."""

from types import SimpleNamespace

import pytest

from observatory.config import Settings
from observatory.models import Answer, Evidence, Filters
from observatory.research_agent import ResearchRun
from observatory.service import Service


class Agent:
    def __init__(self, run):
        self.result = run
        self.calls = []

    def run(self, question, filters, visitor):
        self.calls.append((question, filters, visitor))
        self.result.original_question = question
        self.result.base_filters = filters.model_dump(mode="json")
        return self.result


class Database:
    def __init__(self):
        self.version = "source-v1:index1"
        self.saved = []
        self.searches = []
        self.health_calls = 0

    def health(self):
        self.health_calls += 1
        return {"status": "ok", "data_version": self.version}

    def public_rows(self, _filters):
        return []

    def search(self, query, filters, **kwargs):
        self.searches.append((query, filters, kwargs))
        return [evidence()]

    def save_answer(self, *args):
        self.saved.append(args)


def evidence():
    text = "The sponsor proposes a carbon capture facility."
    return Evidence(evidence_id="e1", record_id="r1", version_id="v1", dataset="native",
                    title="Stored article", text=text, start=0, end=len(text))


def setup_service(run, *, rag=None, db=None):
    db = db or Database()
    agent = Agent(run)
    service = Service(Settings(research_agent_enabled=True), db=db, rag=rag or object(),
                      research_agent=agent)
    return service, db, agent


def no_legacy(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("The model-first path must not use sentence rules")

    monkeypatch.setattr("observatory.service.plan_question", fail)


def statistics(groups=None):
    return {
        "status": "ok", "kind": "count", "method": "database", "group_by": None,
        "filters": Filters(publishers=["The New York Times"]).model_dump(mode="json"),
        "collections": [{"dataset": "native", "total": 19, "retrievable": 14, "unknown_dates": 2}],
        "groups": groups or [], "records": [], "scope_notes": ["Stored records only."],
    }


@pytest.mark.parametrize("question", [
    "Washington Post 有哪些赞助方?", "想知道纽约时报一共收录了几篇", "How many ads come from NYT?",
])
def test_model_mode_bypasses_legacy_intent_rules_and_publishes_only_tool_statistics(monkeypatch, question):
    no_legacy(monkeypatch)
    run = ResearchRun(route="statistics", result=statistics(), cost_usd=0.002)
    service, db, agent = setup_service(run)
    result = service.answer(question, Filters(), "visitor")
    assert result.status == "answered" and result.answer_mode == "statistics"
    assert "19 eligible native ad records" in result.answer
    assert result.structured_result == run.result
    assert result.cost_usd == pytest.approx(0.002)
    assert len(agent.calls) == len(db.saved) == 1
    assert not db.searches
    assert db.saved[0][0] == question
    assert db.saved[0][2] is result
    assert result.research_trace["original_question"] == question
    assert result.research_trace["transport"] == "responses_function_calling"


def test_all_categories_are_preserved_and_unknown_not_called_a_company(monkeypatch):
    no_legacy(monkeypatch)
    data = statistics(groups=[
        {"name": "shell", "count": 3}, {"name": "cera", "count": 2},
        {"name": "(Unknown)", "count": 1},
    ])
    data.update(group_by="sponsors", kind="list_sponsors")
    service, _, _ = setup_service(ResearchRun(route="statistics", result=data))
    result = service.answer("华盛顿邮报有哪些赞助方", Filters(), "visitor")
    assert "2 source-listed sponsors / organizations" in result.answer
    assert result.structured_result["groups"] == data["groups"]
    assert "companies" not in result.answer


def semantic_rag(db, *, change_version=False):
    state = {"embedded": [], "generated": [], "reserved": [], "cancelled": []}

    def reserve(*args):
        state["reserved"].append(args)
        return "semantic-reservation"

    def embed(texts, visitor, cost_sink):
        state["embedded"].append((texts, visitor))
        cost_sink.append(0.001)
        return [[0.0]]

    def generate(question, passages, visitor, reservation):
        state["generated"].append((question, passages, visitor, reservation))
        if change_version:
            db.version = "source-v2:index2"
        return Answer(status="answered", answer="The advertisement describes a proposed facility.",
                      evidence=passages)

    rag = SimpleNamespace(
        embed=embed, generate=generate,
        budget=SimpleNamespace(reserve=reserve, cancel_unsent=state["cancelled"].append,
                               reservation_cost=lambda _rid: 0.01),
    )
    return rag, state


def test_semantic_query_rewrite_is_for_retrieval_and_original_question_for_cited_generation(monkeypatch):
    no_legacy(monkeypatch)
    db = Database()
    rag, state = semantic_rag(db)
    original = "Shell 的广告怎么描述碳捕集？"
    rewrite = "Shell carbon capture planned facility"
    narrowed = Filters(sponsors=["shell"], publishers=["The New York Times"])
    run = ResearchRun(route="evidence", cost_usd=0.002, result={
        "status": "ok", "filters": narrowed.model_dump(mode="json"), "search_query": rewrite,
    })
    service, _, _ = setup_service(run, db=db, rag=rag)
    result = service.answer(original, Filters(), "visitor")
    assert result.status == "answered"
    assert state["embedded"] == [([rewrite], "visitor")]
    assert [row[0] for row in db.searches] == [rewrite, rewrite]
    assert all(row[1] == narrowed for row in db.searches)
    assert state["generated"][0][0] == original
    assert state["generated"][0][3] == "semantic-reservation"
    assert result.cost_usd == pytest.approx(0.013)  # Intent + embedding + grounded generation.
    assert len(db.saved) == 1
    assert db.saved[0][0] == original and db.saved[0][2] is result
    assert not state["cancelled"]


def test_collection_change_withholds_answer_but_preserves_aggregate_paid_cost(monkeypatch):
    no_legacy(monkeypatch)
    db = Database()
    rag, state = semantic_rag(db, change_version=True)
    run = ResearchRun(route="evidence", cost_usd=0.002, result={
        "status": "ok", "filters": Filters().model_dump(mode="json"), "search_query": "carbon capture",
    })
    service, _, _ = setup_service(run, db=db, rag=rag)
    result = service.answer("广告怎么描述碳捕集？", Filters(), "visitor")
    assert result.status == "service_unavailable"
    assert result.failure_reason == "data_changed_during_tool_research"
    assert result.cost_usd == pytest.approx(0.013)
    assert not result.evidence and not result.citations and result.structured_result is None
    assert "proposed facility" not in result.answer
    assert len(state["generated"]) == len(db.saved) == 1
    assert not state["cancelled"]


def test_database_check_failure_after_paid_semantics_preserves_both_costs(monkeypatch):
    no_legacy(monkeypatch)
    db = Database()
    run = ResearchRun(route="evidence", cost_usd=0.002, result={
        "status": "ok", "filters": Filters().model_dump(mode="json"), "search_query": "carbon capture",
    })
    service, _, _ = setup_service(run, db=db)

    def completed_semantics(*_args, **_kwargs):
        def unavailable():
            raise RuntimeError("credential-like database diagnostic")

        db.health = unavailable
        return Answer(status="answered", answer="Paid semantic answer", cost_usd=0.011)

    monkeypatch.setattr(service, "_answer_evidence", completed_semantics)
    result = service.answer("Summarize carbon capture qualifications", Filters(), "visitor")
    assert result.status == "service_unavailable"
    assert result.cost_usd == pytest.approx(0.013)
    assert "credential" not in result.answer and "Paid semantic" not in result.answer
    assert len(db.saved) == 1


@pytest.mark.parametrize("route,status", [("unavailable", "service_unavailable"), ("limited", "limited")])
def test_unavailable_intent_has_no_semantic_or_regex_fallback(monkeypatch, route, status):
    no_legacy(monkeypatch)
    run = ResearchRun(route=route, failure_reason="research_provider_unavailable", cost_usd=0.003)
    service, db, agent = setup_service(run)
    result = service.answer("How many records?", Filters(), "visitor")
    assert result.status == status and result.answer_mode == "tools"
    assert result.failure_reason == "research_provider_unavailable"
    assert result.cost_usd == pytest.approx(0.003)
    assert len(agent.calls) == len(db.saved) == 1
    assert not db.searches
    assert not result.evidence and result.structured_result is None


def test_clarification_retains_user_language_and_no_evidence_generation(monkeypatch):
    no_legacy(monkeypatch)
    run = ResearchRun(route="clarify", result={"message": "请确认公司的名称。"}, cost_usd=0.001)
    service, db, _ = setup_service(run)
    result = service.answer("那家公司有多少广告", Filters(), "visitor")
    assert result.status == "insufficient_evidence" and result.answer_mode == "clarification"
    assert result.answer == "请确认公司的名称。"
    assert result.cost_usd == pytest.approx(0.001)
    assert len(db.saved) == 1 and not db.searches


@pytest.mark.parametrize("route", ["graph", "sources", "record"])
def test_source_and_graph_routes_are_returned_as_tool_data_without_a_second_model(monkeypatch, route):
    no_legacy(monkeypatch)
    data = {"status": "ok", "record": {"record_id": "r1", "version_id": "v1"}}
    run = ResearchRun(route=route, result=data, cost_usd=0.001)
    service, db, _ = setup_service(run)
    result = service.answer("查看原文和关系", Filters(), "visitor")
    assert result.answer_mode == "tools" and result.status == "answered"
    assert result.structured_result == {"kind": route, **data}
    assert len(db.saved) == 1 and not db.searches


def test_blank_question_never_invokes_model_or_audit_database():
    service, db, agent = setup_service(ResearchRun(route="unavailable"))
    result = service.answer("  ", Filters(), "visitor")
    assert result.status == "insufficient_evidence" and result.answer_mode == "clarification"
    assert not agent.calls and not db.saved and db.health_calls == 0


def test_limit_is_reported_as_a_limit_not_an_outage(monkeypatch):
    no_legacy(monkeypatch)
    run = ResearchRun(route="limited", failure_reason="research_budget_limit")
    limited, _, _ = setup_service(run)
    answer = limited.answer("How many records?", Filters(), "visitor").answer
    assert "limit" in answer and "temporarily unavailable" not in answer
    run = ResearchRun(route="unavailable", failure_reason="research_provider_unavailable")
    outage, _, _ = setup_service(run)
    assert "temporarily unavailable" in outage.answer("How many records?", Filters(), "visitor").answer
