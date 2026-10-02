"""Real SDK initialize/list/call exchanges through its in-memory transport."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import Client, StdioServerParameters
from test_claims_research import QUOTE, VERSION, claims_catalog
from test_research_tools import make_catalog

from observatory.mcp_server import build_mcp_server, loopback_host, parser, port_number


def test_official_protocol_initializes_lists_read_only_tools_and_calls_sql_fixture():
    catalog, _ = make_catalog()
    server = build_mcp_server(catalog)

    async def check():
        # Legacy mode drives the protocol initialize/initialized handshake and
        # JSON-RPC requests over SDK memory streams; no mock HTTP handler.
        async with Client(server, mode="legacy") as client:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == {item["name"] for item in catalog.mcp_definitions()}
            for tool in listed.tools:
                assert tool.input_schema["additionalProperties"] is False
                assert tool.annotations.read_only_hint is True
                assert tool.annotations.destructive_hint is False
            result = await client.call_tool("record_statistics", {
                "filters": {"publishers": ["The Washington Post"]}, "group_by": "sponsors",
            })
            assert not result.is_error
            payload = result.structured_content
            assert payload["status"] == "ok"
            assert payload["collections"][0]["total"] == 3
            assert {item["name"] for item in payload["groups"]} == {"bp", "exxonmobil", "ExxonMobil"}
            assert json.loads(result.content[0].text) == payload

    asyncio.run(check())


def test_protocol_validation_forbids_extra_keys_paths_and_overlimit_graph_reads():
    catalog, db = make_catalog()
    server = build_mcp_server(catalog)

    async def check():
        async with Client(server, mode="legacy") as client:
            for name, args in [
                ("record_statistics", {"sql": "SELECT secret"}),
                ("get_record", {"record_id": "../private"}),
                ("get_graph_neighborhood", {"limit": 100}),
            ]:
                result = await client.call_tool(name, args)
                assert result.is_error
                if result.structured_content:
                    assert result.structured_content["status"] == "invalid_request"
            assert db.reads == []

    asyncio.run(check())


def test_protocol_graph_source_and_clarification_use_same_catalog_executor():
    catalog, _ = make_catalog()
    server = build_mcp_server(catalog)

    async def check():
        async with Client(server) as client:
            graph = await client.call_tool("get_graph_neighborhood", {"record_id": "r1", "limit": 1})
            assert graph.structured_content["graph"]["records"][0]["record_id"] == "r1"
            assert all(edge["provenance"]["version_id"] == "version-r1"
                       for edge in graph.structured_content["graph"]["edges"])
            ambiguity = await client.call_tool("resolve_entity", {"query": "ExxonMobil", "entity_type": "sponsor"})
            assert not ambiguity.is_error
            assert ambiguity.structured_content["status"] == "clarify"
            schema = await client.call_tool("get_graph_schema", {})
            assert schema.structured_content["claims_status"] == "historical_annotations_are_not_verified_greenwashing"

    asyncio.run(check())


def test_protocol_published_claims_read_exposes_bounded_schema_and_exact_evidence():
    catalog, _, store = claims_catalog()
    server = build_mcp_server(catalog)

    async def check():
        async with Client(server, mode="legacy") as client:
            listed = await client.list_tools()
            tool = next(item for item in listed.tools if item.name == "get_claims_matches")
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            schema = tool.input_schema
            assert schema["additionalProperties"] is False
            assert set(schema["properties"]) == {
                "filters", "nc_ids", "sc_ids", "taxonomy", "review_state", "offset", "limit",
            }
            categories = schema["properties"]["nc_ids"]["anyOf"][0]
            assert categories["maxItems"] == 20
            assert categories["items"]["pattern"] == "^NC_[1-9][0-9]*$"
            result = await client.call_tool("get_claims_matches", {
                "filters": {"record_ids": ["r1"]}, "nc_ids": ["NC_1"], "limit": 1,
            })
            assert not result.is_error
            payload = result.structured_content
            assert payload["claims_version"] == VERSION
            assert payload["records"][0]["claims"][0]["quote"] == QUOTE
            assert payload["source_refs"][0]["body_hash"] == "c" * 64
            assert "not classified negatives" in payload["coverage"]
            assert json.loads(result.content[0].text) == payload
            assert store.calls[0][0].record_ids == ["r1"]

    asyncio.run(check())


def test_protocol_claims_rejects_legacy_category_sql_and_review_shortcuts():
    catalog, _, store = claims_catalog()
    server = build_mcp_server(catalog)

    async def check():
        async with Client(server, mode="legacy") as client:
            for args in ({"nc_ids": ["false_solutions"]}, {"review_state": "verified"},
                         {"sql": "SELECT private"}, {"limit": 21}):
                result = await client.call_tool("get_claims_matches", args)
                assert result.is_error
            assert not store.calls

    asyncio.run(check())


def test_standalone_stdio_process_lists_tools_and_reads_schema_without_a_database():
    async def check():
        parameters = StdioServerParameters(
            command=sys.executable, args=["-X", "utf8", "-m", "observatory.mcp_server"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "OBS_DATABASE_URL": "", "OPENAI_API_KEY": "", "OBS_WEB_SEARCH_ENABLED": "false"},
        )
        async with Client(parameters, mode="legacy", read_timeout_seconds=10) as client:
            listed = await client.list_tools()
            assert len(listed.tools) == 8
            result = await client.call_tool("get_graph_schema", {})
            assert not result.is_error
            assert result.structured_content["schema_version"] == "advertising-source-graph-v2"
            assert result.structured_content["adapters"]["external_fact_check"] == "not_connected"

    asyncio.run(check())


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "8.8.8.8", "example.org", "https://127.0.0.1"])
def test_http_host_cannot_expose_tools_publicly(host):
    with pytest.raises(argparse.ArgumentTypeError):
        loopback_host(host)


def test_cli_defaults_to_stdio_and_only_accepts_loopback_http():
    args = parser().parse_args([])
    assert args.transport == "stdio" and args.host == "127.0.0.1" and args.port == 8051
    assert loopback_host("::1") == "::1"
    assert loopback_host("localhost") == "localhost"
    with pytest.raises(SystemExit):
        parser().parse_args(["--transport", "streamable-http", "--host", "0.0.0.0"])


@pytest.mark.parametrize("port", ["0", "65536", "oops"])
def test_http_port_is_bounded(port):
    with pytest.raises(argparse.ArgumentTypeError):
        port_number(port)
