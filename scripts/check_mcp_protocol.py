"""Read-only real stdio MCP check against the configured local collection."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters


async def check():
    server = StdioServerParameters(
        command=sys.executable, args=["-m", "observatory.mcp_server", "--dataset", "native"],
        env={**os.environ, "OPENAI_API_KEY": "", "OBS_CLAIMS_SOURCE_SEARCH_ENABLED": "false"},
    )
    async with Client(server, mode="legacy") as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}
        assert names == {
            "record_statistics", "search_records", "resolve_entity", "get_record_sources",
            "get_graph_neighborhood", "get_claims_matches", "get_graph_schema", "get_record",
        }, names
        stats = (await client.call_tool("record_statistics", {
            "filters": {"publishers": ["The Washington Post"]}, "group_by": "sponsors",
        })).structured_content
        assert stats["status"] == "ok"
        assert sum(row["count"] for row in stats["groups"]) == stats["collections"][0]["total"]
        record_id = stats["records"][0]["record_id"]
        graph = (await client.call_tool("get_graph_neighborhood", {"record_id": record_id, "limit": 1})).structured_content
        sources = (await client.call_tool("get_record_sources", {"record_id": record_id})).structured_content
        assert graph["status"] == sources["status"] == "ok"
        assert graph["graph"]["records"][0]["record_id"] == sources["record"]["record_id"]
        claims = (await client.call_tool("get_claims_matches", {
            "filters": {"record_ids": [record_id]}, "limit": 5,
        })).structured_content
        assert claims["status"] == "ok"
        assert len(claims["records"]) <= 5
        assert "category_counts" not in claims
        rejected = await client.call_tool("record_statistics", {"sql": "SELECT secret"})
        assert rejected.is_error
        return {"status": "passed", "transport": "stdio", "real_subprocess": True,
                "database": "configured_local_collection", "model_calls": 0,
                "tools": [tool.name for tool in tools.tools], "data_version": stats["data_version"],
                "statistics": {"collections": stats["collections"], "groups": stats["groups"]},
                "record_id": record_id, "version_id": sources["record"]["version_id"],
                "source_refs": sources["source_refs"], "source_artifacts": len(sources["source_artifacts"]),
                "graph_nodes": len(graph["graph"]["nodes"]), "graph_edges": len(graph["graph"]["edges"]),
                "claims": {"returned_records": len(claims["records"]),
                           "coverage": claims.get("coverage_summary"),
                           "completion_known": False},
                "extra_sql_argument_rejected": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="New JSON receipt path")
    args = parser.parse_args()
    if args.out.exists():
        parser.error("The receipt already exists; choose a new path.")
    report = asyncio.run(check())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
