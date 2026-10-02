"""Offline corpus-gap web tests; every provider and budget operation is fake."""

import copy
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_research_agent import FakeBudget

from observatory.budget import LimitReached
from observatory.claims_source_search import MODEL, RESERVATION_USD, source_search_price
from observatory.config import Settings
from observatory.models import Filters
from observatory.web_research import MAX_SOURCES, WebResearch, WebResearchRequest

QUESTION = "How does Shell describe carbon capture in its native advertisements?"
URL = "https://www.example.org/advertising/quest"
MARKER = "[source]"
PARAPHRASE = "The advertisement says Shell presents its Quest project as a carbon capture example. "


def response(*, text=None, annotations=None, sources=None):
    text = text if text is not None else PARAPHRASE + MARKER
    if annotations is None:
        start = text.find(MARKER)
        annotations = [{"type": "url_citation", "url": URL, "title": "Quest advertisement",
                        "start_index": start, "end_index": start + len(MARKER)}] if start >= 0 else []
    return {
        "id": "resp-web", "model": MODEL, "status": "completed",
        "usage": {"input_tokens": 120, "output_tokens": 40,
                  "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 10}},
        "output": [
            {"id": "search-1", "type": "web_search_call", "status": "completed",
             "action": {"type": "search", "sources": sources or []}},
            {"type": "message", "content": [{"type": "output_text", "text": text,
                                                "annotations": annotations}]},
        ],
    }


def harness(answer=None, *, enabled=True, settings=None, scope=None, budget=None):
    calls = []

    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if isinstance(answer, Exception):
            raise answer
        return copy.deepcopy(answer if answer is not None else response())

    rag = SimpleNamespace(settings=settings or Settings(), budget=budget or FakeBudget(),
                          client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    return WebResearch(rag, enabled=enabled, base_filters=scope), rag, calls


@pytest.fixture(autouse=True)
def deterministic_language(monkeypatch):
    monkeypatch.setattr("observatory.web_research.language_hint", lambda _text: {"code": "en"})
    monkeypatch.setattr("observatory.web_research.check_claim_languages",
                        lambda _texts, _target, **_kwargs: {"status": "match"})


def test_disabled_adapter_does_not_reserve_or_dispatch():
    adapter, rag, calls = harness(enabled=False)
    result = adapter.call({"question": QUESTION})
    assert result["status"] == "disabled"
    assert result["model_calls"] == 0 and not calls and not rag.budget.reserved


@pytest.mark.parametrize("args", [
    {}, None, [], {"question": ""}, {"question": " "}, {"question": "a" * 2001},
    {"question": True}, {"question": "bad\x00text"},
    {"question": QUESTION, "url": URL}, {"question": QUESTION, "filters": {}},
    {"question": QUESTION, "missing_topics": [" "]},
    {"question": QUESTION, "missing_topics": ["a" * 161]},
    {"question": QUESTION, "missing_topics": ["topic"] * 5},
    {"question": QUESTION, "missing_topics": [True]},
])
def test_strict_request_rejected_before_io(args):
    adapter, rag, calls = harness()
    result = adapter.call(args)
    assert result["status"] == "invalid_request"
    assert not calls and not rag.budget.reserved


def test_single_dispatch_preserves_trusted_scope_and_settles_actual_tool_fee():
    scope = Filters(publishers=["The Atlantic"], date_presence="missing")
    adapter, rag, calls = harness(scope=scope)
    scope.publishers.append("should not expand copied scope")
    result = adapter.call({"question": QUESTION, "missing_topics": ["Shell", "Shell"]}, visitor="visitor-1")
    assert result["status"] == "ok" and len(calls) == 1
    request = calls[0]
    assert request["model"] == MODEL and request["store"] is False
    assert request["max_tool_calls"] == 1 and request["tool_choice"] == "required"
    assert request["tools"] == [{"type": "web_search", "search_context_size": "low"}]
    assert request["include"] == ["web_search_call.action.sources"]
    assert request["max_output_tokens"] == 1600 and request["reasoning"] == {"effort": "none"}
    payload = json.loads(request["input"][1]["content"])
    assert payload["question"] == QUESTION and payload["missing_topics"] == ["Shell"]
    assert payload["trusted_local_scope"]["publishers"] == ["The Atlantic"]
    assert result["filters"]["date_presence"] == "missing"
    assert rag.budget.reserved[0][1:] == (RESERVATION_USD, "visitor-1", "generation", MODEL)
    assert result["cost_usd"] == float(source_search_price(120, 40, 20, 10))
    assert rag.budget.settled[0][1] == Decimal("0.0100689")
    assert result["audit"]["state"] == "settled" and not rag.budget.uncertain_calls
    audit = rag.budget.settled[0][2]["observatory_request"]
    assert audit["stage"] == "external_web_research" and audit["search_actions"] == 1
    assert QUESTION not in str(audit)


def test_only_cited_paragraphs_are_retained_as_paraphrases():
    cited = PARAPHRASE + MARKER
    answer = response(text="Invented unsupported conclusion.\n\n" + cited + "\n\nUncited conclusion.")
    adapter, _, _ = harness(answer)
    result = adapter.call({"question": QUESTION})
    assert result["summary"] == PARAPHRASE + "[W1]"
    assert result["dropped_uncited_paragraphs"] == 2
    assert result["passages"][0]["text_kind"] == "model_paraphrase"
    assert result["passages"][0]["source_ids"] == ["W1"]
    assert result["sources"][0]["source_kind"] == "external_web"
    assert result["sources"][0]["fact_status"] == "not_verified"
    assert result["sources"][0]["corpus_membership"] == "not_verified"
    assert result["sources"][0]["supports_generated_paragraph"] is True
    assert result["meaning"] and "quotes" not in result["passages"][0]


def test_action_sources_only_do_not_authorize_generated_prose():
    adapter, _, _ = harness(response(text="This prose has no annotation.", sources=[{"type": "url", "url": URL}]))
    result = adapter.call({"question": QUESTION})
    assert result["status"] == "unresolved" and result["summary"] == "" and result["passages"] == []
    assert result["sources"][0]["url"] == URL
    assert result["sources"][0]["supports_generated_paragraph"] is False


@pytest.mark.parametrize("start,end", [(-1, 5), (0, 9999), (4, 3), (3, 3), (True, 3), ("0", 5), (None, 5)])
def test_invalid_annotation_ranges_do_not_bind_prose(start, end):
    adapter, _, _ = harness(response(annotations=[{"type": "url_citation", "url": URL,
                                                  "start_index": start, "end_index": end}]))
    result = adapter.call({"question": QUESTION})
    assert result["status"] == "unresolved" and result["summary"] == "" and result["sources"] == []


@pytest.mark.parametrize("url", ["file:///private/key", "javascript:alert(1)", "http://localhost/page",
                                "http://127.0.0.1/private", "https://user:password@example.org/page",
                                "http://10.0.0.1/page", "http://[::1]/page", "https://example.org/with space"])
def test_unsafe_provider_urls_are_not_sources(url):
    answer = response()
    answer["output"][1]["content"][0]["annotations"][0]["url"] = url
    answer["output"][0]["action"]["sources"] = [{"type": "url", "url": url}]
    adapter, _, _ = harness(answer)
    result = adapter.call({"question": QUESTION})
    assert result["status"] == "unresolved" and result["sources"] == [] and not result["summary"]


def test_prose_urls_are_not_promoted_to_references():
    adapter, _, _ = harness(response(text=PARAPHRASE + "https://invented.org/page " + MARKER))
    result = adapter.call({"question": QUESTION})
    assert [s["url"] for s in result["sources"]] == [URL]
    assert "invented.org" not in result["summary"] and "unverified link omitted" in result["summary"]


def test_duplicate_sources_and_multiple_citations_share_stable_ids():
    text = PARAPHRASE + "[a][b]"
    annotations = [{"type": "url_citation", "url": URL, "title": "Title", "start_index": len(PARAPHRASE) + n,
                    "end_index": len(PARAPHRASE) + n + 3} for n in (0, 3)]
    adapter, _, _ = harness(response(text=text, annotations=annotations))
    result = adapter.call({"question": QUESTION})
    assert len(result["sources"]) == 1 and result["passages"][0]["source_ids"] == ["W1"]
    assert result["summary"].endswith("[W1][W1]")


def test_source_cap_drops_a_paragraph_if_any_reference_cannot_be_bound():
    text, annotations = "", []
    for n in range(MAX_SOURCES + 1):
        paragraph = f"A cited research paragraph number {n}. [src]"
        start = len(text) + paragraph.index("[src]")
        annotations.append({"type": "url_citation", "url": f"https://example.org/{n}",
                            "start_index": start, "end_index": start + 5})
        text += paragraph + "\n\n"
    adapter, _, _ = harness(response(text=text, annotations=annotations))
    result = adapter.call({"question": QUESTION})
    assert len(result["sources"]) == MAX_SOURCES and len(result["passages"]) == MAX_SOURCES
    assert result["dropped_uncited_paragraphs"] == 1
    assert "number 5" not in result["summary"]


def test_overlapping_provider_markers_are_rejected():
    answer = response()
    second = copy.deepcopy(answer["output"][1]["content"][0]["annotations"][0])
    second["start_index"] += 1
    answer["output"][1]["content"][0]["annotations"].append(second)
    adapter, _, _ = harness(answer)
    result = adapter.call({"question": QUESTION})
    assert result["status"] == "unresolved" and result["passages"] == []


@pytest.mark.parametrize("malformation", ["refusal", "bad_text", "bad_annotations", "bad_sources", "incomplete", "oversized"])
def test_invalid_content_is_settled_but_not_published(malformation):
    answer = response()
    block = answer["output"][1]["content"][0]
    if malformation == "refusal":
        answer["output"][1]["content"] = [{"type": "refusal", "refusal": "No"}]
    elif malformation == "bad_text":
        block["text"] = None
    elif malformation == "bad_annotations":
        block["annotations"] = {}
    elif malformation == "bad_sources":
        answer["output"][0]["action"]["sources"] = "bad"
    elif malformation == "oversized":
        block["text"] = "a" * 20_001
    else:
        answer["status"] = "incomplete"
    adapter, rag, calls = harness(answer)
    result = adapter.call({"question": QUESTION})
    assert result["status"] == "unavailable" and not result["summary"] and not result["sources"]
    assert result["audit"]["state"] == "settled" and len(rag.budget.settled) == 1
    assert len(calls) == 1 and not rag.budget.uncertain_calls


@pytest.mark.parametrize("malformation", ["missing_usage", "missing_trace", "excess_calls", "unknown_action", "usage_over_limit"])
def test_unobservable_usage_retains_reserved_exposure(malformation):
    answer = response()
    if malformation == "missing_usage":
        answer["usage"] = None
    elif malformation == "missing_trace":
        answer["output"] = answer["output"][1:]
    elif malformation == "excess_calls":
        answer["output"].append(copy.deepcopy(answer["output"][0]))
    elif malformation == "unknown_action":
        answer["output"][0]["action"]["type"] = "other"
    else:
        answer["usage"]["input_tokens"] = 922_001
    adapter, rag, _ = harness(answer)
    result = adapter.call({"question": QUESTION})
    assert result["status"] == "unavailable" and result["audit"]["state"] == "uncertain"
    assert result["cost_usd"] == float(RESERVATION_USD)
    assert not rag.budget.settled and len(rag.budget.uncertain_calls) == 1


def test_language_mismatch_cannot_publish_wrong_language_prose(monkeypatch):
    monkeypatch.setattr("observatory.web_research.check_claim_languages", lambda *_args, **_kwargs: {"status": "mismatch"})
    adapter, rag, _ = harness()
    result = adapter.call({"question": QUESTION})
    assert result["status"] == "unresolved" and result["reason"] == "web_answer_language_mismatch"
    assert result["summary"] == "" and result["passages"] == [] and result["sources"]
    assert len(rag.budget.settled) == 1


def test_timeout_is_not_retried_or_cancelled_and_private_details_do_not_leak():
    adapter, rag, calls = harness(TimeoutError("private credential"))
    result = adapter.call({"question": QUESTION})
    assert result["status"] == "unavailable" and len(calls) == 1
    assert len(rag.budget.uncertain_calls) == 1 and not rag.budget.settled
    assert "private credential" not in str(result)


def test_budget_and_configuration_errors_do_not_dispatch():
    adapter, rag, calls = harness(settings=Settings(generation_model="not-priced"))
    assert adapter.call({"question": QUESTION})["reason"] == "web_model_not_supported"
    assert not rag.budget.reserved and not calls
    adapter, rag, calls = harness()
    rag.client = None
    assert adapter.call({"question": QUESTION})["reason"] == "web_client_unavailable"
    assert not rag.budget.reserved and not calls
    adapter, rag, calls = harness()
    rag.budget.reserve = lambda *_args: (_ for _ in ()).throw(LimitReached("private limit details"))
    result = adapter.call({"question": QUESTION})
    assert result["status"] == "limited" and not calls
    assert "private limit details" not in str(result)


def test_tool_schema_is_explicit_about_external_provenance_and_no_filter_override():
    schema = WebResearchRequest.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"question", "missing_topics"}
    definition = WebResearch.definition()
    assert definition["name"] == "search_external_sources"
    assert "cannot" in definition["description"] and "corpus counts" in definition["description"]
