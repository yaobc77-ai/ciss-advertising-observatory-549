"""Offline discovery/accounting tests; no live search or source associations."""

import asyncio
import copy
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_research_agent import FakeBudget

from observatory.budget import LimitReached
from observatory.claims_source_search import (
    MODEL,
    RESERVATION_USD,
    TOOL_NAME,
    ClaimsSourceSearch,
    SourceSearchRequest,
    source_search_price,
)
from observatory.config import Settings
from observatory.models import Filters

EXCERPT = "Our carbon capture project plans to reduce emissions across the industrial region."
URL = "https://www.example.org/advertising/carbon-capture"


def response(*, sources=None, annotations=None, report="Possible original article; review the full text.",
             status="completed", usage=None):
    return {
        "id": "resp-search", "model": MODEL, "status": status,
        "usage": usage if usage is not None else {
            "input_tokens": 120, "output_tokens": 40,
            "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 10},
        },
        "output": [
            {"id": "search-1", "type": "web_search_call", "status": "completed",
             "action": {"type": "search", "query": EXCERPT, "sources": sources or []}},
            {"type": "message", "content": [{"type": "output_text", "text": report,
                                               "annotations": annotations or []}]},
        ],
    }


def local_candidate():
    return {"source_kind": "local_current_article", "source_association": "needs_review",
            "record_id": "record-a", "version_id": "version-a", "body_hash": "a" * 64,
            "url": URL, "title": "Carbon capture", "publisher": "Publisher",
            "quote": EXCERPT, "locations": [{"start": 5, "end": 5 + len(EXCERPT)}],
            "location_count": 1, "locations_complete": True, "location_status": "exact_original_characters"}


