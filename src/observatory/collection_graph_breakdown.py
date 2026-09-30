"""Exact count partitions for clicked objects in the advertising source map.

All denominators refer to the current filtered record snapshot. The module
uses witnessed source paths, never text similarity, inferred partnerships or
model classifications. Missing source values remain visible count buckets.
"""

from __future__ import annotations

import colorsys
from collections import defaultdict
from datetime import date
from html import escape

import plotly.graph_objects as go

from .collection_graph_visual import selection_item
from .knowledge_map import SUMMARY_PREDICATE, validate_collection_map

# Colors distinguish categories within the selected breakdown. Assigning the
# palette by sorted canonical IDs keeps it reproducible for the same category
# set even when counts change or the ranked table reorders. These colors do not
# identify graph node types or encode endorsement, opposition or claim truth.
CATEGORY_COLORS = (
    "#537a95", "#b77859", "#6f947e", "#8d7aa3", "#b59855", "#6d969b",
    "#b26f83", "#6c83b1", "#9a8e72", "#809654", "#a776aa", "#528984",
    "#a64c50", "#49639b", "#ac773e", "#775b93", "#687a42", "#436d66",
    "#925b5b", "#8a6f35",
)
MISSING_COLOR = "#aab4bf"
_DIMENSIONS = {
    "SponsorCandidate": ("published_in", "Outlet", "News outlets", "Missing outlet"),
    "Outlet": ("source_lists_sponsor", "SponsorCandidate", "Source-listed sponsors", "Missing sponsor"),
}


def _category_colors(categories):
    """Give every nonmissing bucket a distinct, selection-local color."""
    known_ids = sorted(category["id"] for category in categories if not category["missing"])
    palette = list(CATEGORY_COLORS[:len(known_ids)])
    used = set(palette) | {MISSING_COLOR}
    extra = 0
    while len(palette) < len(known_ids):
        # Golden-angle hue spacing and three lightness bands extend the fixed
        # palette for unusually many categories without repeating hex colors.
        hue = (.17 + extra * .618033988749895) % 1
        red, green, blue = colorsys.hls_to_rgb(hue, .40 + .06 * (extra % 3), .50 + .10 * (extra % 2))
        color = "#" + "".join(f"{round(component * 255):02x}" for component in (red, green, blue))
        extra += 1
        if color not in used:
            palette.append(color)
            used.add(color)
    colors = dict(zip(known_ids, palette, strict=True))
    for category in categories:
        category["color"] = MISSING_COLOR if category["missing"] else colors[category["id"]]


def _index(graph):
    """Validate source paths in addition to the collection summary witnesses."""
    validate_collection_map(graph)
    nodes = {node["id"]: node for node in graph["nodes"]}
    records = {row["record_id"]: row for row in graph["records"]}
    paths, memberships = defaultdict(dict), defaultdict(set)
    for edge in graph["article_edges"]:
        source, target = nodes.get(edge["source"]), nodes.get(edge["target"])
        expected = {"published_in": "Outlet", "source_lists_sponsor": "SponsorCandidate"}.get(edge["predicate"])
        if not source or source["type"] != "Article" or not target or target["type"] != expected:
            raise ValueError("Invalid article source path in count breakdown")
        ids = source["record_ids"]
        if len(ids) != 1 or edge["record_ids"] != ids:
            raise ValueError("Article source path has conflicting record membership")
        record_id = ids[0]
        record = records.get(record_id)
        if not record or record["article_node_id"] != source["id"] or (
            edge["provenance"].get("record_id") != record_id
            or edge["provenance"].get("version_id") != record["version_id"]
        ):
            raise ValueError("Article source path has conflicting record/version provenance")
        if edge["predicate"] in paths[source["id"]]:
            raise ValueError("Ambiguous article source path in count breakdown")
        paths[source["id"]][edge["predicate"]] = edge
        memberships[target["id"]].add(record_id)
    for node in nodes.values():
        if node["type"] in _DIMENSIONS and set(node["record_ids"]) != memberships[node["id"]]:
            raise ValueError("Entity count membership differs from its article source paths")
    return nodes, records, paths


def _category(identifier, label, record_ids, total, *, node=None, edge=None, missing=False):
    ids = sorted(record_ids)
    return {
        "id": identifier, "label": label, "count": len(ids),
        "share": len(ids) / total if total else 0.0,
        "record_ids": ids, "node_id": node["id"] if node else None,
        "edge_id": edge["id"] if edge else None,
        "selection": {"kind": "edge", "id": edge["id"]} if edge else (
            {"kind": "node", "id": node["id"]} if node else None
        ),
        "source_value": node["properties"].get("source_value", "") if node else "",
        "missing": missing,
    }


