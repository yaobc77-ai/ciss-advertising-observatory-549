"""Cytoscape views of the complete, witnessed advertising source map."""

from __future__ import annotations

import math
from functools import lru_cache
from urllib.parse import quote

COLORS = {"SponsorCandidate": "#54798a", "Outlet": "#a07862", "Article": "#7d8c73"}
TYPE_NAMES = {"SponsorCandidate": "Source-listed sponsor", "Outlet": "News outlet", "Article": "Article"}
TEXT_READY_COLOR = "#3a9c89"
METADATA_ONLY_COLOR = "#97a6b9"
_SOURCE_LABELS = {"source_lists_sponsor": "Source lists sponsor", "published_in": "Published in"}


@lru_cache(maxsize=512, typed=True)
def entity_record_image(kind, record_count, text_ready_count):
    """Encode exact record membership as dots and text availability as a ring.

    SVG inputs are fixed type names and checked integer counts. Record IDs,
    document text and entity labels are never interpolated into XML. The image
    decorates an existing entity; its dots do not create additional graph facts.
    """
    if kind not in {"SponsorCandidate", "Outlet"}:
        raise ValueError("Only source sponsors and outlets have record rings")
    if (type(record_count) is not int or type(text_ready_count) is not int
            or not 0 <= text_ready_count <= record_count):
        raise ValueError("Record-ring counts must be valid integer partitions")
    metadata_only = record_count - text_ready_count
    if kind == "Outlet":
        # A rounded square, beginning at the top centre, preserves the outlet
        # shape even though both entity types use the same availability colours.
        path = ("M64 7 H99 A22 22 0 0 1 121 29 V99 A22 22 0 0 1 99 121 "
                "H29 A22 22 0 0 1 7 99 V29 A22 22 0 0 1 29 7 Z")
        perimeter = 4 * 70 + 2 * math.pi * 22
    else:
        path = "M64 7 A57 57 0 1 1 64 121 A57 57 0 1 1 64 7 Z"
        perimeter = 2 * math.pi * 57
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE svg>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" '
        f'data-record-count="{record_count}" data-text-ready="{text_ready_count}" '
        f'data-metadata-only="{metadata_only}">',
        f'<path d="{path}" fill="#f4f7f8" stroke="{METADATA_ONLY_COLOR}" stroke-width="10"/>',
    ]
    if text_ready_count:
        portion = perimeter * text_ready_count / record_count
        parts.append(f'<path d="{path}" fill="none" stroke="{TEXT_READY_COLOR}" stroke-width="10" '
                     f'stroke-dasharray="{portion:.8f} {perimeter:.8f}"/>')
    # A deterministic sunflower distribution puts precisely one marker inside
    # the ring for each record without fetching article contents or using RNG.
    golden_angle = math.pi * (3 - math.sqrt(5))
    dot_radius = min(4.2, 42 / (2 * math.sqrt(max(1, record_count))))
    for index in range(record_count):
        radius = 42 * math.sqrt((index + .5) / record_count)
        angle = index * golden_angle
        x, y = 64 + radius * math.cos(angle), 64 + radius * math.sin(angle)
        parts.append(f'<circle class="record-dot" cx="{x:.3f}" cy="{y:.3f}" '
                     f'r="{dot_radius:.3f}" fill="{COLORS[kind]}" stroke="#fff" stroke-width=".6"/>')
    parts.append("</svg>")
    # Cytoscape supports URL-encoded SVG data URIs; base64 is deliberately not
    # used. Explicit dimensions avoid browser-specific viewBox scaling issues.
    return "data:image/svg+xml;utf8," + quote("".join(parts), safe="")


def selection_item(graph, candidate):
    """Accept only a canonical ID and kind, discarding all client properties."""
    if not isinstance(candidate, dict) or not isinstance(candidate.get("id"), str):
        return None, None
    kind = candidate.get("kind")
    items = graph.get("nodes", []) if kind == "node" else (
        [*graph.get("summary_edges", []), *graph.get("article_edges", [])] if kind == "edge" else []
    )
    for item in items:
        if item.get("id") == candidate["id"]:
            return {"kind": kind, "id": item["id"]}, item
    return None, None


def visible_map(graph, mode="entities", anchor=None, *, record_ids=None):
    """Overview never prunes links; article expansion has an explicit anchor."""
    if mode != "articles":
        return ([node for node in graph.get("nodes", []) if node["type"] != "Article"],
                list(graph.get("summary_edges", [])))
    _, item = selection_item(graph, anchor)
    ids = set(item.get("record_ids", [])) if item else None
    if record_ids is not None:
        ids = set(record_ids) if ids is None else ids.intersection(record_ids)
    articles = [node for node in graph.get("nodes", []) if node["type"] == "Article"
                and (ids is None or ids.intersection(node.get("record_ids", [])))]
    article_ids = {node["id"] for node in articles}
    edges = [edge for edge in graph.get("article_edges", []) if edge["source"] in article_ids]
    node_ids = article_ids | {edge["target"] for edge in edges}
    return [node for node in graph.get("nodes", []) if node["id"] in node_ids], edges


