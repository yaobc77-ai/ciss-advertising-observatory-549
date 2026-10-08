"""P18 social accounts over official SDK in-memory legacy JSON-RPC.

Independent synthetic rows are reused from the account tools fixture. These
checks do not exercise stdio, HTTP, PostgreSQL, a real model, or social admission.
"""

import asyncio
import hashlib
import json

import pytest

pytest.importorskip("mcp")
from mcp import Client
from test_social_accounts_tools import catalog, post

from observatory.mcp_server import build_mcp_server
from observatory.models import Filters


def payload(result):
    assert not result.is_error
    data = result.structured_content
    assert json.loads(result.content[0].text) == data
    return data


def test_memory_protocol_lists_bounded_account_filters_grouping_and_resolution():
    tools, _, _ = catalog()
    server = build_mcp_server(tools)

    async def check():
        async with Client(server, mode="legacy") as client:
            listed = {tool.name: tool for tool in (await client.list_tools()).tools}
            statistics = listed["record_statistics"]
            schema = statistics.input_schema
            assert schema["additionalProperties"] is False
            filters = schema["$defs"]["FiltersRequest"]
            assert filters["additionalProperties"] is False
            def resolve(node):
                # Shared list definitions are referenced through $defs.
                while "$ref" in node:
                    node = schema["$defs"][node["$ref"].rsplit("/", 1)[1]]
                return node

            accounts = resolve(resolve(filters["properties"]["accounts"])["anyOf"][0])
            assert accounts["maxItems"] == 20 and resolve(accounts["items"])["maxLength"] == 200
            assert "accounts" in schema["properties"]["group_by"]["anyOf"][0]["enum"]
            resolver = listed["resolve_entity"].input_schema
            assert "account" in resolver["properties"]["entity_type"]["enum"]
            assert statistics.annotations.read_only_hint is True
            assert statistics.annotations.destructive_hint is False

    asyncio.run(check())


def test_memory_protocol_account_filters_and_complete_groups_keep_exact_source_fields():
    tools, db, _ = catalog()
    db.rows.extend(post(f"many-{n}", f"Source account {n}", body="") for n in range(12))
    server = build_mcp_server(tools)

    async def check():
        async with Client(server, mode="legacy") as client:
            filtered = payload(await client.call_tool("record_statistics", {
                "filters": {"accounts": ["Shared Name"]},
            }))
            assert filtered["status"] == "ok"
            assert filtered["collections"][0]["total"] == 2
            assert filtered["collections"][0]["retrievable"] == 1
            assert {(row["record_id"], row["account"], row["platform"]) for row in filtered["records"]} == {
                ("s1", "Shared Name", "X"), ("s2", "Shared Name", "YouTube")}
            grouped = payload(await client.call_tool("record_statistics", {"group_by": "accounts"}))
            counts = {item["name"]: item["count"] for item in grouped["groups"]}
            assert counts == {"Shared Name": 2, "Other Name": 1, "(Unknown)": 1,
                              **{f"Source account {n}": 1 for n in range(12)}}
            assert grouped["collections"][0]["total"] == sum(counts.values()) == 16
            assert len(grouped["records"]) == 10 and len(grouped["groups"]) == 15
            assert {item["dataset"] for item in grouped["groups"]} == {"social"}
            resolved = payload(await client.call_tool("resolve_entity", {
                "entity_type": "account", "query": "Shared Name",
            }))
            assert resolved["candidate_count"] == 1
            assert resolved["candidates"][0]["source_value"] == "Shared Name"
            assert resolved["candidates"][0]["identity_status"] == "source_candidate_not_resolved"
            assert "private" not in json.dumps(grouped)

    asyncio.run(check())


def test_memory_protocol_preserves_trusted_account_on_clear_and_refuses_native_accounts():
    base = Filters(dataset="social", accounts=["Shared Name"], platforms=["X"])
    tools, _, _ = catalog(base)
    native, native_db, _ = catalog(Filters())

    async def check():
        async with Client(build_mcp_server(tools), mode="legacy") as client:
            cleared = payload(await client.call_tool("record_statistics", {
                "filters": {"accounts": [], "platforms": [], "dataset": "all"},
            }))
            assert cleared["status"] == "ok"
            assert cleared["filters"]["accounts"] == ["Shared Name"]
            assert cleared["filters"]["platforms"] == ["X"] and cleared["filters"]["dataset"] == "social"
            assert [row["record_id"] for row in cleared["records"]] == ["s1"]
            escaped = payload(await client.call_tool("record_statistics", {
                "filters": {"accounts": ["Other Name"]},
            }))
            assert escaped["status"] == "clarify" and "collections" not in escaped
        async with Client(build_mcp_server(native), mode="legacy") as client:
            for arguments in ({"filters": {"accounts": ["Shared Name"]}}, {"group_by": "accounts"}):
                refused = payload(await client.call_tool("record_statistics", arguments))
                assert refused["status"] == "clarify" and "collections" not in refused
            assert native_db.reads == []

    asyncio.run(check())


def test_memory_protocol_source_reads_keep_current_account_platform_and_version_scope():
    tools, db, _ = catalog(Filters(dataset="social", accounts=["Shared Name"], platforms=["X"]))
    server = build_mcp_server(tools)

    async def check():
        async with Client(server, mode="legacy") as client:
            record = payload(await client.call_tool("get_record", {"record_id": "s1"}))
            assert record["status"] == "ok"
            assert record["record"]["account"] == "Shared Name" and record["record"]["platform"] == "X"
            expected_hash = hashlib.sha256(db.rows[0]["body"].encode()).hexdigest()
            assert record["source_refs"] == [{"record_id": "s1", "version_id": "v-s1",
                "body_hash": expected_hash, "start": 0, "end": len(db.rows[0]["body"])}]
            sources = payload(await client.call_tool("get_record_sources", {"record_id": "s1"}))
            assert sources["source_refs"][0]["verification_status"] == "body_hash_matched"
            assert sources["record"]["company_affiliation"] == "Company affiliation"
            assert "not proof of paid sponsorship" in sources["record"]["relation_note"]
            for identifier in ("s2", "s3"):
                refused = payload(await client.call_tool("get_record_sources", {"record_id": identifier}))
                assert refused["status"] == "clarify" and "source_refs" not in refused
            assert "raw" not in json.dumps(sources) and "file:///private" not in json.dumps(sources)

    asyncio.run(check())
