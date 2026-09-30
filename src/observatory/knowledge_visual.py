"""Deterministic article-centered rendering of the source knowledge graph."""

from collections import Counter
from html import escape
from textwrap import wrap

import plotly.graph_objects as go

TYPE_STYLE = {
    "sponsorcandidate": (0, "#a66d3b", "Sponsor candidate"),
    "outlet": (0, "#416785", "News outlet"),
    "article": (1, "#223e53", "Article"),
    "textversion": (2, "#627887", "Text version"),
    "sourceartifact": (2, "#748274", "Source material"),
    "annotation": (3, "#8c718c", "Historical annotation"),
    "label": (4, "#667c85", "Label category"),
    "evidencespan": (4, "#4e7d70", "Evidence span"),
}


def type_key(node):
    return str(node.get("type", "")).replace("_", "").lower()


def article_nodes(graph):
    order = {row["record_id"]: index for index, row in enumerate(graph.get("records", []))}
    nodes = [node for node in graph.get("nodes", []) if type_key(node) == "article"]
    return sorted(nodes, key=lambda node: min((order.get(rid, len(order)) for rid in node.get("record_ids", [])), default=len(order)))


def focused_graph(graph, article_id):
    """Return only the selected article's server-built neighborhood."""
    article = next((node for node in article_nodes(graph) if node["id"] == article_id), None)
    if article is None:
        return {"nodes": [], "edges": []}
    record_ids = set(article.get("record_ids", []))
    nodes = [node for node in graph.get("nodes", [])
             if node["id"] == article_id or record_ids.intersection(node.get("record_ids", []))]
    ids = {node["id"] for node in nodes}
    edges = [edge for edge in graph.get("edges", [])
             if edge.get("source") in ids and edge.get("target") in ids
             and record_ids.intersection(edge.get("record_ids", []))]
    return {"nodes": nodes, "edges": edges}


def _category_name(node):
    value = str(node.get("properties", {}).get("label_key") or node.get("label") or "Label category")
    for prefix in ("ff_labels.", "green_labels."):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    text = value.replace("_", " ")
    return text[:1].upper() + text[1:]


def _scheme_hint(node):
    scheme = str(node.get("properties", {}).get("scheme") or "Scheme unavailable")
    scheme = scheme.removeprefix("unknown-codebook:")
    return scheme if len(scheme) <= 22 else scheme[:11] + "…" + scheme[-9:]


def _node_caption(node, repeated_categories):
    """Friendly display only; identities, taxonomy keys and full hover stay intact."""
    if type_key(node) == "label":
        category = _category_name(node)
        lines = wrap(category, width=22) or ["Label category"]
        if repeated_categories[category] > 1:
            return "<br>".join(escape(line) for line in lines[:2]) + ("…" if len(lines) > 2 else "") + "<br>" + escape(_scheme_hint(node))
    else:
        lines = wrap(str(node.get("label") or node.get("type") or "Entity"), width=22)
    return "<br>".join(escape(line) for line in lines[:3]) + ("…" if len(lines) > 3 else "")


def _node_hover(node, title):
    lines = [str(node.get("label") or title), title]
    if type_key(node) == "label":
        properties = node.get("properties", {})
        if properties.get("label_key"):
            lines.append(str(properties["label_key"]))
        if properties.get("scheme"):
            lines.append("Scheme: " + str(properties["scheme"]))
    return "<br>".join(escape(line) for line in lines)


def graph_layer(graph, layer="sources", annotation_id=None):
    """A named view, not a change to graph identity or the complete export."""
    nodes, edges = graph.get("nodes", []), graph.get("edges", [])
    if layer != "annotations":
        ids = {node["id"] for node in nodes if type_key(node) in {
            "article", "sponsorcandidate", "outlet", "sourceartifact", "textversion"}}
    else:
        annotations = sorted((n for n in nodes if type_key(n) == "annotation"), key=lambda n: (n["label"], n["id"]))
        if annotation_id not in {node["id"] for node in annotations}:
            annotation_id = annotations[0]["id"] if annotations else None
        ids = {node["id"] for node in nodes if type_key(node) == "article"}
        if annotation_id:
            ids.add(annotation_id)
            ids.update(edge["target"] for edge in edges if edge["source"] == annotation_id)
        # The article-to-text relation is shown in Sources, not duplicated here.
        edges = [edge for edge in edges if edge.get("predicate") in {
            "has_annotation_record", "annotates", "assigns_label", "has_evidence", "located_in"}]
    return {"nodes": [node for node in nodes if node["id"] in ids],
            "edges": [edge for edge in edges if edge["source"] in ids and edge["target"] in ids]}