def _entity_categories(graph, node, nodes, records, paths):
    predicate, target_type, dimension, missing_label = _DIMENSIONS[node["type"]]
    grouped = defaultdict(set)
    for record_id in node["record_ids"]:
        path = paths[records[record_id]["article_node_id"]].get(predicate)
        grouped[path["target"] if path else None].add(record_id)
    total = len(node["record_ids"])
    associations = {
        (edge["source"], edge["target"]): edge for edge in graph["summary_edges"]
    }
    categories = []
    for target_id, ids in grouped.items():
        target = nodes.get(target_id)
        pair = (node["id"], target_id) if node["type"] == "SponsorCandidate" else (target_id, node["id"])
        edge = associations.get(pair)
        if target:
            if target["type"] != target_type or edge is None or set(edge["record_ids"]) != ids:
                raise ValueError("Count breakdown lacks its exact source association")
            category = _category(target_id, target["label"], ids, total, node=target, edge=edge)
        else:
            category = _category(f"missing:{target_type}", missing_label, ids, total, missing=True)
        categories.append(category)
    # Display aliases are presentation only. Two exact source values that have
    # the same display name must remain distinguishable in both the chart/table.
    labels = defaultdict(list)
    for category in categories:
        labels[category["label"]].append(category)
    for duplicates in labels.values():
        if len(duplicates) > 1:
            for category in duplicates:
                category["label"] += f" [source: {category['source_value']}]"
    categories.sort(key=lambda item: (item["missing"], -item["count"], item["label"], item["id"]))
    return dimension, categories


def _date_categories(record_ids, records):
    grouped = defaultdict(set)
    for record_id in record_ids:
        try:
            year = str(date.fromisoformat(records[record_id].get("date") or "").year)
        except (TypeError, ValueError):
            year = None
        grouped[year].add(record_id)
    total = len(record_ids)
    return [_category(f"date:{year or 'unknown'}", year or "Unknown date", ids, total,
                      missing=year is None)
            for year, ids in sorted(grouped.items(), key=lambda item: (item[0] is None, item[0] or ""))]


def selection_breakdown(graph, selected):
    """Return an exact record partition for a canonical node or relation.

    Sponsor/outlet details contain every related source category and an explicit
    missing bucket. Relation/article details use publication years plus unknown
    dates; relation endpoint shares are separate from the pie denominator.
    Invalid or stale client selection IDs return ``None``. Inconsistent server
    snapshots raise ``ValueError`` instead of producing plausible percentages.
    """
    selection, item = selection_item(graph, selected)
    if item is None:
        return None
    nodes, records, paths = _index(graph)
    ids = sorted(item["record_ids"])
    total = len(ids)
    if selection["kind"] == "node" and item["type"] in _DIMENSIONS:
        dimension, categories = _entity_categories(graph, item, nodes, records, paths)
    else:
        dimension, categories = "Publication years", _date_categories(ids, records)
    _category_colors(categories)
    membership = [identifier for category in categories for identifier in category["record_ids"]]
    if sorted(membership) != ids or len(membership) != len(set(membership)):
        raise ValueError("Count breakdown is not a complete non-overlapping record partition")
    shares = []
    if selection["kind"] == "edge":
        endpoints = [nodes[item["source"]], nodes[item["target"]]]
        # Article source paths can still expose the associated sponsor and outlet
        # denominators. These values are read from the same filtered snapshot.
        if item["predicate"] != SUMMARY_PREDICATE:
            article = nodes[item["source"]]
            endpoints = [nodes[path["target"]] for path in paths[article["id"]].values()]
        for node in endpoints:
            if node["type"] not in _DIMENSIONS:
                continue
            denominator = len(node["record_ids"])
            if not set(ids).issubset(node["record_ids"]):
                raise ValueError("Relation count does not belong to its endpoint denominator")
            shares.append({"node_id": node["id"], "label": node["label"], "type": node["type"],
                           "count": total, "total_records": denominator,
                           "share": total / denominator if denominator else 0.0})
    title = item["label"] if selection["kind"] == "node" else (
        f"{nodes[item['source']]['label']} → {nodes[item['target']]['label']}"
    )
    return {
        "selection": selection, "kind": item.get("type", "relation"), "title": title,
        "dimension": dimension, "total_records": total, "record_ids": ids,
        "unknown_date_records": sum(not records[identifier].get("date") for identifier in ids),
        "categories": categories, "endpoint_shares": shares,
        "counting_unit": "eligible_native_record", "scope": "current_filtered_snapshot",
        "color_scope": "selected_breakdown_categories",
    }


def resolve_breakdown_slice(graph, selected, candidate_id):
    """Re-resolve a chart/table bucket ID against its parent server selection."""
    if not isinstance(candidate_id, str):
        return None
    detail = selection_breakdown(graph, selected)
    if detail is None:
        return None
    return next((item for item in detail["categories"] if item["id"] == candidate_id), None)


