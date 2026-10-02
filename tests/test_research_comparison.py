"""Independent target scopes prevent a larger company's results hiding another."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_research_fallback import Web, harness
from test_research_tools import make_catalog, source

from observatory.models import Answer, Filters


def test_comparison_queries_have_distinct_canonical_targets_and_preserve_date_scope():
    base = Filters(date_from="2020-01-01", publishers=["The Washington Post"])
    catalog, db = make_catalog(base)
    db.rows.append(source("shell", "The Washington Post", "shell"))
    result = catalog.call("search_records", {"query": "emissions", "comparison_scopes": [
        {"query": "ExxonMobil emissions hydrogen", "filters": {"sponsors": ["ExxonMobil"]}},
        {"query": "Shell emissions hydrogen", "filters": {"sponsors": ["shell"]}},
    ]})
    assert result["status"] == "ok"
    assert len(result["retrieval_groups"]) == 2
    for group in result["retrieval_groups"]:
        assert group["filters"]["publishers"] == base.publishers
        assert group["filters"]["date_from"] == "2020-01-01"
        assert len(group["filters"]["sponsors"]) == 1
    assert {item["sponsor"] for item in result["evidence"]} == {"ExxonMobil", "shell"}


@pytest.mark.parametrize("groups", [
    [{"query": "emissions", "filters": None}, {"query": "emissions", "filters": None}],
    [{"query": "emissions", "filters": {"sponsors": ["shell"]}}, {"query": "emissions", "filters": {"sponsors": ["shell"]}}],
    [{"query": "emissions", "filters": {"sponsors": ["not-listed"]}}, {"query": "emissions", "filters": {"sponsors": ["shell"]}}],
])
def test_invalid_or_duplicate_comparison_never_returns_partial_first_target(groups):
    catalog, db = make_catalog()
    db.rows.append(source("shell", "The Washington Post", "shell"))
    result = catalog.call("search_records", {"query": "emissions", "comparison_scopes": groups})
    assert result["status"] == "clarify" and "evidence" not in result


def test_hybrid_comparison_embeddings_and_reads_cover_both_targets_and_missing_target_triggers_web():
    service, db, web, _ = harness(has_evidence=True, generated=lambda p: Answer(status="answered", answer="Supported first-company statement.", evidence=p))
    original_search = db.search
    db.search = lambda query, filters, **kwargs: original_search(query, filters, **kwargs) if filters.sponsors == ["exxonmobil"] else []
    groups = [{"label": name, "query": name + " emissions", "filters": Filters(sponsors=[name.casefold()]).model_dump(mode="json")}
              for name in ["ExxonMobil", "Shell"]]
    result = service._answer_evidence("Compare ExxonMobil and Shell", Filters(), "reader", retrieval_groups=groups)
    assert [group["passages"] for group in result.structured_result["groups"]] == [1, 0]
    assert web.calls[0][0]["missing_topics"] == ["Shell"]
    assert result.status == "insufficient_evidence" and result.external_research["source_kind"] == "external_web"
    assert "comparison is incomplete" in result.answer


def test_mcp_web_ticket_is_single_use_and_cannot_be_forged_or_used_after_source_change():
    catalog, db = make_catalog()
    catalog.service.settings.web_search_enabled = True
    web = Web()
    catalog.service.web_research = web
    db.rows = [source("empty", "The Washington Post", "shell", body="")]
    assert catalog.search_external_sources({"ticket": "f" * 32})["status"] == "invalid_request"
    result = catalog.call("search_records", {"query": "Shell hydrogen"})
    ticket = result["web_fallback_ticket"]
    external = catalog.search_external_sources({"ticket": ticket})
    assert external["status"] == "ok" and len(web.calls) == 1
    assert catalog.search_external_sources({"ticket": ticket})["status"] == "invalid_request"
    next_ticket = catalog.call("search_records", {"query": "Shell hydrogen"})["web_fallback_ticket"]
    db.version = "changed"
    assert catalog.search_external_sources({"ticket": next_ticket})["status"] == "unavailable"
    assert len(web.calls) == 1


def test_actual_mcp_web_tool_declares_cost_and_requires_database_miss():
    mcp = pytest.importorskip("mcp")
    from observatory.mcp_server import build_mcp_server

    catalog, db = make_catalog()
    catalog.service.settings.web_search_enabled = True
    catalog.service.web_research = Web()
    db.rows = [source("empty", "The Washington Post", "shell", body="")]
    server = build_mcp_server(catalog)

    async def check():
        async with mcp.Client(server, mode="legacy") as client:
            listed = await client.list_tools()
            tool = next(t for t in listed.tools if t.name == "search_external_sources")
            assert not tool.annotations.read_only_hint and tool.annotations.open_world_hint
            assert (await client.call_tool("search_external_sources", {"ticket": "f" * 32})).is_error
            local = await client.call_tool("search_records", {"query": "Shell hydrogen"})
            web = await client.call_tool("search_external_sources", {"ticket": local.structured_content["web_fallback_ticket"]})
            assert web.structured_content["source_kind"] == "external_web"
    asyncio.run(check())


def test_concurrent_mcp_retry_consumes_ticket_exactly_once():
    catalog, db = make_catalog()
    catalog.service.settings.web_search_enabled = True
    web = Web()
    catalog.service.web_research = web
    db.rows = [source("empty", "The Washington Post", "shell", body="")]
    ticket = catalog.call("search_records", {"query": "Shell hydrogen"})["web_fallback_ticket"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: catalog.search_external_sources({"ticket": ticket}), range(2)))
    assert sorted(r["status"] for r in results) == ["invalid_request", "ok"]
    assert len(web.calls) == 1