def _layout(graph):
    """Two readable card columns; each visible annotation owns its leaf rows."""
    nodes = graph.get("nodes", [])
    articles = [n for n in nodes if type_key(n) == "article"]
    annotations = [n for n in nodes if type_key(n) == "annotation"]
    captures = {e["target"] for e in graph.get("edges", []) if e.get("predicate") == "derived_from"}
    roots = {n["id"] for n in articles + annotations}
    order = {"sponsorcandidate": 0, "outlet": 1, "sourceartifact": 2, "label": 3,
             "evidencespan": 4, "textversion": 5}
    leaves = sorted((n for n in nodes if n["id"] not in roots | captures),
                    key=lambda n: (order.get(type_key(n), 3), _category_name(n), n["id"]))
    first_y = 225 if annotations else 75
    centers = {n["id"]: (640, first_y + i * 112) for i, n in enumerate(leaves)}
    mid_y = first_y + max(0, len(leaves) - 1) * 56
    for index, node in enumerate(annotations):
        centers[node["id"]] = (150, mid_y + index * 112)
    for index, node in enumerate(articles):
        centers[node["id"]] = (150, (70 if annotations else mid_y) + index * 160)
    bottom = max((y for _, y in centers.values()), default=400)
    for index, node in enumerate(n for n in nodes if n["id"] in captures):
        centers[node["id"]] = (150, bottom + 150 + index * 150)
    boxes = {}
    for node in nodes:
        x, y = centers[node["id"]]
        half_width = 115 if x == 150 else 130
        half_height = 56 if type_key(node) == "article" else 44
        boxes[node["id"]] = (x - half_width, y - half_height, x + half_width, y + half_height)
    height = max(520, int(max((b[3] for b in boxes.values()), default=400) + 34))
    width = 930 if any(e.get("predicate") == "located_in" for e in graph.get("edges", [])) else 810
    return centers, boxes, width, height


def _route(edge, centers, boxes):
    """Orthogonal routes stop at card edges. Shared trunks have a common source."""
    source, target = edge["source"], edge["target"]
    sx, sy = centers[source]
    tx, ty = centers[target]
    sl, st, sr, sb = boxes[source]
    tl, tt, tr, tb = boxes[target]
    if edge.get("predicate") == "derived_from":
        # A reviewed capture sits below the source view, away from other cards.
        lane_y = sy + 88
        return [(sx, sb), (sx, lane_y), (tx, lane_y), (tx, tt)], ((sx + tx) / 2, lane_y)
    if sx == tx and sx < 300:
        return [(sx, sb), (tx, tt)], (sx, (sb + tt) / 2)
    if sx == tx:
        lane = 880
        return [(sr, sy), (lane, sy), (lane, ty), (tr, ty)], ((sr + lane) / 2, sy)
    if tx > sx:
        lane = 307
        return [(sr, sy), (lane, sy), (lane, ty), (tl, ty)], (410, ty)
    lane_y = max(sb, tb) + 24
    return [(sx, sb), (sx, lane_y), (tx, lane_y), (tx, tb)], ((sx + tx) / 2, lane_y)


