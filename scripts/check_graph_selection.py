"""Check every selected relationship against the current local stored corpus.

Run with the project Python environment. Configuration stays private; only
loopback databases are accepted. The permitted database methods use read-only
SQL transactions, and injected model components fail on any attribute access.
An existing receipt is never overwritten. No article bodies enter this check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import tomllib
from collections import Counter
from datetime import UTC, date, datetime
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from observatory.collection_graph_breakdown import (
    selection_breakdown,
    selection_relationships,
)
from observatory.collection_graph_visual import map_elements
from observatory.config import Settings
from observatory.db import Database
from observatory.knowledge_map import SUMMARY_PREDICATE, validate_collection_map
from observatory.models import Filters
from observatory.service import Service

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports/graph_selection_consistency_v0_4_2_20260930.json"
SOURCE_FILES = [
    "scripts/check_graph_selection.py",
    *[f"src/observatory/{name}.py" for name in (
        "collection_graph_breakdown", "collection_graph_visual", "knowledge_map",
        "knowledge_graph", "service", "db", "models",
    )],
]


class NoModel:
    def __init__(self):
        self.attempted_calls = []

    def __getattr__(self, name):
        self.attempted_calls.append(name)
        raise AssertionError("Selection checks must not access a model component")


class ReadOnlyDatabase:
    """Limit the service to existing read-only metadata and SQL count methods."""

    def __init__(self, database_url):
        if urlsplit(database_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("A loopback project database is required")
        self.database = Database(database_url)

    def __getattr__(self, name):
        if name not in {"health", "knowledge_map_rows", "dashboard"}:
            raise AssertionError("A database method outside the read-only allowlist was requested")
        return getattr(self.database, name)


def _hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def _check_focus(graph, selected, item, record_ids, counts):
    """Compare helper output with independently selected raw graph witnesses."""
    chosen = set(record_ids)
    nodes = {node["id"]: node for node in graph["nodes"]}
    records = {row["record_id"]: row for row in graph["records"]}
    article_paths = (selected["kind"] == "node" and item["type"] == "Article"
                     or selected["kind"] == "edge" and item["predicate"] != SUMMARY_PREDICATE)
    if article_paths:
        article_id = item["id"] if selected["kind"] == "node" else item["source"]
        candidates = [edge for edge in graph["article_edges"] if edge["source"] == article_id]
    else:
        candidates = [edge for edge in graph["summary_edges"]
                      if selected["kind"] == "edge" and edge["id"] == selected["id"]
                      or selected["kind"] == "node" and selected["id"] in (edge["source"], edge["target"])]
    expected = {edge["id"]: (edge, sorted(chosen.intersection(edge["record_ids"])))
                for edge in candidates if chosen.intersection(edge["record_ids"])}
    focus = selection_relationships(graph, selected, record_ids=sorted(chosen))
    assert focus["record_ids"] == sorted(chosen)
    assert focus["total_records"] == len(chosen)
    assert focus["article_paths"] is article_paths
    actual = {row["id"]: row for row in focus["relations"]}
    assert len(actual) == len(focus["relations"]) and set(actual) == set(expected)
    for identifier, (edge, members) in expected.items():
        row = actual[identifier]
        assert row["record_ids"] == members and row["count"] == len(members)
        assert row["source"] == nodes[edge["source"]]["label"]
        assert row["target"] == nodes[edge["target"]]["label"]
        assert row["predicate"] == edge["predicate"] and row["predicate"] in graph["predicate_definitions"]
        assert row["selected"] is (selected["kind"] == "edge" and selected["id"] == identifier)
        endpoint_nodes = [nodes[edge[key]] for key in ("source", "target")
                          if nodes[edge[key]]["type"] != "Article"]
        assert len(row["endpoint_shares"]) == len(endpoint_nodes)
        for share, node in zip(row["endpoint_shares"], endpoint_nodes, strict=True):
            assert share["label"] == node["label"] and share["count"] == len(members)
            assert share["total_records"] == len(node["record_ids"])
            assert math.isclose(share["share"], len(members) / len(node["record_ids"]))
        counts["relationship_rows_checked"] += 1
        counts[f"predicate_{edge['predicate']}_rows_checked"] += 1
    for field in ("sponsor", "outlet"):
        source_field = "publisher" if field == "outlet" else field
        missing = sum(not records[rid][source_field] for rid in chosen)
        assert focus[f"missing_{field}_records"] == missing
        if missing:
            counts["focuses_with_missing_source_fields"] += 1
    if not article_paths:
        # Each valid pair is witnessed once; missing counterparts create no pair.
        paired = sum(bool(records[rid]["sponsor"] and records[rid]["publisher"]) for rid in chosen)
        assert sum(row["count"] for row in actual.values()) == paired
    elements = map_elements(graph, "articles", selected, record_ids=sorted(chosen))
    article_ids = {element["data"]["id"] for element in elements if element["data"].get("type") == "Article"}
    assert article_ids == {records[rid]["article_node_id"] for rid in chosen}
    assert {element["data"]["id"] for element in elements if element["data"]["kind"] == "edge"} == {
        edge["id"] for edge in graph["article_edges"] if edge["source"] in article_ids
    }
    assert all(element["data"]["record_count"] == 1 for element in elements if element["data"]["kind"] == "edge")
    for element in elements:
        data = element["data"]
        if data["kind"] == "node":
            assert data["record_count"] == len(chosen.intersection(nodes[data["id"]]["record_ids"]))
    counts["relationship_focuses_checked"] += 1
    counts["article_expansions_checked"] += 1
    return focus


def _check_scope(service, name, filters):
    graph = service.knowledge_map(filters)
    validate_collection_map(graph)
    stats = service.statistics(filters)
    assert graph["filters"] == filters.model_dump(mode="json")
    assert stats["total"] == graph["coverage"]["total_records"] == len(graph["records"])
    assert stats["retrievable"] == graph["coverage"]["retrievable_records"]
    assert stats["unknown_dates"] == sum(not row["date"] for row in graph["records"])
    sql_pairs = {(row["sponsor"], row["publisher"]): row["count"] for row in stats["relationships"]}
    nodes = {node["id"]: node for node in graph["nodes"]}
    counts = Counter()
    for kind, field in (("SponsorCandidate", "sponsors"), ("Outlet", "publishers")):
        sql_values = {row["name"]: row["count"] for row in stats[field]}
        for node in graph["nodes"]:
            if node["type"] == kind:
                assert sql_values[node["properties"]["source_value"]] == len(node["record_ids"])
                counts["sql_entity_counts_checked"] += 1
    for edge in graph["summary_edges"]:
        pair = tuple(nodes[edge[key]]["properties"]["source_value"] for key in ("source", "target"))
        assert sql_pairs[pair] == edge["count"] == len(edge["record_ids"])
        counts["sql_summary_counts_checked"] += 1
    selections = [("node", node) for node in graph["nodes"]]
    selections += [("edge", edge) for edge in [*graph["summary_edges"], *graph["article_edges"]]]
    for kind, item in selections:
        selected = {"kind": kind, "id": item["id"]}
        ids = sorted(item["record_ids"])
        detail = selection_breakdown(graph, selected)
        assert detail["record_ids"] == ids and detail["total_records"] == len(ids)
        assert Counter(rid for category in detail["categories"] for rid in category["record_ids"]) == Counter(ids)
        _check_focus(graph, selected, item, ids, counts)
        _check_focus(graph, selected, item, [], counts)
        counts["empty_subsets_checked"] += 1
        counts[f"{item['type'] if kind == 'node' else item['predicate']}_selections_checked"] += 1
        for category in detail["categories"]:
            assert category["count"] == len(category["record_ids"])
            assert math.isclose(category["share"], category["count"] / detail["total_records"])
            _check_focus(graph, selected, item, category["record_ids"], counts)
            counts["breakdown_categories_checked"] += 1
    assert selection_relationships(graph, {"kind": "node", "id": "stale-selection"}) is None
    assert selection_relationships(graph, None) is None
    if not graph["records"]:
        assert not map_elements(graph, "articles")
    return {"scope": name, "filters": filters.model_dump(mode="json"), "counts": graph["counts"],
            "coverage": graph["coverage"], "checks": dict(sorted(counts.items())),
            "same_filter_sql_counts_agree": True}, graph


def _examples(graph):
    examples = {}
    for name, kind, value in (("exxonmobil", "SponsorCandidate", "exxonmobil"),
                              ("washington_post", "Outlet", "The Washington Post")):
        node = next((node for node in graph["nodes"] if node["type"] == kind
                     and node["properties"]["source_value"] == value), None)
        if node is not None:
            focus = selection_relationships(graph, {"kind": "node", "id": node["id"]})
            examples[name] = {"total_records": focus["total_records"],
                              "relations": [{key: row[key] for key in ("source", "predicate", "target", "count", "endpoint_shares")}
                                            for row in focus["relations"]],
                              "missing_sponsor_records": focus["missing_sponsor_records"],
                              "missing_outlet_records": focus["missing_outlet_records"]}
    return examples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=REPORT, help="New receipt path; existing files are rejected")
    args = parser.parse_args()
    if args.report.exists():
        raise FileExistsError("Use a new report path; historical receipts must be preserved")
    hashes_before = _hashes()
    no_model = NoModel()
    settings = Settings.from_env()
    service = Service(settings, db=ReadOnlyDatabase(settings.database_url), rag=no_model, research_agent=no_model)
    before = service.health()
    assert before["status"] == "ok", "The local project database must be available"
    complete = service.knowledge_map(Filters())
    years = Counter(row["date"][:4] for row in complete["records"] if row["date"])
    chosen_year = int(max(years, key=lambda year: (years[year], year))) if years else date.today().year
    scopes = [
        ("complete", Filters()),
        ("exxonmobil", Filters(sponsors=["exxonmobil"])),
        ("washington_post", Filters(publishers=["The Washington Post"])),
        ("exxonmobil_at_washington_post", Filters(sponsors=["exxonmobil"], publishers=["The Washington Post"])),
        ("known_dates", Filters(include_unknown_dates=False)),
        ("filtered_year", Filters(date_from=date(chosen_year, 1, 1), date_to=date(chosen_year, 12, 31), include_unknown_dates=False)),
        ("empty", Filters(publishers=["unavailable-outlet"])),
    ]
    results = []
    for name, filters in scopes:
        result, _ = _check_scope(service, name, filters)
        results.append(result)
    after = service.health()
    assert before == after, "Data/index health changed during the read-only check"
    assert not no_model.attempted_calls
    hashes_after = _hashes()
    assert hashes_before == hashes_after, "Checked source files changed during the check"
    totals = Counter()
    for result in results:
        totals.update(result["checks"])
    checked_at = datetime.now(UTC)
    payload = {
        "status": "passed", "checked_at_utc": checked_at.isoformat(),
        "checked_at_america_new_york": checked_at.astimezone(ZoneInfo("America/New_York")).isoformat(),
        "source_project_version": tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
        "installed_package_metadata_version": version("ciss-observatory"),
        "executed_source_modules": {"service": str(Path(__import__(Service.__module__, fromlist=["__file__"]).__file__).resolve())},
        "local_database_host_guard": "loopback only", "read_only_database_method_allowlist": ["health", "knowledge_map_rows", "dashboard"],
        "no_model_calls": True, "attempted_model_component_calls": no_model.attempted_calls,
        "health_before": before, "health_after": after, "source_data_and_index_health_unchanged": True,
        "checked_source_sha256": hashes_before, "checked_sources_unchanged": True,
        "scopes_checked": len(results), "totals": dict(sorted(totals.items())),
        "entity_examples": _examples(complete), "results": results,
        "limitations": [
            "These are local stored-corpus engineering checks, not customer acceptance, semantic answer accuracy, or confirmation of an online deployment.",
            "The checker verifies relationship helpers and graph elements; sequential browser state transitions are covered separately by UI regressions and browser checks.",
            "Graph witnesses and SQL counts come from separate read-only transactions with identical filters and unchanged health before/after, not one cross-query atomic snapshot.",
            "Exact source values identify candidates; co-listing does not independently verify payment, corporate identity, endorsement, or a business relationship.",
            "No article body, vector, embedding, generated answer, migration, import, or paid model operation is read or created by the checker.",
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_bytes((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({"status": "passed", "scopes_checked": len(results), "totals": payload["totals"],
                      "source_data_and_index_health_unchanged": True, "no_model_calls": True,
                      "report": str(args.report.resolve())}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never echo database/configuration exception text or private values.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": "Selection check failed; no successful receipt was written."}))
        raise SystemExit(1) from None
