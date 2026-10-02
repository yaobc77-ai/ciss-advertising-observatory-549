"""Read-only actual-corpus checks for graph donuts, record dots and text rings.

Run from the project root with the project Python environment. Settings reads
the existing private .env; neither configuration nor credentials are printed.
Every database read uses the service's read-only query path, and the injected
NoModel object rejects any accidental generation/embedding access.
"""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import unquote

from observatory.collection_graph_breakdown import (
    breakdown_figure,
    resolve_breakdown_slice,
    selection_breakdown,
)
from observatory.collection_graph_visual import map_elements, visible_map
from observatory.config import Settings
from observatory.knowledge_map import validate_collection_map
from observatory.models import Filters
from observatory.service import Service

FROZEN_DATA_VERSION = "f19d4d697f4e6d2699d2c58b3ce80620b03e02c7e9447f24e012123adcb00f53"
REPORT = Path(__file__).resolve().parents[1] / "reports/graph_breakdowns_smoke_20260929.json"


class NoModel:
    def __init__(self):
        self.attempted_calls = []

    def __getattr__(self, name):
        self.attempted_calls.append(name)
        raise AssertionError("Graph analytics must not access a paid component")


def _check_ring_elements(graph, *, mode="entities", anchor=None, record_ids=None):
    """Check actual SVG dots/ring counts against this view's article membership."""
    nodes, _ = visible_map(graph, mode, anchor, record_ids=record_ids)
    node_ids = {node["id"] for node in nodes}
    all_records = {row["record_id"]: row for row in graph["records"]}
    visible_record_ids = {rid for node in nodes if node["type"] == "Article" for rid in node["record_ids"]}
    elements = map_elements(graph, mode, anchor, record_ids=record_ids)
    rendered = {item["data"]["id"]: item["data"] for item in elements if item["data"]["kind"] == "node"}
    assert set(rendered) == node_ids, "Rendered nodes do not match the documented visible scope"
    checked, dots, ready_total = 0, 0, 0
    for node in nodes:
        ids = set(node["record_ids"])
        if mode == "articles":
            ids.intersection_update(visible_record_ids)
        data = rendered[node["id"]]
        assert data["record_count"] == len(ids)
        assert data["scope_record_count"] == node["record_count"]
        if node["type"] == "Article":
            continue
        ready = sum(all_records[rid]["retrievable"] is True for rid in ids)
        assert data["text_ready_count"] == ready
        assert data["metadata_only_count"] == len(ids) - ready
        uri = data["record_image"]
        assert uri.startswith("data:image/svg+xml;utf8,")
        svg = ET.fromstring(unquote(uri.partition(",")[2]))
        assert svg.attrib["data-record-count"] == str(len(ids))
        assert svg.attrib["data-text-ready"] == str(ready)
        assert svg.attrib["data-metadata-only"] == str(len(ids) - ready)
        actual_dots = len([element for element in svg.iter() if element.attrib.get("class") == "record-dot"])
        assert actual_dots == len(ids), "Each decorative dot must represent exactly one scoped record"
        dash_paths = [element for element in svg.iter() if "stroke-dasharray" in element.attrib]
        assert len(dash_paths) == bool(ready)
        if ready:
            portion, perimeter = (float(number) for number in dash_paths[0].attrib["stroke-dasharray"].split())
            assert math.isclose(portion / perimeter, ready / len(ids), abs_tol=1e-8)
        checked += 1
        dots += actual_dots
        ready_total += ready
    return {"entity_rings_checked": checked, "record_dots_checked": dots,
            "text_ready_memberships_checked": ready_total, "mode": mode,
            "visible_article_records": len(visible_record_ids)}


def _source_value(node):
    return node["properties"]["source_value"]