def knowledge_figure(graph=None, selected=None):
    """Labeled orthogonal relations, opaque node cards and stable selection."""
    graph = graph or {"nodes": [], "edges": []}
    nodes, edges = graph.get("nodes", []), graph.get("edges", [])
    centers, boxes, width, height = _layout(graph)
    repeated_categories = Counter(_category_name(node) for node in nodes if type_key(node) == "label")
    selected = selected or {}
    emphasized_edges = {edge["id"] for edge in edges
                        if (selected.get("kind") == "edge" and edge["id"] == selected.get("id"))
                        or (selected.get("kind") == "node" and selected.get("id") in {edge["source"], edge["target"]})}
    emphasized_nodes = {selected.get("id")} if selected.get("kind") == "node" else set()
    for edge in edges:
        if edge["id"] in emphasized_edges:
            emphasized_nodes.update((edge["source"], edge["target"]))
    figure = go.Figure()
    # Draw the selected route last so it remains visible over a shared trunk.
    for edge in sorted(edges, key=lambda e: e["id"] in emphasized_edges):
        if edge["source"] not in centers or edge["target"] not in centers:
            continue
        points, (label_x, label_y) = _route(edge, centers, boxes)
        active = edge["id"] in emphasized_edges
        muted = bool(selected) and not active
        color = "#1c5978" if active else "#dce3e8" if muted else "#899ca9"
        figure.add_scatter(x=[p[0] for p in points], y=[p[1] for p in points], mode="lines",
                           showlegend=False, hoverinfo="skip", line={"color": color, "width": 2.6 if active else 1.4})
        label = str(edge.get("label") or edge.get("predicate") or "relation")
        vertical = all(p[0] == points[0][0] for p in points)
        figure.add_scatter(
            x=[label_x], y=[label_y], mode="markers+text", showlegend=False,
            text=["<br>".join(escape(part) for part in wrap(label, width=25))],
            textposition="middle right" if vertical else "top center",
            textfont={"size": 12, "color": "#a4b0b9" if muted else "#3d5666"},
            marker={"size": 10 if active else 7, "color": color, "symbol": "diamond"},
            customdata=[{"kind": "edge", "id": edge["id"]}],
            hovertext=[escape(label) + "<br>Click for meaning and source"],
            hovertemplate="%{hovertext}<extra></extra>")
        end, before = points[-1], points[-2]
        dx, dy = end[0] - before[0], end[1] - before[1]
        distance = max(abs(dx), abs(dy), 1)
        figure.add_annotation(x=end[0], y=end[1], ax=end[0] - dx / distance * 18,
                              ay=end[1] - dy / distance * 18, xref="x", yref="y", axref="x", ayref="y",
                              text="", showarrow=True, arrowhead=2, arrowsize=1, arrowwidth=2 if active else 1.4,
                              arrowcolor=color)
    for node in nodes:
        kind = type_key(node)
        _, color, title = TYPE_STYLE.get(kind, (2, "#627887", kind or "Other"))
        active = node["id"] in emphasized_nodes
        muted = bool(selected) and not active
        x, y = centers[node["id"]]
        left, top, right, bottom = boxes[node["id"]]
        figure.add_shape(type="rect", x0=left, y0=top, x1=right, y1=bottom, layer="below",
                         fillcolor="#f4f9fc" if active else "#ffffff",
                         line={"color": "#1c5978" if active else "#e5e9ed" if muted else "#c5d0d8",
                               "width": 2 if active else 1})
        figure.add_shape(type="line", x0=left, y0=top, x1=left, y1=bottom, layer="below",
                         line={"color": "#d9e0e5" if muted else color, "width": 3})
        figure.add_annotation(x=x, y=top + 15, xref="x", yref="y", text=escape(title.upper()),
                              font={"size": 10, "color": "#a4afb8" if muted else color}, showarrow=False)
        caption = _node_caption(node, repeated_categories)
        if kind == "article":
            lines = wrap(str(node.get("label") or "Article"), width=27)
            caption = "<br>".join(escape(line) for line in lines[:4]) + ("…" if len(lines) > 4 else "")
        figure.add_scatter(
            x=[x], y=[y + 8], mode="text", name=title, showlegend=False,
            text=[caption], textposition="middle center", textfont={"size": 14, "color": "#98a6b0" if muted else "#243e50"},
            customdata=[{"kind": "node", "id": node["id"]}],
            hovertext=[_node_hover(node, title)], hovertemplate="%{hovertext}<extra></extra>")
    figure.update_layout(
        template="plotly_white", height=height, margin={"l": 0, "r": 0, "t": 0, "b": 0},
        font={"family": "Arial, sans-serif", "color": "#34495e", "size": 12},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False, legend={"font": {"size": 12}},
        xaxis={"visible": False, "range": [0, width], "fixedrange": True},
        yaxis={"visible": False, "range": [height, 0], "fixedrange": True},
        meta={"canvas_width": width}, dragmode=False, clickmode="event",
    )
    if not nodes:
        figure.add_annotation(text="No relationships available in this view", x=width / 2, y=150, showarrow=False)
    return figure
