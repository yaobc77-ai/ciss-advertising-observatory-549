"""Bounded local tool check; paid interpretation runs only with --paid.

This smoke check is not an independent semantic benchmark or client acceptance.
It does not import, relabel or re-index source records.
"""

import argparse
import json
from dataclasses import replace
from pathlib import Path

from observatory.config import Settings
from observatory.models import Filters
from observatory.research_agent import ResearchAgent
from observatory.research_tools import ToolCatalog
from observatory.service import Service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paid", action="store_true")
    args = parser.parse_args()
    service = Service(replace(Settings.from_env(), research_agent_enabled=True))
    filters = Filters()
    catalog = ToolCatalog(service, filters)
    before = service.health()["data_version"]
    checks = {}
    stats = catalog.call("record_statistics", {"filters": {"publishers": ["The Washington Post"]}, "group_by": "sponsors"})
    checks["washington_post"] = {"status": stats["status"], "collections": stats.get("collections"), "groups": stats.get("groups")}
    assert stats["status"] == "ok"
    assert sum(group["count"] for group in stats["groups"]) == stats["collections"][0]["total"]
    record_id = stats["records"][0]["record_id"]
    for tool, arguments in (("get_graph_schema", {}),
                            ("get_graph_neighborhood", {"record_id": record_id, "limit": 1}),
                            ("get_record_sources", {"record_id": record_id}),
                            ("get_record", {"record_id": record_id, "body_limit": 500})):
        result = catalog.call(tool, arguments)
        assert result["status"] == "ok", tool
        checks[tool] = {"status": result["status"], "data_version": result.get("data_version"),
                        "coverage": result.get("coverage"), "record_id": record_id,
                        "source_refs": result.get("source_refs"),
                        "nodes": len(result.get("graph", {}).get("nodes", [])),
                        "edges": len(result.get("graph", {}).get("edges", [])),
                        "source_artifacts": len(result.get("source_artifacts", []))}
    paid = []
    if args.paid:
        # Exactly two API interpretation attempts; no model retries or followups.
        service.research_agent = ResearchAgent(service.rag, catalog, max_steps=1)
        for question, expected_group, expected_entity in (
            ("纽约时报这里收集了几篇原生广告？", None, ("publishers", "The New York Times")),
            ("ExxonMobil 的广告出现在什么媒体？列出每家数量。", "publishers", ("sponsors", "exxonmobil")),
        ):
            result = service.answer(question, filters, "research-tools-smoke-20260929")
            data = result.structured_result or {}
            calls = result.research_trace.get("model_calls", [])
            paid.append({"question": question, "status": result.status, "mode": result.answer_mode,
                         "answer": result.answer, "failure_reason": result.failure_reason,
                         "filters": data.get("filters"), "collections": data.get("collections"),
                         "groups": data.get("groups"), "cost_usd": result.cost_usd,
                         "model_calls": len(calls), "usage": [call.get("usage") for call in calls],
                         "tools": [{"tool": step["tool"], "status": step.get("status")}
                                   for step in result.research_trace.get("tools", [])],
                         "expectation_met": result.status == "answered" and result.answer_mode == "statistics"
                           and data.get("group_by") == expected_group
                           and expected_entity[1] in data.get("filters", {}).get(expected_entity[0], [])})
            if result.status != "answered":
                break  # A provider failure is reported, never silently retried.
        assert sum(item["cost_usd"] for item in paid) < 0.04
    after = service.health()["data_version"]
    assert before == after
    report = {"data_version": after, "source_records_unchanged": True,
              "checks": checks, "paid_smoke": paid,
              "semantic_benchmark": False, "client_acceptance": False}
    target = Path("reports/research_tools_smoke_20260929.json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