def _check_selection(service, name, filters):
    graph = service.knowledge_map(filters)
    validate_collection_map(graph)
    stats = service.statistics(filters)
    assert stats["total"] == graph["coverage"]["total_records"]
    assert stats["retrievable"] == graph["coverage"]["retrievable_records"]
    assert stats["unknown_dates"] == sum(not row["date"] for row in graph["records"])
    lookup = {node["id"]: node for node in graph["nodes"]}
    relationships = {(row["sponsor"], row["publisher"]): row["count"] for row in stats["relationships"]}
    records = {row["record_id"]: row for row in graph["records"]}
    details = []
    article_expansions = 0
    category_expansions = 0
    entity_ring_checks = _check_ring_elements(graph)
    for node in graph["nodes"]:
        if node["type"] == "Article":
            continue
        selected = {"kind": "node", "id": node["id"]}
        detail = selection_breakdown(graph, selected)
        assert detail["total_records"] == node["record_count"]
        assert detail["record_ids"] == node["record_ids"]
        assert sum(category["count"] for category in detail["categories"]) == node["record_count"]
        assert math.isclose(sum(category["share"] for category in detail["categories"]), 1.0)
        assert Counter(rid for category in detail["categories"] for rid in category["record_ids"]) == Counter(node["record_ids"])
        trace = breakdown_figure(detail).data[0]
        assert list(trace.values) == [category["count"] for category in detail["categories"]]
        assert list(trace.customdata) == [category["id"] for category in detail["categories"]]
        assert trace.sort is False, "Chart ordering must match the full ranked category list"
        _check_ring_elements(graph, mode="articles", anchor=selected)
        article_expansions += 1
        for category in detail["categories"]:
            assert category["share"] == category["count"] / detail["total_records"]
            assert resolve_breakdown_slice(graph, selected, category["id"]) == category
            assert resolve_breakdown_slice(graph, selected, "client-forged-bucket") is None
            if category["missing"]:
                value = "(Unknown)"
                assert category["node_id"] is category["edge_id"] is category["selection"] is None
                field = "publisher" if node["type"] == "SponsorCandidate" else "sponsor"
                assert all(not records[rid][field] for rid in category["record_ids"])
            else:
                target = lookup[category["node_id"]]
                value = _source_value(target)
                edge = next(item for item in graph["summary_edges"] if item["id"] == category["edge_id"])
                assert edge["record_ids"] == category["record_ids"]
                assert edge["count"] == category["count"]
            pair = (_source_value(node), value) if node["type"] == "SponsorCandidate" else (value, _source_value(node))
            assert relationships[pair] == category["count"], "Donut category disagrees with same-filter SQL cross-tab"
            _check_ring_elements(graph, mode="articles", anchor=selected, record_ids=category["record_ids"])
            category_expansions += 1
        details.append({"node_id": node["id"], "type": node["type"], "label": node["label"],
                        "source_value": _source_value(node), "total_records": detail["total_records"],
                        "unknown_dates": detail["unknown_date_records"],
                        "categories": [{key: category[key] for key in (
                            "id", "label", "source_value", "count", "share", "missing", "record_ids", "node_id", "edge_id",
                        )} for category in detail["categories"]]})
    relations_checked = 0
    for edge in [*graph["summary_edges"], *graph["article_edges"]]:
        detail = selection_breakdown(graph, {"kind": "edge", "id": edge["id"]})
        assert detail["record_ids"] == edge["record_ids"]
        assert sum(category["count"] for category in detail["categories"]) == len(edge["record_ids"])
        assert math.isclose(sum(category["share"] for category in detail["categories"]), 1.0)
        for endpoint in detail["endpoint_shares"]:
            assert endpoint["total_records"] == lookup[endpoint["node_id"]]["record_count"]
            assert endpoint["share"] == len(edge["record_ids"]) / endpoint["total_records"]
        relations_checked += 1
    for article in [node for node in graph["nodes"] if node["type"] == "Article"]:
        detail = selection_breakdown(graph, {"kind": "node", "id": article["id"]})
        assert detail["record_ids"] == article["record_ids"]
        assert detail["total_records"] == 1 and detail["categories"][0]["share"] == 1.0
    if not graph["records"]:
        assert selection_breakdown(graph, {"kind": "node", "id": "stale-selection"}) is None
        assert not breakdown_figure(None).data
    _check_ring_elements(graph, mode="articles")
    return {"selection": name, "filters": filters.model_dump(mode="json"),
            "counting_unit": "eligible_native_record", "counts": graph["counts"], "coverage": graph["coverage"],
            "stats_agree": True, "all_entity_partitions_checked": len(details),
            "all_relation_breakdowns_checked": relations_checked,
            "all_article_breakdowns_checked": graph["counts"]["articles"],
            "article_expansions_checked": article_expansions,
            "category_article_expansions_checked": category_expansions,
            "ring_checks": entity_ring_checks, "entity_details": details}, graph


