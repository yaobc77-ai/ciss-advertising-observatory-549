"""Top-level collection gaps use one separate, cited external research attempt.

These are offline route contracts. Provider results and collection reads are
stubs; no API, database connection, or frozen evaluation gold is used.
"""

import pytest
from test_research_fallback import Agent, Web, always, harness

from observatory.models import Filters
from observatory.research_agent import ResearchRun
from observatory.service import WEB_SUPPLEMENT_LEAD


@pytest.mark.parametrize("dataset", ["native", "social", "all"])
@pytest.mark.parametrize(
    ("reason", "question", "message"),
    [
        (
            "unsupported_analysis",
            "Which year has the largest number of ads at this outlet?",
            "This collection tool does not currently support grouping by year.",
        ),
        (
            "compound_question",
            "Compare this sponsor's advertisements before and after 2020.",
            "This collection tool cannot compare two date periods in one result.",
        ),
        (
            "ambiguous_entity",
            "What advertisements were sponsored by Williams?",
            "Several source-listed sponsor names match Williams.",
        ),
    ],
)
def test_collection_clarification_searches_web_once_without_claiming_collection_statistics(
    dataset, reason, question, message,
):
    run = ResearchRun(route="clarify", result={"status": "clarify", "reason": reason, "message": message})
    service, _, web = always(run)

    result = service.answer(question, Filters(dataset=dataset), "reader")

    assert result.status == "answered" and result.answer_mode == "web_supplement"
    assert result.answer.startswith(WEB_SUPPLEMENT_LEAD)
    assert message in result.answer
    assert "Why the collection could not answer:" in result.answer
    assert "[W1] External source - https://example.org/ad" in result.answer
    assert result.external_research["source_kind"] == "external_web"
    assert result.structured_result is None
    assert len(web.calls) == 1
    assert run.base_filters["dataset"] == dataset


def _evidence_service(*, web_status=None, generated=None):
    service, db, web, stages = harness(generated=generated)
    service.settings.research_agent_enabled = True
    service.research_agent = Agent(ResearchRun(
        route="evidence",
        result={"filters": Filters(include_inferred_dates=True).model_dump(mode="json"), "search_query": "emissions proposal"},
    ))
    if web_status:
        class UnsuccessfulWeb(Web):
            def call(self, args, *, visitor):
                self.calls.append((args, visitor))
                return {"status": web_status, "sources": [], "cost_usd": 0.02, "model_calls": 1}

        web = service.web_research = UnsuccessfulWeb()
    return service, db, web, stages


@pytest.mark.parametrize("web_status", ["unresolved", "unavailable", "limited"])
def test_an_unsuccessful_web_attempt_is_not_retried_by_the_top_level_route(web_status):
    service, _, web, _ = _evidence_service(web_status=web_status)

    result = service.answer("How does this sponsor describe emissions reductions?", Filters(), "reader")

    assert len(web.calls) == 1
    assert result.status != "answered"
    assert result.external_research["status"] == web_status
    assert result.cost_usd == pytest.approx(0.023)
    assert "external page" not in result.answer


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("Unverifiable citation", "citation_mismatch"),
        ("Evidence failed original-version validation", "evidence_version_mismatch"),
        ("Citation exceeds short-quote limit", "quote_too_long"),
    ],
)
def test_invalid_source_or_quote_validation_is_not_replaced_by_web_research(failure, reason):
    def reject(_passages):
        raise ValueError(failure)

    service, _, web, _ = _evidence_service(generated=reject)

    result = service.answer("Describe this advertisement's proposal.", Filters(), "reader")

    assert result.status == "service_unavailable" and result.failure_reason == reason
    assert not web.calls
    assert not result.external_research


def test_collection_changed_during_tool_selection_does_not_dispatch_web_research():
    run = ResearchRun(route="clarify", result={"status": "clarify", "message": "No matching source name."})
    service, db, web = always(run)

    class ChangingAgent(Agent):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            db.version = "corpus-v2"
            return result

    service.research_agent = ChangingAgent(run)

    result = service.answer("Who sponsored this article?", Filters(), "reader")

    assert result.status == "service_unavailable"
    assert result.failure_reason == "data_changed_during_tool_research"
    assert not web.calls and not result.external_research


def test_collection_changed_during_web_is_not_promoted_or_searched_again():
    service, db, _, _ = _evidence_service()
    web = service.web_research = Web(db, change_version=True)

    result = service.answer("How does this sponsor describe emissions reductions?", Filters(), "reader")

    assert len(web.calls) == 1
    assert result.status == "service_unavailable"
    assert result.failure_reason in {"data_changed_during_web_research", "data_changed_during_tool_research"}
    assert result.answer_mode != "web_supplement" and not result.citations
    assert "An external page describes" not in result.answer
    assert result.cost_usd == pytest.approx(0.023)


@pytest.mark.parametrize("total", [0, 12])
def test_exact_collection_count_is_complete_without_web_or_content_generation(total):
    data = {
        "status": "ok", "kind": "count", "method": "database", "group_by": None,
        "filters": Filters(include_inferred_dates=True).model_dump(mode="json"),
        "collections": [{"dataset": "native", "total": total}], "groups": [], "records": [],
    }
    service, _, web = always(ResearchRun(route="statistics", result=data))

    result = service.answer("How many stored ads are in the selected outlet?", Filters(), "reader")

    assert result.status == "answered" and result.answer_mode == "statistics"
    assert result.structured_result["collections"][0]["total"] == total
    assert not web.calls and not result.external_research


def test_question_budget_limit_never_dispatches_a_second_paid_route():
    service, _, web = always(ResearchRun(route="limited", failure_reason="research_budget_limit"))

    result = service.answer("Find the sponsors of this publisher.", Filters(), "reader")

    assert result.status == "limited" and result.failure_reason == "research_budget_limit"
    assert not web.calls and not result.external_research


def test_late_web_fallback_is_in_the_combined_trace_and_cost():
    run = ResearchRun(
        route="clarify", cost_usd=0.004,
        model_calls=[{"purpose": "interpretation", "cost_usd": 0.004}],
        tool_trace=[{"tool": "request_clarification", "status": "clarify"}],
        result={"status": "clarify", "reason": "unsupported_analysis", "message": "Unsupported grouping."},
    )
    service, _, web = always(run)

    result = service.answer("Group the archive by year.", Filters(), "reader")

    assert len(web.calls) == 1 and result.cost_usd == pytest.approx(0.024)
    trace = result.research_trace
    assert trace["cost_usd"] == pytest.approx(result.cost_usd)
    assert trace["external_web"]["status"] == "ok"
    assert trace["external_web"]["model_calls"] == 1
    web_tools = [tool for tool in trace["tools"] if tool["tool"] == "search_external_sources"]
    assert len(web_tools) == 1 and web_tools[0]["status"] == "ok"
    assert web_tools[0]["data_refs"] == [{"source_id": "W1", "url": "https://example.org/ad"}]


def test_disabled_web_keeps_a_collection_clarification_without_dispatch():
    run = ResearchRun(route="clarify", result={"status": "clarify", "message": "Unknown source name."})
    service, _, web = always(run, enabled=False)

    result = service.answer("Which outlet published this unknown sponsor?", Filters(), "reader")

    assert result.status == "insufficient_evidence" and result.answer_mode == "clarification"
    assert "Unknown source name." in result.answer
    assert not web.calls