def map_elements(graph, mode="entities", anchor=None, *, record_ids=None):
    nodes, edges = visible_map(graph, mode, anchor, record_ids=record_ids)
    expanded_records = ({rid for node in nodes if node["type"] == "Article"
                         for rid in node.get("record_ids", [])} if mode == "articles" else None)
    memberships = {node["id"]: (set(node.get("record_ids", [])) if expanded_records is None
                               else expanded_records.intersection(node.get("record_ids", [])))
                   for node in nodes}
    maximum = max((len(memberships[node["id"]]) for node in nodes), default=1)
    records = {row["record_id"]: row for row in graph.get("records", [])}
    groups = {kind: [node for node in nodes if node["type"] == kind] for kind in COLORS}
    elements = []
    for kind, members in groups.items():
        members = sorted(members, key=lambda node: (-node["record_count"], node["label"], node["id"]))
        for index, node in enumerate(members):
            angle = 2 * math.pi * index / max(1, len(members))
            center = {"SponsorCandidate": -500, "Outlet": 500, "Article": 0}[kind]
            # Keep every article marker available even when the whole corpus is
            # expanded; a fixed circle would overlap hundreds of 25px markers.
            radius = max(440, len(members) * 32 / (2 * math.pi)) if kind == "Article" else 390
            member_ids = memberships[node["id"]]
            count = len(member_ids)
            size = 25 if kind == "Article" else 70 + 54 * math.sqrt(count / max(1, maximum))
            label = node.get("label") or TYPE_NAMES[kind]
            # Entity names remain complete; text wrapping handles long names.
            caption = label if kind != "Article" or len(label) <= 65 else label[:62] + "…"
            position = {"x": center + radius * math.cos(angle), "y": radius * math.sin(angle)}
            if mode != "articles":
                # A role-based initial view keeps all 27 names readable. Force,
                # circle and grid remain optional native Cytoscape layouts.
                position = ({"x": (index % 3) * 250, "y": (index // 3) * 165}
                            if kind == "SponsorCandidate" else {"x": 1010, "y": index * 145})
            data = {"id": node["id"], "kind": "node", "type": kind,
                    "label": label, "display_label": (caption if kind == "Article"
                                                       else f"{caption}\n{count:,} {'record' if count == 1 else 'records'}"),
                    "record_count": count, "scope_record_count": node.get("record_count", count),
                    "size": round(size, 2)}
            if kind != "Article":
                ready = sum(records.get(rid, {}).get("retrievable") is True
                            for rid in member_ids)
                data.update(text_ready_count=ready, metadata_only_count=count - ready,
                            record_image=entity_record_image(kind, count, ready))
            elements.append({
                "data": data,
                "position": position,
                "classes": kind,
            })
    for index, edge in enumerate(edges):
        count = edge.get("count", len(edge.get("record_ids", [])))
        elements.append({"data": {
            "id": edge["id"], "kind": "edge", "source": edge["source"], "target": edge["target"],
            "predicate": edge["predicate"], "record_count": count,
            "count_label": (f"{count:,} {'record' if count == 1 else 'records'}" if mode != "articles"
                            else _SOURCE_LABELS.get(edge["predicate"], edge.get("label", edge["predicate"]))),
            "curve": (1 if index % 2 else -1) * (32 + 8 * (index % 4)),
            "width": round(min(7, 1 + math.sqrt(count) * .65), 2),
        }, "classes": "association" if mode != "articles" else "source-path " + edge["predicate"]})
    return elements


def _focus_ids(graph, mode, anchor, selected, *, record_ids=None):
    selection, item = selection_item(graph, selected)
    if not item:
        return set(), set()
    nodes, edges = visible_map(graph, mode, anchor, record_ids=record_ids)
    if record_ids is not None:
        scope = set(record_ids)
        edges = [edge for edge in edges if scope.intersection(edge.get("record_ids", []))]
    node_ids, edge_ids = set(), set()
    article_focus = (selection["kind"] == "node" and item["type"] == "Article"
                     or selection["kind"] == "edge" and item["predicate"] != "derived_source_association")
    if mode != "articles" and article_focus:
        # Switching views keeps an article selection meaningful: highlight its
        # witnessed entity pair (or just the known endpoint for a missing field).
        ids = set(item["record_ids"])
        if record_ids is not None:
            ids.intersection_update(record_ids)
        for edge in graph.get("article_edges", []):
            if ids.intersection(edge.get("record_ids", [])):
                node_ids.add(edge["target"])
        for edge in edges:
            if ids.intersection(edge.get("record_ids", [])):
                edge_ids.add(edge["id"])
                node_ids.update((edge["source"], edge["target"]))
    elif selection["kind"] == "node":
        node_ids.add(item["id"])
        for edge in edges:
            if item["id"] in (edge["source"], edge["target"]):
                edge_ids.add(edge["id"])
                node_ids.update((edge["source"], edge["target"]))
    elif any(edge["id"] == item["id"] for edge in edges):
        edge_ids.add(item["id"])
        node_ids.update((item["source"], item["target"]))
    elif mode == "articles":
        records = set(item.get("record_ids", []))
        for edge in edges:
            if records.intersection(edge.get("record_ids", [])):
                edge_ids.add(edge["id"])
                node_ids.update((edge["source"], edge["target"]))
    return node_ids & {node["id"] for node in nodes}, edge_ids


def map_stylesheet(graph=None, mode="entities", selected=None, anchor=None, *, record_ids=None):
    rules = [
        {"selector": "node", "style": {
            "label": "data(display_label)", "width": "data(size)", "height": "data(size)",
            "font-family": "Arial, sans-serif", "font-size": 26, "font-weight": 500,
            "text-wrap": "wrap", "text-max-width": 190, "text-valign": "bottom", "text-margin-y": 9,
            "text-background-color": "#f4f6f7", "text-background-opacity": .85,
            "text-background-padding": 3, "color": "#253743", "border-width": 1,
            "border-color": "#fff", "min-zoomed-font-size": 8,
        }},
        {"selector": "edge", "style": {
            "curve-style": "unbundled-bezier", "control-point-distances": "data(curve)",
            "control-point-weights": .5, "width": "data(width)", "line-color": "#a9b8bf",
            "opacity": .5, "label": "", "font-size": 17,
            "text-background-color": "#fff", "text-background-opacity": .95,
            "text-background-padding": 3, "color": "#263a46",
        }},
        {"selector": ".source-path", "style": {"target-arrow-shape": "triangle", "target-arrow-color": "#a9b8bf", "arrow-scale": .8}},
        {"selector": ".Outlet", "style": {"shape": "round-rectangle", "text-valign": "center",
                                            "text-halign": "right", "text-margin-x": 12,
                                            "text-margin-y": 0}},
        {"selector": ".Article", "style": {"shape": "round-rectangle", "label": ""}},
        {"selector": ":selected", "style": {"border-color": "#253743", "border-width": 3}},
    ]
    rules.extend({"selector": "." + kind, "style": {"background-color": color}}
                 for kind, color in COLORS.items())
    rules.extend([
        {"selector": ".SponsorCandidate, .Outlet", "style": {
            "background-color": "#f4f7f8", "background-image": "data(record_image)",
            "background-fit": "contain", "background-width": "100%", "background-height": "100%",
            "background-image-crossorigin": "null", "border-width": 0,
        }},
        {"selector": ".source_lists_sponsor", "style": {
            "line-color": COLORS["SponsorCandidate"], "target-arrow-color": COLORS["SponsorCandidate"],
        }},
        {"selector": ".published_in", "style": {
            "line-color": COLORS["Outlet"], "target-arrow-color": COLORS["Outlet"],
        }},
    ])
    graph = graph or {}
    selection, selected_item = selection_item(graph, selected)
    node_lookup = {node["id"]: node for node in graph.get("nodes", [])}
    focused_nodes, focused_edges = _focus_ids(graph, mode, anchor, selected, record_ids=record_ids)
    if focused_nodes or focused_edges:
        rules.extend([{"selector": "node", "style": {"opacity": .4}},
                      {"selector": "edge", "style": {"opacity": .07}}])
        for identifier in sorted(focused_nodes):
            # A publisher can expand hundreds of article markers. Show a title
            # for the specific article being inspected, not every neighbor.
            article = node_lookup[identifier]["type"] == "Article"
            show_title = (not article or selection["kind"] == "edge"
                          and selected_item["predicate"] != "derived_source_association"
                          or selection["kind"] == "node" and selection["id"] == identifier)
            rules.append({"selector": f'node[id = "{identifier}"]', "style": {
                "opacity": 1, "border-width": 2, "border-color": "#233b49",
                "label": "data(display_label)" if show_title else "",
            }})
        for identifier in sorted(focused_edges):
            rules.append({"selector": f'edge[id = "{identifier}"]', "style": {
                "opacity": .95, "line-color": "#385f72", "target-arrow-color": "#385f72",
                "label": "data(count_label)",
            }})
    return rules


def map_layout(name="cose", *, fit=True):
    name = name if name in {"cose", "circle", "grid", "preset"} else "cose"
    # Labels take part in layout spacing, so the readable text does not collide
    # even when the node's circle is much smaller than its name.
    result = {"name": name, "fit": fit, "padding": 48, "animate": False,
              "nodeDimensionsIncludeLabels": True}
    if name == "preset":
        result["padding"] = 22
    if name == "cose":
        result.update(randomize=False, nodeRepulsion=8500, idealEdgeLength=190, nodeOverlap=55,
                      componentSpacing=60, numIter=800, gravity=.8)
    return result