def harness(answer=None, *, enabled=True, local=None, settings=None, budget=None, scope=None):
    calls, local_reads = [], []

    def lookup(excerpt, limit=5, *, filters=None):
        local_reads.append((excerpt, limit, filters))
        if isinstance(local, Exception):
            raise local
        return copy.deepcopy(local or {
            "status": "ok", "candidates": [], "literal_record_matches": 0,
            "scanned_records": 0, "scan_complete": True, "candidate_limit": 5,
        })

    def create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if isinstance(answer, Exception):
            raise answer
        return copy.deepcopy(answer or response())

    rag = SimpleNamespace(settings=settings or Settings(), budget=budget or FakeBudget(),
                          db=SimpleNamespace(claims_source_candidates=lookup),
                          client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    return ClaimsSourceSearch(rag, enabled=enabled, base_filters=scope), rag, calls, local_reads


def test_disabled_adapter_does_not_read_reserve_or_call():
    adapter, rag, calls, reads = harness(enabled=False)
    result = adapter.call({"excerpt": EXCERPT})
    assert result["status"] == "disabled"
    assert result["model_calls"] == 0 and not reads and not calls and not rag.budget.reserved


@pytest.mark.parametrize("args", [
    {}, {"excerpt": "short"}, {"excerpt": " " * 40}, {"excerpt": "a" * 2001},
    {"excerpt": True}, {"excerpt": 45}, {"excerpt": EXCERPT, "url": URL},
    {"excerpt": EXCERPT, "path": "C:/private/key"},
    {"excerpt": EXCERPT, "publisher_hint": " "}, {"excerpt": EXCERPT, "publisher_hint": False},
    {"excerpt": EXCERPT, "allowed_domains": ["https://example.org/page"]},
    {"excerpt": EXCERPT, "allowed_domains": ["localhost"]},
    {"excerpt": EXCERPT, "allowed_domains": ["127.0.0.1"]},
    {"excerpt": EXCERPT, "allowed_domains": ["*.example.org"]},
    {"excerpt": EXCERPT, "allowed_domains": ["example.org:443"]},
    {"excerpt": EXCERPT, "allowed_domains": [True]},
    {"excerpt": EXCERPT, "allowed_domains": ["example.org"] * 11},
    [], None,
])
def test_strict_bounded_request_rejection_before_any_io(args):
    adapter, rag, calls, reads = harness()
    result = adapter.call(args)
    assert result["status"] == "invalid_request"
    assert not reads and not calls and not rag.budget.reserved


def test_local_literal_candidates_skip_paid_search_preserve_scope_and_original_text():
    excerpt = "  " + EXCERPT + "\n"
    candidate = {**local_candidate(), "quote": excerpt, "private_path": "private-should-not-leak"}
    scope = Filters(record_ids=["record-a"], publishers=["Publisher"])
    adapter, rag, calls, reads = harness(local={"status": "ok", "candidates": [candidate],
                                              "literal_record_matches": 1, "scan_complete": True}, scope=scope)
    result = adapter.call({"excerpt": excerpt, "allowed_domains": ["another.org"]})
    assert result["status"] == "ok" and result["source_association"] == "needs_review"
    assert result["excerpt"] == excerpt and result["candidates"][0]["quote"] == excerpt
    assert result["candidates"][0]["version_id"] == "version-a"
    assert "private_path" not in result["candidates"][0]
    assert result["local_status"]["filters"]["record_ids"] == ["record-a"]
    assert reads[0] == (excerpt, 5, scope)
    assert result["model_calls"] == 0 and result["cost_usd"] == 0 and not calls and not rag.budget.reserved


def test_local_exact_match_does_not_require_a_supported_hosted_model():
    adapter, rag, calls, _ = harness(settings=Settings(generation_model="not-priced"),
                                   local={"status": "ok", "candidates": [local_candidate()]})
    assert adapter.call({"excerpt": EXCERPT})["status"] == "ok"
    assert not calls and not rag.budget.reserved


@pytest.mark.parametrize("dataset", ["social", "all"])
def test_non_native_scope_does_not_expand_local_reads(dataset):
    adapter, _, calls, reads = harness(scope=Filters(dataset=dataset))
    result = adapter.call({"excerpt": EXCERPT})
    assert not reads and len(calls) == 1
    assert result["local_status"]["reason"] == "local_discovery_requires_native_scope"
    assert result["local_status"]["filters"]["dataset"] == dataset


def test_local_read_failure_preserved_without_serializing_exception_and_web_may_continue():
    adapter, _, calls, _ = harness(local=RuntimeError("postgres://private-password"))
    result = adapter.call({"excerpt": EXCERPT})
    assert result["local_status"]["status"] == "unavailable" and len(calls) == 1
    assert "private-password" not in str(result)


def test_hosted_search_request_and_settlement_use_annotated_sources_and_actual_fee():
    result_response = response(
        sources=[{"type": "url", "url": URL}, {"type": "url", "url": "https://example.org/second"}],
        annotations=[{"type": "url_citation", "url": URL, "title": "Original title"}],
        report="Possible page. https://invented.org/unannotated must not become a candidate.",
    )
    adapter, rag, calls, _ = harness(result_response)
    result = adapter.call({"excerpt": EXCERPT, "publisher_hint": "Publisher",
                           "allowed_domains": ["EXAMPLE.ORG"]})
    assert len(calls) == 1
    request = calls[0]
    assert request["model"] == MODEL and request["store"] is False
    assert request["max_tool_calls"] == 1 and request["tool_choice"] == "required"
    assert request["include"] == ["web_search_call.action.sources"]
    assert request["max_output_tokens"] == 1600 and request["service_tier"] == "default"
    assert request["tools"] == [{"type": "web_search", "search_context_size": "low",
                                  "filters": {"allowed_domains": ["example.org"]}}]
    assert result["status"] == "ok" and result["source_association"] == "needs_review"
    assert [row["url"] for row in result["candidates"]] == [URL, "https://example.org/second"]
    assert result["candidates"][0]["title"] == "Original title"
    assert result["candidates"][1]["title"] == ""
    assert all(row["source_kind"] == "external_search" for row in result["candidates"])
    assert result["model_calls"] == 1 and result["audit"]["state"] == "settled"
    assert rag.budget.reserved[0][1:] == (Decimal("0.47388"), "claims-source-maintenance", "generation", MODEL)
    cost = source_search_price(120, 40, 20, 10)
    assert rag.budget.settled[0][1] == cost and result["cost_usd"] == float(cost)
    audit = rag.budget.settled[0][2]["observatory_request"]
    assert audit["stage"] == "claims_source_search" and audit["search_actions"] == 1
    assert EXCERPT not in str(audit) and "model_report" not in audit
    assert not rag.budget.uncertain_calls


def test_multiple_message_blocks_citation_sources_deduplicate_and_stop_at_five():
    answer = response(sources=[{"type": "url", "url": f"https://example.org/{i}"} for i in range(10)])
    answer["output"].append({"type": "message", "content": [
        {"type": "output_text", "text": "Second block", "annotations": [
            {"type": "url_citation", "url": "https://example.org/3", "title": "Third"},
        ]},
    ]})
    adapter, _, _, _ = harness(answer)
    result = adapter.call({"excerpt": EXCERPT})
    assert len(result["candidates"]) == 5
    assert result["candidates"][0]["url"] == "https://example.org/3"
    assert result["candidates"][0]["title"] == "Third"
    assert "Second block" in result["model_report"]


@pytest.mark.parametrize("url", ["file:///C:/private", "javascript:alert(1)", "https://name:secret@example.org",
                                "http://localhost/foo", "http://127.0.0.1/foo", "http://10.0.0.1/foo",
                                "http://[::1]/foo", "https://example.org/with space"])
def test_nonpublic_or_unsafe_provider_url_is_not_promoted(url):
    adapter, rag, _, _ = harness(response(sources=[{"type": "url", "url": url}]))
    result = adapter.call({"excerpt": EXCERPT})
    assert result["status"] == "unresolved" and result["candidates"] == []
    assert len(rag.budget.settled) == 1


def test_domain_allowlist_is_applied_to_external_candidate_urls():
    adapter, _, _, _ = harness(response(sources=[
        {"type": "url", "url": "https://sub.example.org/yes"},
        {"type": "url", "url": "https://example.org.evil.org/no"},
    ]))
    result = adapter.call({"excerpt": EXCERPT, "allowed_domains": ["example.org"]})
    assert [row["url"] for row in result["candidates"]] == ["https://sub.example.org/yes"]


@pytest.mark.parametrize("malformation", ["bad_text", "bad_annotations", "refusal", "incomplete", "bad_sources"])
def test_known_usage_and_action_fees_settle_even_when_content_is_invalid(malformation):
    answer = response()
    if malformation == "bad_text":
        answer["output"][1]["content"][0]["text"] = None
    elif malformation == "bad_annotations":
        answer["output"][1]["content"][0]["annotations"] = {}
    elif malformation == "refusal":
        answer["output"][1]["content"] = [{"type": "refusal", "refusal": "No"}]
    elif malformation == "incomplete":
        answer["status"] = "incomplete"
    else:
        answer["output"][0]["action"]["sources"] = "bad"
    adapter, rag, calls, _ = harness(answer)
    result = adapter.call({"excerpt": EXCERPT})
    assert result["status"] == "unavailable" and result["candidates"] == []
    assert result["audit"]["state"] == "settled"
    assert len(rag.budget.settled) == 1 and not rag.budget.uncertain_calls and len(calls) == 1


@pytest.mark.parametrize("usage", [
    None, {}, {"input_tokens": True, "output_tokens": 1}, {"input_tokens": -1, "output_tokens": 1},
    {"input_tokens": 1, "output_tokens": 1, "input_tokens_details": {"cached_tokens": False}},
    {"input_tokens": 1, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 2}},
    {"input_tokens": 1, "output_tokens": 1, "input_tokens_details": {"cache_write_tokens": -1}},
    {"input_tokens": 922001, "output_tokens": 1}, {"input_tokens": 120, "output_tokens": 1601},
    *[{"input_tokens": 120, "output_tokens": 40, "input_tokens_details": details}
      for details in (None, {}, "bad", {"cached_tokens": 0}, {"cache_write_tokens": 0})],
])
def test_missing_or_malformed_usage_retains_reserved_exposure(usage):
    answer = response()
    answer["usage"] = usage
    adapter, rag, calls, _ = harness(answer)
    result = adapter.call({"excerpt": EXCERPT})
    assert result["status"] == "unavailable" and result["audit"]["state"] == "uncertain"
    assert result["cost_usd"] == float(RESERVATION_USD)
    assert not rag.budget.settled and len(rag.budget.uncertain_calls) == 1 and len(calls) == 1


