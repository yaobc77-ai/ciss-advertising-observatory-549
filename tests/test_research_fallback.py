"""Corpus gaps, not outages or aggregates, authorize a separate web lookup."""

from types import SimpleNamespace

import pytest

from observatory.config import Settings
from observatory.models import Answer, Filters
from observatory.research_agent import ResearchRun
from observatory.service import Service


class DB:
    def __init__(self, *, has_evidence=False):
        from test_research_service import evidence

        self.evidence = [evidence()] if has_evidence else []
        self.version = "corpus-v1"
        self.saved = []
        self.reads = []

    def health(self):
        return {"status": "ok", "data_version": self.version, "record_counts": {"native": 2}}

    def public_rows(self, filters):
        return []

    def search(self, query, filters, **kwargs):
        self.reads.append((query, filters, kwargs))
        return self.evidence

    def save_answer(self, *args):
        self.saved.append(args)


class Web:
    def __init__(self, db=None, *, change_version=False):
        self.calls = []
        self.db, self.change_version = db, change_version

    def call(self, args, *, visitor):
        self.calls.append((args, visitor))
        if self.change_version:
            self.db.version = "corpus-v2"
        return {"status": "ok", "source_kind": "external_web", "summary": "An external page describes a proposal. [W1]",
                "sources": [{"source_id": "W1", "title": "External source", "url": "https://example.org/ad"}],
                "cost_usd": 0.02, "model_calls": 1}


def harness(*, enabled=True, has_evidence=False, generated=None):
    db = DB(has_evidence=has_evidence)
    web = Web()
    stages, reservations = [], []

    def generate(_question, passages, _visitor, reservation, **kwargs):
        if generated:
            return generated(passages)
        return Answer(status="insufficient_evidence", answer="Corpus evidence is insufficient.", evidence=passages)

    rag = SimpleNamespace(embed=lambda texts, **kwargs: [[0.0] for _ in texts], generate=generate,
                          budget=SimpleNamespace(reserve=lambda *_: "rid", cancel_unsent=reservations.append,
                                                 reservation_cost=lambda _: 0.003))
    service = Service(Settings(web_search_enabled=enabled), db=db, rag=rag, web_research=web)
    return service, db, web, stages


def test_hybrid_miss_searches_web_once_and_keeps_result_outside_corpus_answer():
    service, db, web, stages = harness()
    result = service._answer_evidence("How does Shell discuss hydrogen?", Filters(), "reader", progress=stages.append)
    assert len(db.reads) == 2 and len(web.calls) == 1
    assert result.status == "insufficient_evidence" and not result.citations
    assert result.external_research["status"] == "ok"
    assert "external page" not in result.answer
    assert result.cost_usd == pytest.approx(0.023)
    assert stages == ["database", "organizing", "citations", "web", "citations"]
    assert len(db.saved) == 1


def test_local_hits_without_semantic_support_can_search_but_supported_answer_does_not():
    service, _, web, _ = harness(has_evidence=True)
    assert service._answer_evidence("Describe hydrogen", Filters(), "reader").external_research["status"] == "ok"
    service.rag.generate = lambda _q, passages, _v, **kwargs: Answer(status="answered", answer="Supported local statement.", evidence=passages)
    result = service._answer_evidence("Describe hydrogen", Filters(), "reader")
    assert not result.external_research and len(web.calls) == 1


@pytest.mark.parametrize("filters", [Filters(dataset="social"), Filters(dataset="all"),
                                       Filters(record_ids=["r1"]), Filters(labels=["historical"]),
                                       Filters(date_presence="missing"), Filters(include_inferred_dates=True)])
def test_every_scope_can_be_supplemented_from_the_web(filters):
    # Policy since 2026-10-01: always answer; filters travel as context only.
    service, _, web, _ = harness()
    result = service._answer_evidence("Describe hydrogen", filters, "reader")
    assert result.external_research["status"] == "ok" and len(web.calls) == 1


def test_web_needs_showable_source_links():
    service, _, web, _ = harness()
    service.settings.show_source_links = False
    result = service._answer_evidence("Describe hydrogen", Filters(), "reader")
    assert result.external_research["reason"] == "scope_not_supported" and not web.calls


def test_disabled_web_has_no_dispatch():
    service, _, web, _ = harness(enabled=False)
    result = service._answer_evidence("Describe hydrogen", Filters(), "reader")
    assert result.external_research["status"] == "disabled" and not web.calls


@pytest.mark.parametrize("failure", ["Unverifiable citation", "Evidence failed original-version validation", "Provider down"])
def test_generation_failure_does_not_search_the_web(failure):
    service, _, web, _ = harness(has_evidence=True)

    def fail(*args, **kwargs):
        raise ValueError(failure)

    service.rag.generate = fail
    result = service._answer_evidence("Describe hydrogen", Filters(), "reader")
    assert result.status == "service_unavailable" and not web.calls


def test_database_failure_does_not_search_the_web():
    service, db, web, _ = harness()

    def fail(*args, **kwargs):
        raise RuntimeError("private driver diagnostic")

    db.search = fail
    result = service._answer_evidence("Describe hydrogen", Filters(), "reader")
    assert result.status == "service_unavailable" and not web.calls
    assert "private" not in result.answer


def test_source_change_during_web_retains_cost_but_discards_stale_answer_and_sources():
    service, db, _, _ = harness()
    service.web_research = Web(db, change_version=True)
    result = service._answer_evidence("Describe hydrogen", Filters(), "reader")
    assert result.failure_reason == "data_changed_during_web_research"
    assert result.cost_usd == pytest.approx(0.023) and not result.external_research