def _customer_examples(graph):
    def node_detail(kind, value):
        node = next((node for node in graph["nodes"] if node["type"] == kind
                     and _source_value(node) == value), None)
        return selection_breakdown(graph, {"kind": "node", "id": node["id"]}) if node else None

    output = {}
    for name, kind, value in (
        ("new_york_times_native_ads", "Outlet", "The New York Times"),
        ("exxonmobil_publishers", "SponsorCandidate", "exxonmobil"),
        ("washington_post_sponsors", "Outlet", "The Washington Post"),
    ):
        detail = node_detail(kind, value)
        output[name] = {"available": bool(detail), "count": detail["total_records"] if detail else 0,
                        "related_categories": [{"label": item["label"], "count": item["count"],
                                                "percent": item["share"] * 100}
                                               for item in detail["categories"]] if detail else []}
    return output


def main():
    no_model = NoModel()
    service = Service(Settings.from_env(), rag=no_model)
    before = service.health()
    assert before["status"] == "ok", "Project database must be available for actual-corpus verification"
    complete = service.knowledge_map(Filters())
    years = Counter(row["date"][:4] for row in complete["records"] if row["date"])
    chosen_year = max(years, key=lambda year: (years[year], year)) if years else str(date.today().year)
    selections = [
        ("complete", Filters()),
        ("exxonmobil", Filters(sponsors=["exxonmobil"])),
        ("washington_post", Filters(publishers=["The Washington Post"])),
        ("new_york_times", Filters(publishers=["The New York Times"])),
        ("exxonmobil_at_washington_post", Filters(sponsors=["exxonmobil"], publishers=["The Washington Post"])),
        ("known_dates", Filters(include_unknown_dates=False)),
        ("filtered_year", Filters(date_from=date(int(chosen_year), 1, 1), date_to=date(int(chosen_year), 12, 31),
                                  include_unknown_dates=False)),
        ("empty", Filters(publishers=["unavailable-outlet"])),
    ]
    results = []
    for name, filters in selections:
        result, graph = _check_selection(service, name, filters)
        results.append(result)
        if name == "complete":
            complete = graph
    examples = _customer_examples(complete)
    frozen_matches = before["data_version"] == FROZEN_DATA_VERSION
    if frozen_matches:
        assert examples["new_york_times_native_ads"]["count"] == 19
        assert examples["exxonmobil_publishers"]["count"] == 15
        assert len(examples["exxonmobil_publishers"]["related_categories"]) == 4
        assert examples["washington_post_sponsors"]["count"] == 18
        assert len(examples["washington_post_sponsors"]["related_categories"]) == 7
        pair = next(result for result in results if result["selection"] == "exxonmobil_at_washington_post")
        assert pair["coverage"]["total_records"] == 5
    after = service.health()
    assert before == after, "Data/index health changed during read-only checks; rerun against a stable snapshot"
    assert not no_model.attempted_calls
    payload = {"status": "passed", "checked_at_utc": datetime.now(UTC).isoformat(),
               "no_model_calls": True, "attempted_paid_component_calls": no_model.attempted_calls,
               "read_only_service_paths": True, "health_before": before, "health_after": after,
               "source_data_and_index_health_unchanged": True,
               "frozen_customer_example_snapshot_matches": frozen_matches,
               "customer_examples": examples,
               "limitations": [
                   "Checks establish record count/source-path consistency and SVG count encoding, not semantic answer accuracy or client acceptance.",
                   "Text-ready rings use stored retrievable flags; they do not certify complete extraction or greenwashing claim truth.",
                   "Graph and SQL statistics use the same filters in separate read-only snapshots, with unchanged health before and after.",
               ], "results": results}
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "passed", "selections": len(results),
                      "entity_donuts_checked": sum(result["all_entity_partitions_checked"] for result in results),
                      "relation_breakdowns_checked": sum(result["all_relation_breakdowns_checked"] for result in results),
                      "complete_counts": results[0]["counts"], "no_model_calls": True,
                      "source_data_and_index_health_unchanged": True,
                      "report": str(REPORT)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
