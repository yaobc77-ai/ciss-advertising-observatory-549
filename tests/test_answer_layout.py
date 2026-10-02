"""Global query presentation checks with synthetic data and no model calls."""

import json
from types import SimpleNamespace

import pytest
from dash import html
from test_app import SECRET, FakeService, callback, component_tree, research_values
from test_claims_ui import ClaimsService

from observatory.app import (
    _evidence_cards,
    _grounded_answer_content,
    _statistics_card,
    _tools_card,
    create_app,
)
from observatory.date_inference import BASIS_NOTE, UNCHECKED_NOTE
from observatory.models import Filters


def serialized(component):
    return json.dumps(component, default=lambda item: item.to_plotly_json())


def headings(component):
    payload = json.loads(serialized(component))
    return [node["props"].get("children") for node in component_tree(payload)
            if node["type"] == "H4"]


def assert_answer_order(component):
    labels = headings(component)
    assert labels.count("Summary") == labels.count("Evidence") == labels.count("Scope and limits") == 1
    assert labels.index("Summary") < labels.index("Evidence") < labels.index("Scope and limits")


@pytest.mark.parametrize("kind,group_by", [
    ("count", None), ("share", None), ("list", "publishers"), ("list", "sponsors"),
])
def test_statistics_use_common_layout_without_rewriting_database_answer(kind, group_by):
    service = FakeService()
    item = {"dataset": "native", "total": 2, "retrievable": 1, "unknown_dates": 1}
    if kind == "share":
        item.update(numerator=2, denominator=4, percentage=50.0)
    data = {
        "kind": kind, "group_by": group_by, "filters": Filters().model_dump(),
        "denominator_filters": Filters().model_dump(), "collections": [item],
        "groups": [{"dataset": "native", "name": "Recorded category", "count": 2}] if group_by else [],
        "records": [], "scope_notes": ["Unknown source dates are included."],
    }
    result = {"answer": "A deterministic database answer, with its original quantities.",
              "structured_result": data}
    card = _statistics_card(result, True, service)
    assert_answer_order(card)
    text = serialized(card)
    assert result["answer"] in text
    assert "not a census of all advertising" in text
    assert "Unknown source dates are included." in text
    assert service.answer_calls == service.search_calls == []


@pytest.mark.parametrize("kind", ["graph", "record", "sources", "claims"])
def test_each_data_tool_has_a_program_derived_summary_and_traceable_evidence(kind):
    data = {"kind": kind, "filters": Filters().model_dump(), "scope_notes": ["Current selection only."]}
    if kind == "graph":
        data["graph"] = {
            "nodes": [{"id": "r", "label": "Article"}, {"id": "s", "label": "Source sponsor"}],
            "edges": [{"source": "r", "target": "s", "predicate": "source_lists_sponsor",
                       "label": "source lists sponsor", "provenance": {"record_id": "native-a"}}],
        }
    elif kind == "record":
        data["body"] = {"start": 20, "end": 58, "total_characters": 100,
                        "text": "The advertiser describes a proposal."}
    elif kind == "sources":
        data["source_artifacts"] = [{"label": "Original source", "properties": {"url": "https://example.test/source"}}]
    else:
        data.update(ClaimsService().claims_matches(Filters(), limit=1))
    result = {"status": "answered", "answer": "Generic tool boilerplate.", "structured_result": data}
    card = _tools_card(result, True)
    assert_answer_order(card)
    text = serialized(card)
    assert "Current selection only." in text
    assert "Generic tool boilerplate." not in text
    if kind == "graph":
        assert "1 recorded relationship" in text and "on this page" in text
        assert "/records/native-a" in text and "not the complete graph" in text
    elif kind == "record":
        assert "Characters 20" in text and data["body"]["text"] in text
        assert "completeness" in text
    elif kind == "sources":
        assert "1 stored source reference" in text and "https://example.test/source" in text
        assert "not independently verified" in text
    else:
        assert "published CLAIMS2 assignments" in text and "not classified negatives" in text


@pytest.mark.parametrize("warning", [BASIS_NOTE, UNCHECKED_NOTE])
def test_general_rag_findings_keep_server_date_basis_warnings(warning):
    result = {
        "summary": [{"text": "This article describes a planned pilot.", "citation_indices": [1]}],
        "sections": [{"title": "Planned pilot", "citation_indices": [1]}],
        "cited_claims": [{"text": "The advertiser describes a pilot, not an achieved result.", "citation_indices": [1]}],
        "citations": [{"evidence_id": "e", "quote": "planned pilot"}],
        "answer": "A cited statement. [1]\n\n" + warning,
    }
    card = html.Div(_grounded_answer_content(result, result["answer"]))
    text = serialized(card)
    assert "Summary" in headings(card) and "Findings" in headings(card)
    assert "Scope and limits" in headings(card)
    assert warning in text
    assert "What the advertisements emphasize" not in text


def submit_result(result, *, keyword=False, empty_search=False):
    service = FakeService()
    service.answer = lambda *args, **kwargs: result
    if empty_search:
        service.search = lambda *args, **kwargs: []
    settings = SimpleNamespace(show_source_links=True, cookie_secret="answer-layout-tests", monthly_budget_usd=100)
    app = create_app(service, settings)
    values = research_values() | {"answer-paid.n_clicks": 0 if keyword else 1}
    response = callback(app, app.server.test_client(), "research-results.children", values,
                        "search-free.n_clicks" if keyword else "answer-paid.n_clicks")
    return response