@pytest.mark.parametrize("malformation", ["missing", "unknown_action", "too_many", "not_list"])
def test_unobservable_search_fee_retains_full_reservation(malformation):
    answer = response()
    if malformation == "missing":
        answer["output"] = answer["output"][1:]
    elif malformation == "unknown_action":
        answer["output"][0]["action"] = {"type": "unknown"}
    elif malformation == "too_many":
        answer["output"].append(copy.deepcopy(answer["output"][0]))
    else:
        answer["output"] = None
    adapter, rag, _, _ = harness(answer)
    result = adapter.call({"excerpt": EXCERPT})
    assert result["audit"]["state"] == "uncertain" and result["cost_usd"] == float(RESERVATION_USD)
    assert not rag.budget.settled and len(rag.budget.uncertain_calls) == 1


@pytest.mark.parametrize("status", ["failed", "searching", "in_progress"])
def test_known_unfinished_search_settles_usage_but_cannot_publish_candidates(status):
    answer = response(sources=[{"type": "url", "url": URL}])
    answer["output"][0]["status"] = status
    adapter, rag, _, _ = harness(answer)
    result = adapter.call({"excerpt": EXCERPT})
    assert result["status"] == "unavailable" and result["candidates"] == []
    assert result["audit"]["state"] == "settled" and len(rag.budget.settled) == 1
    assert not rag.budget.uncertain_calls