def test_zero_database_count_never_calls_web_or_semantic_generator():
    service, _, web, _ = harness()
    service.settings.research_agent_enabled = True
    data = {"status": "ok", "kind": "count", "method": "database", "group_by": None,
            "filters": Filters().model_dump(mode="json"), "collections": [{"dataset": "native", "total": 0}],
            "groups": [], "records": []}
    service.research_agent = SimpleNamespace(run=lambda *_: ResearchRun(route="statistics", result=data))
    result = service.answer("How many ads are stored?", Filters(), "reader")
    assert result.answer_mode == "statistics" and "0 eligible" in result.answer
    assert not web.calls


def test_progress_disconnect_never_changes_answer_accounting():
    service, _, web, _ = harness()

    def disconnected(_stage):
        raise RuntimeError("browser closed")

    result = service._answer_evidence("Describe hydrogen", Filters(), "reader", progress=disconnected)
    assert len(web.calls) == 1 and result.cost_usd == pytest.approx(0.023)


class Agent:
    def __init__(self, run):
        self.result = run

    def run(self, question, filters, visitor, *args, **kwargs):
        self.result.original_question, self.result.base_filters = question, filters.model_dump(mode="json")
        return self.result


def always(run, *, web=None, enabled=True):
    db = DB()
    web = web or Web()
    rag = SimpleNamespace(budget=SimpleNamespace(reserve=lambda *_: "rid", cancel_unsent=lambda *_: None,
                                                 reservation_cost=lambda _: 0.0))
    service = Service(Settings(research_agent_enabled=True, web_search_enabled=enabled), db=db, rag=rag,
                      research_agent=Agent(run), web_research=web)
    return service, db, web


def test_a_clarification_becomes_a_labelled_web_supplemented_answer():
    from observatory.service import WEB_SUPPLEMENT_LEAD

    run = ResearchRun(route="clarify", result={"status": "clarify", "message": "Social data is not loaded."})
    service, db, web = always(run)
    result = service.answer("Which Twitter accounts post the most fossil fuel ads?", Filters(), "reader")
    assert result.status == "answered" and result.answer_mode == "web_supplement"
    assert result.answer.startswith(WEB_SUPPLEMENT_LEAD)
    assert "Social data is not loaded." in result.answer
    assert result.external_research["sources"][0]["url"] == "https://example.org/ad"
    # Text-only clients receive the web findings and their links in the answer itself.
    assert "An external page describes a proposal. [W1]" in result.answer
    assert "[W1] External source - https://example.org/ad" in result.answer
    assert len(web.calls) == 1


def test_limits_and_failed_web_searches_never_invent_an_answer():
    limited = ResearchRun(route="limited", failure_reason="research_budget_limit")
    service, _, web = always(limited)
    assert service.answer("Anything?", Filters(), "reader").status == "limited" and not web.calls

    class FailingWeb(Web):
        def call(self, args, *, visitor):
            self.calls.append(args)
            return {"status": "unresolved", "sources": [], "cost_usd": 0.01}

    run = ResearchRun(route="clarify", result={"status": "clarify", "message": "Unknown name."})
    service, _, web = always(run, web=FailingWeb())
    result = service.answer("Who is Imaginary Oil?", Filters(), "reader")
    assert result.status == "insufficient_evidence" and result.answer_mode == "clarification"
    assert len(web.calls) == 1


def test_disabled_web_keeps_the_collection_answer():
    run = ResearchRun(route="clarify", result={"status": "clarify", "message": "Unknown name."})
    service, _, web = always(run, enabled=False)
    assert service.answer("Who is Imaginary Oil?", Filters(), "reader").status == "insufficient_evidence"
    assert not web.calls


def test_explicit_source_only_dates_survive_the_complete_question_route():
    run = ResearchRun(route="clarify", result={"status": "clarify", "message": "Unknown source."})
    service, _, _ = always(run)
    service.answer("Which source contains this advertisement?", Filters(include_inferred_dates=False), "reader")
    assert run.base_filters["include_inferred_dates"] is False


def test_default_questions_can_use_supplements_without_mutating_the_callers_filters():
    run = ResearchRun(route="clarify", result={"status": "clarify", "message": "Unknown source."})
    service, _, _ = always(run)
    original = Filters()
    service.answer("Which source contains this advertisement?", original, "reader")
    assert run.base_filters["include_inferred_dates"] is True
    assert original.include_inferred_dates is False


def test_late_web_source_change_retains_cost_and_withheld_lookup_trace():
    run = ResearchRun(route="clarify", result={"status": "clarify", "message": "Unknown source."})
    service, db, _ = always(run)
    web = service.web_research = Web(db, change_version=True)
    result = service.answer("Which source contains this advertisement?", Filters(), "reader")
    assert len(web.calls) == 1 and result.status == "service_unavailable"
    assert result.failure_reason == "data_changed_during_web_research"
    assert result.cost_usd == pytest.approx(0.02)
    assert result.research_trace["external_web"]["status"] == "withheld"
    assert result.research_trace["external_web"]["model_calls"] == 1
    assert result.research_trace["tools"][-1]["data_refs"] == []
    assert not result.external_research and not result.citations
    assert db.saved[-1][-1] == "corpus-v1"


def test_web_ok_without_cited_material_is_not_published_as_an_answer():
    run = ResearchRun(route="clarify", result={"status": "clarify", "message": "Unknown source."})
    service, _, web = always(run)

    def no_sources(args, *, visitor):
        web.calls.append(args)
        return {"status": "ok", "summary": "An unsupported answer.", "sources": [], "cost_usd": 0.02}

    web.call = no_sources
    result = service.answer("Which source contains this advertisement?", Filters(), "reader")
    assert result.status == "insufficient_evidence" and len(web.calls) == 1
    assert result.external_research["status"] == "unresolved"
    assert result.external_research["reason"] == "no_cited_answer"