def selection_relationships(graph, selected, *, record_ids=None):
    """Resolve named, witnessed relations for exactly the inspected records.

    An article or an article-field edge exposes both of that article's source
    fields. Entity/summary-edge selections expose counted sponsor/outlet pairs.
    Missing fields are reported separately; they never become invented nodes.
    Endpoint denominators always belong to the same filtered graph snapshot.
    """
    selection, item = selection_item(graph, selected)
    if item is None:
        return None
    nodes, records, paths = _index(graph)
    ids = set(item["record_ids"] if record_ids is None else record_ids)
    if not ids.issubset(item["record_ids"]):
        raise ValueError("Relationship focus exceeds its selected record scope")
    is_article = selection["kind"] == "node" and item["type"] == "Article"
    is_source_edge = selection["kind"] == "edge" and item["predicate"] != SUMMARY_PREDICATE
    if is_article or is_source_edge:
        article_id = item["id"] if is_article else item["source"]
        candidates = list(paths[article_id].values())
    else:
        candidates = [edge for edge in graph["summary_edges"]
                      if edge["id"] == item["id"] or selection["kind"] == "node"
                      and item["id"] in (edge["source"], edge["target"])]
    relations = []
    for edge in candidates:
        members = sorted(ids.intersection(edge["record_ids"]))
        if not members:
            continue
        endpoints = [nodes[edge["source"]], nodes[edge["target"]]]
        relations.append({
            "id": edge["id"], "source": endpoints[0]["label"], "target": endpoints[1]["label"],
            "predicate": edge["predicate"], "record_ids": members, "count": len(members),
            "selected": selection["kind"] == "edge" and item["id"] == edge["id"],
            "endpoint_shares": [{"label": node["label"], "count": len(members),
                                 "total_records": len(node["record_ids"]),
                                 "share": len(members) / len(node["record_ids"])}
                                for node in endpoints if node["type"] in _DIMENSIONS],
        })
    relations.sort(key=lambda row: (-row["count"], row["source"], row["target"], row["predicate"], row["id"]))
    return {"record_ids": sorted(ids), "total_records": len(ids), "relations": relations,
            "article_paths": is_article or is_source_edge,
            "missing_sponsor_records": sum("source_lists_sponsor" not in paths[records[rid]["article_node_id"]] for rid in ids),
            "missing_outlet_records": sum("published_in" not in paths[records[rid]["article_node_id"]] for rid in ids)}


def breakdown_figure(detail, selected_category=None):
    """A compact donut; full category names/counts belong in the adjacent table.

    Slice custom data contains only bucket IDs for server re-resolution. The
    plotted percentages are observed record shares, not model probabilities.
    No data / zero records produces an explicit empty state rather than a pie.
    """
    figure = go.Figure()
    categories = detail.get("categories", []) if detail else []
    total = detail.get("total_records", 0) if detail else 0
    if total and categories:
        figure.add_trace(go.Pie(
            labels=[escape(item["label"]) for item in categories],
            values=[item["count"] for item in categories],
            customdata=[item["id"] for item in categories],
            ids=[item["id"] for item in categories],
            marker={"colors": [item["color"] for item in categories],
                    "line": {"color": "#ffffff", "width": 2}},
            hole=.7, sort=False, direction="clockwise", rotation=90,
            text=[f"{item['share']:.0%}" if item["share"] >= .05 else "" for item in categories],
            textinfo="text", textposition="inside", insidetextorientation="horizontal",
            textfont={"size": 13, "color": "#ffffff"},
            hovertemplate="%{label}<br>%{value:,} records · %{percent:.1%}<extra></extra>",
            pull=[.06 if item["id"] == selected_category else 0 for item in categories],
            showlegend=False, name=detail["dimension"],
        ))
        figure.add_annotation(x=.5, y=.54, text=f"<b>{total:,}</b>", showarrow=False,
                              font={"size": 34, "color": "#20364c"})
        figure.add_annotation(x=.5, y=.40, text="records", showarrow=False,
                              font={"size": 13, "color": "#596d80"})
    else:
        figure.add_annotation(x=.5, y=.5, text="No records in this selection", showarrow=False,
                              font={"size": 15, "color": "#596d80"})
    figure.update_layout(height=290, margin={"l": 8, "r": 8, "t": 8, "b": 8},
                         paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                         font={"family": "Arial, sans-serif", "color": "#20364c"},
                         clickmode="event", hoverlabel={"font_size": 14},
                         transition={"duration": 0},
                         uirevision=str(detail["selection"]) if detail else "empty")
    return figure