@pytest.mark.parametrize("key,value", [("status", None), ("status", "unknown"),
                                      ("id", None), ("id", ""), ("id", {})])
def test_missing_or_invalid_search_metadata_keeps_reservation(key, value):
    answer = response(sources=[{"type": "url", "url": URL}])
    answer["output"][0][key] = value
    adapter, rag, _, _ = harness(answer)
    result = adapter.call({"excerpt": EXCERPT})
    assert result["status"] == "unavailable" and result["candidates"] == []
    assert result["audit"]["state"] == "uncertain"
    assert not rag.budget.settled and len(rag.budget.uncertain_calls) == 1


def test_timeout_dispatched_once_does_not_cancel_or_retry():
    adapter, rag, calls, _ = harness(TimeoutError("private credential detail"))
    result = adapter.call({"excerpt": EXCERPT})
    assert result["status"] == "unavailable" and len(calls) == 1
    assert not rag.budget.settled and len(rag.budget.uncertain_calls) == 1
    assert "private credential" not in str(result)


def test_model_configuration_client_configuration_and_budget_refusal_do_not_dispatch():
    adapter, rag, calls, _ = harness(settings=Settings(generation_model="other-model"))
    assert adapter.call({"excerpt": EXCERPT})["reason"] == "source_search_model_not_supported"
    assert not calls and not rag.budget.reserved
    adapter, rag, calls, _ = harness()
    rag.client = None
    assert adapter.call({"excerpt": EXCERPT})["reason"] == "source_search_client_unavailable"
    assert not calls and not rag.budget.reserved
    adapter, rag, calls, _ = harness()
    rag.budget.reserve = lambda *_args: (_ for _ in ()).throw(LimitReached("limit"))
    assert adapter.call({"excerpt": EXCERPT})["status"] == "limited"
    assert not calls and not rag.budget.reserved