@pytest.mark.parametrize("status,mode", [
    ("insufficient_evidence", "clarification"), ("insufficient_evidence", "rag"),
    ("limited", "tools"), ("service_unavailable", "statistics"),
])
def test_nonanswers_show_status_and_next_step_without_false_summary(status, mode):
    result = {"status": status, "answer_mode": mode, "answer": "Please specify the outlet."}
    if status in {"limited", "service_unavailable"}:
        result["answer"] = SECRET
        # A stale structured payload must not bypass a failure state.
        result["structured_result"] = {"kind": "graph", "filters": Filters().model_dump(),
                                       "graph": {"nodes": [{"id": "private", "label": SECRET}], "edges": []}}
    response = submit_result(result)
    labels = [node["props"].get("children") for node in component_tree(response) if node["type"] == "H4"]
    assert "Status" in labels and "Next step" in labels
    assert "Summary" not in labels and "Evidence" not in labels
    assert SECRET not in json.dumps(response)


def test_keyword_search_is_a_scoped_result_summary_not_a_collection_total():
    response = submit_result({}, keyword=True)
    assert_answer_order(response)
    text = json.dumps(response)
    assert "evidence passages found" in text
    assert "not the number of advertisements" in text
    assert "No paid model call" in text


def test_empty_keyword_search_has_an_action_without_asserting_corpus_absence():
    response = submit_result({}, keyword=True, empty_search=True)
    labels = [node["props"].get("children") for node in component_tree(response) if node["type"] == "H4"]
    assert "Status" in labels and "Next step" in labels
    assert "Summary" not in labels
    assert "does not establish" in json.dumps(response)


def test_new_single_article_answer_uses_same_layout_as_statistics_and_tools():
    service = FakeService()
    evidence = service.evidence().model_dump(mode="json")
    result = {
        "status": "answered", "answer_mode": "rag", "answer": "Flat answer. [1]",
        "summary": [{"text": "The retrieved advertisement describes a proposal.", "citation_indices": [1]}],
        "sections": [{"title": "Proposed project", "citation_indices": [1]}],
        "cited_claims": [{"text": "The advertiser describes a proposal.", "citation_indices": [1]}],
        "citations": [{"evidence_id": evidence["evidence_id"], "quote": evidence["text"]}],
        "evidence": [evidence],
    }
    response = submit_result(result)
    assert_answer_order(response)
    text = json.dumps(response)
    assert "Findings" in text and "What the advertisements emphasize" not in text
    assert "#answer-citation-1" in text


def test_web_supplement_has_the_global_layout_without_becoming_collection_evidence():
    result = {
        "status": "answered", "answer_mode": "web_supplement",
        "answer": "This answer uses outside web sources.\n\nWhy the collection could not answer: No stored passages answer the question.",
        "external_research": {"status": "ok", "passages": [
            {"text": "A cited external description. [W1]", "source_ids": ["W1"]},
        ], "sources": [{"source_id": "W1", "title": "Public announcement", "url": "https://example.test/announcement"}]},
    }
    response = submit_result(result)
    assert_answer_order(response)
    text = json.dumps(response)
    assert "Outside the advertising collection" in text and "[W1]" in text
    assert "not stored advertisement quotes" in text
    assert "not been verified against the current collection filters" in text
    assert "No stored passages answer the question." in text
    assert "#answer-citation-" not in text


def test_invalid_web_supplement_does_not_get_a_success_summary():
    response = submit_result({"status": "answered", "answer_mode": "web_supplement",
                              "external_research": {"status": "ok", "passages": [], "sources": []}})
    labels = [node["props"].get("children") for node in component_tree(response) if node["type"] == "H4"]
    assert "Status" in labels and "Summary" not in labels


def test_empty_stored_text_does_not_claim_a_text_interval_was_read():
    card = _tools_card({"structured_result": {"kind": "record", "body": {}}}, True)
    assert_answer_order(card)
    assert "No stored article text is available" in serialized(card)
    assert "are shown from this stored record" not in serialized(card)


def test_old_date_disclosure_is_preserved_after_the_server_changes_its_wording():
    warning = ("Some dates are inferred, not taken from the source data: tier A from a date in "
               "the article URL, tier C from a web search. They are unreviewed estimates.")
    card = html.Div(_grounded_answer_content({"answer": "Saved answer.\n\n" + warning}, "Saved answer."))
    assert warning in serialized(card)


def test_source_specific_date_notice_stays_visible_outside_technical_details():
    evidence = FakeService().evidence().model_dump(mode="json")
    evidence["date_notice"] = "Publication date supplemented from a date in the source URL."
    cards = _evidence_cards([evidence], True)
    components = list(component_tree(json.loads(serialized(cards))))
    notices = [node for node in components if node["type"] == "P"
               and node["props"].get("children") == evidence["date_notice"]]
    assert len(notices) == 1
    for node in components:
        if node["type"] == "Details" and "Technical details" in json.dumps(node):
            assert evidence["date_notice"] not in json.dumps(node)