def test_verified_short_long_and_cache_write_prices_plus_action_fee():
    assert source_search_price(1000, 100, 100, 100) == Decimal("0.010307")
    assert source_search_price(272000, 0) == Decimal("0.0644")
    assert source_search_price(272001, 0) == Decimal("0.1188004")
    assert source_search_price(922000, 1600, 0, 922000) == RESERVATION_USD
    with pytest.raises(ValueError):
        source_search_price(10, 0, True)
    with pytest.raises(ValueError):
        source_search_price(10, 0, 11)


def test_request_schema_and_configuration_opt_in(monkeypatch):
    monkeypatch.setattr("observatory.config.load_dotenv", lambda **_kwargs: None)
    monkeypatch.delenv("OBS_CLAIMS_SOURCE_SEARCH_ENABLED", raising=False)
    assert Settings.from_env().claims_source_search_enabled is False
    monkeypatch.setenv("OBS_CLAIMS_SOURCE_SEARCH_ENABLED", "true")
    assert Settings.from_env().claims_source_search_enabled is True
    schema = SourceSearchRequest.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["excerpt"]
    assert set(schema["properties"]) == {"excerpt", "publisher_hint", "allowed_domains"}
    assert schema["properties"]["excerpt"]["maxLength"] == 2000


def test_optional_mcp_tool_is_separate_from_public_catalog_and_has_paid_annotations():
    pytest.importorskip("mcp")
    from mcp import Client
    from test_research_tools import make_catalog

    from observatory.mcp_server import build_mcp_server

    catalog, _ = make_catalog()
    adapter, rag, calls, _ = harness(local={"status": "ok", "candidates": [local_candidate()]})
    assert TOOL_NAME not in {d["name"] for d in catalog.definitions()}

    async def check():
        async with Client(build_mcp_server(catalog), mode="legacy") as client:
            listed = await client.list_tools()
            assert TOOL_NAME not in {tool.name for tool in listed.tools}
        async with Client(build_mcp_server(catalog, source_search=adapter), mode="legacy") as client:
            listed = await client.list_tools()
            tool = next(tool for tool in listed.tools if tool.name == TOOL_NAME)
            assert tool.annotations.read_only_hint is False
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is False
            assert tool.annotations.open_world_hint is True
            assert tool.input_schema["additionalProperties"] is False
            result = await client.call_tool(TOOL_NAME, {"excerpt": EXCERPT})
            assert not result.is_error and result.structured_content["model_calls"] == 0
            invalid = await client.call_tool(TOOL_NAME, {"excerpt": EXCERPT, "url": URL})
            assert invalid.is_error
        assert not calls and not rag.budget.reserved

    asyncio.run(check())


def test_mcp_auto_enabling_adapter_preserves_catalog_native_scope():
    pytest.importorskip("mcp")
    from mcp import Client
    from test_research_tools import make_catalog

    from observatory.mcp_server import build_mcp_server

    catalog, _ = make_catalog()
    _, rag, calls, reads = harness(local={"status": "ok", "candidates": [local_candidate()]})
    catalog.service.settings = Settings(claims_source_search_enabled=True)
    catalog.service.rag = rag
    catalog._base = Filters(record_ids=["record-a"])

    async def check():
        async with Client(build_mcp_server(catalog), mode="legacy") as client:
            result = await client.call_tool(TOOL_NAME, {"excerpt": EXCERPT})
            assert result.structured_content["candidates"][0]["record_id"] == "record-a"
        assert reads[0][2].record_ids == ["record-a"] and not calls

    asyncio.run(check())
