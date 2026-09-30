"""Filtered knowledge graph explorer; browser selections carry IDs, never facts."""

import json
from urllib.parse import unquote

from dash import Input, Output, State, ctx, dcc, html
from dash.exceptions import PreventUpdate

from .knowledge_visual import (
    TYPE_STYLE,
    article_nodes,
    focused_graph,
    graph_layer,
    knowledge_figure,
    type_key,
)
from .models import Filters
from .network_ui import record_cards
from .service import safe_url

PAGE_SIZE = 5


def knowledge_panel():
    return html.Section([
        html.H2("Explore the article knowledge graph"),
        html.P("Follow each article to its source-listed entities, text version, original material and historical annotations.", className="chart-note"),
        html.Div([
            html.Div([html.Label("Article in this page", htmlFor="knowledge-focus"),
                      dcc.Dropdown(id="knowledge-focus", options=[], clearable=False,
                                   placeholder="Choose an article")], className="knowledge-focus"),
            html.Button("Download page graph ↓", id="knowledge-export", n_clicks=0, className="button button-quiet"),
        ], className="network-toolbar"),
        html.Div([
            dcc.RadioItems(id="knowledge-layer", options=[
                {"label": "Sources", "value": "sources"},
                {"label": "Historical labels", "value": "annotations"},
                {"label": "CLAIMS2 evidence", "value": "claims2"},
            ], value="sources", inline=True, className="knowledge-layer"),
            html.Div([
                html.Label("Analysis record", htmlFor="knowledge-annotation"),
                dcc.Dropdown(id="knowledge-annotation", options=[], clearable=False,
                             placeholder="No historical annotations available"),
            ], id="knowledge-annotation-wrap", className="knowledge-annotation", style={"display": "none"}),
        ], className="knowledge-layer-toolbar"),
        html.P(id="knowledge-coverage", className="scope-note", role="status"),
        html.Div(id="knowledge-warnings", className="knowledge-warnings"),
        html.Div([
            html.Div([
                html.Div(dcc.Graph(id="knowledge-graph", figure=knowledge_figure(),
                                   config={"displayModeBar": False, "responsive": True, "scrollZoom": False},
                                   style=_figure_style(knowledge_figure())),
                         className="knowledge-canvas"),
                html.P("Lines turn at right angles; arrows show direction. Select a card or relation diamond to highlight its connections and inspect its source. Unrelated connections fade.", className="scope-note"),
            ], className="knowledge-visual"),
            html.Aside([
                html.Div([html.H3("Knowledge & sources"), html.Button("Clear selection", id="knowledge-reset", n_clicks=0,
                                                                     className="button button-quiet")], className="section-heading"),
                html.Label("Inspect a node or relation", htmlFor="knowledge-item"),
                dcc.Dropdown(id="knowledge-item", options=[], clearable=True, placeholder="Select an item"),
                html.Div(id="knowledge-inspector", className="knowledge-inspector", **{"aria-live": "polite"}),
            ], className="record-sidebar", **{"aria-label": "Knowledge and sources"}),
        ], className="exploration-split knowledge-split"),
        html.Div([
            html.Button("Previous 5 articles", id="knowledge-prev", n_clicks=0, disabled=True, className="button button-quiet"),
            html.Button("Next 5 articles", id="knowledge-next", n_clicks=0, disabled=True, className="button button-quiet"),
        ], className="pager"),
        html.P("Sponsor candidates are source values, not verified company identities. Historical labels are annotations, not verified facts. Missing fields do not create an Unknown entity.", className="scope-note"),
        dcc.Store(id="knowledge-selection"), dcc.Store(id="knowledge-offset", data=0),
        dcc.Download(id="knowledge-download"), html.Div(id="knowledge-export-status", role="status"),
    ], id="article-knowledge-graph", className="knowledge-panel")


def _offset(value):
    try:
        return max(0, int(value or 0)) // PAGE_SIZE * PAGE_SIZE
    except (ValueError, TypeError, OverflowError):
        return 0


def _filters(snapshot):
    filters = Filters.model_validate((snapshot or {}).get("filters") or {})
    if filters.dataset != "native":
        raise ValueError("Knowledge graph is only available for native advertising")
    return filters


def _selection(graph, candidate):
    if not isinstance(candidate, dict) or not isinstance(candidate.get("id"), str):
        return None
    kind = candidate.get("kind")
    collection = "nodes" if kind == "node" else "edges" if kind == "edge" else ""
    if collection and any(item.get("id") == candidate["id"] for item in graph.get(collection, [])):
        return {"kind": kind, "id": candidate["id"]}
    return None


def _human(key):
    return str(key).replace("_", " ").capitalize()


def _source_url(value, links_enabled):
    if not isinstance(value, str) or not links_enabled:
        return ""
    decoded = unquote(value)
    parts = decoded.split("/")
    if (len(parts) == 5 and parts[:2] == ["", "records"] and parts[3] == "attachments"
            and all(part not in {"", ".", ".."} for part in (parts[2], parts[4]))
            and not any(char in decoded for char in ("?", "#", "\\"))):
        return value
    return safe_url(value)


STATUS_TEXT = {
    "source_candidate_not_resolved": "Source candidate · identity not reviewed",
    "source_recorded_unverified": "Recorded in source · not independently verified",
    "historical_automatic_unverified": "Historical automated annotation · not reviewed",
    "imported_unverified": "Imported annotation · not reviewed",
    "current_text_hash_bound": "Linked to the current text by matching its hash",
    "prior_or_unavailable_body": "Prior or unavailable text · current version not linked",
    "no_validated_spans": "No validated evidence passages",
    "has_validated_spans": "Has passages located in the stored text",
    "imported_category_not_a_factual_verdict": "Imported category · not a factual verdict",
    "url_recorded_contents_unverified": "URL recorded · page contents not verified",
    "reviewed_local_capture": "Reviewed local capture",
    "exact_character_match": "Exact match to stored text",
    "not_verified": "Not verified",
    "automatic_unverified": "Automatic taxonomy match · semantic review pending",
    "human_supported": "Human-supported taxonomy match · not independent fact checking",
}


def _property_value(key, value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, str) and (key.endswith("status") or key in {"method", "role", "kind", "relation"}):
        return STATUS_TEXT.get(value, _human(value))
    return str(value)


def _properties(properties, links_enabled):
    values, technical = [], []
    for key, value in (properties or {}).items():
        if value is None or value == "" or value == []:
            continue
        if "url" in key.lower() or key.lower() in {"uri", "href"}:
            url = _source_url(value, links_enabled)
            if url:
                values.append(html.Div([html.Dt(_human(key)), html.Dd(html.A("Open source ↗", href=url, target="_blank", rel="noopener noreferrer"))]))
            continue
        rendered = _property_value(key, value)
        is_hash = isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
        target = technical if is_hash or key.endswith(("_id", "_hash", "_sha256")) or key in {"sha256", "label_key", "ordinal", "source_row"} else values
        target.append(html.Div([html.Dt(_human(key)), html.Dd(rendered)]))
    return html.Div([
        html.Dl(values, className="knowledge-properties"),
        html.Details([html.Summary("Technical details"), html.Dl(technical, className="knowledge-properties")]) if technical else None,
    ])


def _inspector(graph, focused, selected, links_enabled):
    nodes = {node["id"]: node for node in focused["nodes"]}
    if selected:
        collection = focused["nodes"] if selected["kind"] == "node" else focused["edges"]
        item = next(item for item in collection if item["id"] == selected["id"])
        heading = item.get("label") or item.get("predicate") or item.get("type")
        parts = [html.H4(heading)]
        if selected["kind"] == "edge":
            source, target = nodes[item["source"]], nodes[item["target"]]
            parts.append(html.P(f"{source['label']} → {target['label']}", className="knowledge-relation"))
            parts.append(_properties({"relation": item.get("predicate"), **item.get("properties", {})}, links_enabled))
            definition = graph.get("predicate_definitions", {}).get(item.get("predicate"), {})
            if definition.get("description"):
                parts.append(html.P(definition["description"], className="knowledge-definition"))
            parts.append(html.H4("Relation provenance"))
            provenance = item.get("provenance") or {}
            parts.append(_properties(provenance if isinstance(provenance, dict) else {"sources": provenance}, links_enabled))
        else:
            parts.append(html.P(TYPE_STYLE.get(type_key(item), (None, None, item.get("type")))[2], className="eyebrow"))
            parts.append(_properties(item.get("properties"), links_enabled))
        ids = set(item.get("record_ids", []))
        parts.extend(record_cards([row for row in graph.get("records", []) if row["record_id"] in ids], links_enabled))
        parts.append(html.Details([html.Summary("Graph reference"), html.Code(item["id"])]))
        return parts
    articles = article_nodes(focused)
    if not articles:
        return html.P("No eligible articles in this selection.", className="muted")
    article = articles[0]
    ids = set(article.get("record_ids", []))
    parts = [*record_cards([row for row in graph.get("records", []) if row["record_id"] in ids], links_enabled),
             html.H4("Relations in this view"), html.P("Select a diamond to inspect its source and status.", className="muted")]
    parts.append(html.Ul([
        html.Li([html.Strong(edge.get("label") or edge.get("predicate")), ": ",
                 str(nodes[edge["source"]].get("label", "")), " → ", str(nodes[edge["target"]].get("label", ""))])
        for edge in focused["edges"]
    ], className="knowledge-relations"))
    return parts


def _figure_style(figure):
    width = (figure.layout.meta or {}).get("canvas_width", 960)
    return {"height": f"{figure.layout.height}px", "width": f"{width}px", "minWidth": "100%"}


def _annotation_options(focused, layer="annotations"):
    kind = "claimassignment" if layer == "claims2" else "annotation"
    annotations = sorted((node for node in focused["nodes"] if type_key(node) == kind),
                         key=lambda node: (node.get("label", ""), node["id"]))
    return [{"label": node.get("label") or "Historical annotation", "value": node["id"]}
            for node in annotations]


def _unavailable(message):
    figure = knowledge_figure()
    return (figure, message, [], html.P(message), None, 0, True, True, [], None, [], None,
            _figure_style(figure), [], None, {"display": "none"})


def register_knowledge_graph(app, service, links_enabled, record_details=None, *, require_open=False):
    def load_graph(filters, offset):
        kwargs = {"offset": offset, "limit": PAGE_SIZE}
        if record_details is not None:
            kwargs["record_details"] = record_details
        return service.knowledge_graph(filters, **kwargs)

    @app.callback(
        Output("knowledge-graph", "figure"), Output("knowledge-coverage", "children"),
        Output("knowledge-warnings", "children"), Output("knowledge-inspector", "children"),
        Output("knowledge-selection", "data"), Output("knowledge-offset", "data"),
        Output("knowledge-prev", "disabled"), Output("knowledge-next", "disabled"),
        Output("knowledge-focus", "options"), Output("knowledge-focus", "value"),
        Output("knowledge-item", "options"), Output("knowledge-item", "value"),
        Output("knowledge-graph", "style"),
        Output("knowledge-annotation", "options"), Output("knowledge-annotation", "value"),
        Output("knowledge-annotation-wrap", "style"),
        Input("native-network-data", "data"), Input("native-view", "value"),
        Input("knowledge-focus", "value"), Input("knowledge-graph", "clickData"),
        Input("knowledge-item", "value"),
        Input("knowledge-reset", "n_clicks"), Input("knowledge-prev", "n_clicks"), Input("knowledge-next", "n_clicks"),
        Input("knowledge-layer", "value"), Input("knowledge-annotation", "value"),
        *([Input("article-provenance-open", "data"), Input("page-location", "pathname"),
           Input("active-dataset", "value")] if require_open else []),
        State("knowledge-selection", "data"), State("knowledge-offset", "data"),
    )
    def explore(snapshot, view, focus, click, item_value, reset, previous, following, layer, annotation_id, *state):
        selected, offset = state[-2:]
        if require_open and (not state[0] or state[1] != "/data" or state[2] != "native"):
            return _unavailable("Open article provenance to inspect versions, sources and historical annotations.")
        if view != "relationships":
            return _unavailable("Choose Relationships to load the knowledge graph.")
        if not callable(getattr(service, "knowledge_graph", None)):
            return _unavailable("Knowledge graph is unavailable in this environment.")
        trigger = ctx.triggered_id
        offset = _offset(offset)
        if trigger in {"native-network-data", "native-view", None}:
            offset, selected, focus = 0, None, None
        elif trigger == "knowledge-next":
            offset, selected, focus = offset + PAGE_SIZE, None, None
        elif trigger == "knowledge-prev":
            offset, selected, focus = max(0, offset - PAGE_SIZE), None, None
        elif trigger in {"knowledge-reset", "knowledge-focus", "knowledge-layer", "knowledge-annotation"}:
            selected = None
        try:
            filters = _filters(snapshot)
            graph = load_graph(filters, offset)
            total = int(graph.get("coverage", {}).get("total_records", 0))
            last = max(0, (total - 1) // PAGE_SIZE * PAGE_SIZE)
            if offset > last:
                offset = last
                graph = load_graph(filters, offset)
        except Exception:
            return _unavailable("The knowledge graph is temporarily unavailable. Try again.")
        articles = article_nodes(graph)
        options = [{"label": node["label"], "value": node["id"]} for node in articles]
        if not isinstance(focus, str) or focus not in {node["id"] for node in articles}:
            focus = articles[0]["id"] if articles else None
            selected = None
        focused = focused_graph(graph, focus)
        layer = layer if layer in {"annotations", "claims2"} else "sources"
        annotation_options = _annotation_options(focused, layer)
        if not isinstance(annotation_id, str) or annotation_id not in {item["value"] for item in annotation_options}:
            annotation_id = annotation_options[0]["value"] if annotation_options else None
            if layer in {"annotations", "claims2"}:
                selected = None
        visible = graph_layer(focused, layer, annotation_id)
        if trigger == "knowledge-graph":
            points = click.get("points") if isinstance(click, dict) else None
            selected = (points[0].get("customdata")
                        if isinstance(points, list) and points and isinstance(points[0], dict) else None)
        elif trigger == "knowledge-item":
            try:
                selected = json.loads(item_value) if item_value else None
            except (TypeError, ValueError):
                selected = None
        selected = _selection(visible, selected)
        by_id = {node["id"]: node for node in visible["nodes"]}
        item_options = [{"label": f"{TYPE_STYLE.get(type_key(node), (None, None, node['type']))[2]}: {node['label']}",
                         "value": json.dumps({"kind": "node", "id": node["id"]}, sort_keys=True)}
                        for node in visible["nodes"]]
        item_options.extend({"label": f"Relation: {edge.get('label') or edge.get('predicate')} · {by_id[edge['source']]['label']} → {by_id[edge['target']]['label']}",
                             "value": json.dumps({"kind": "edge", "id": edge["id"]}, sort_keys=True)}
                            for edge in visible["edges"])
        count = len(articles)
        coverage = (f"Page contains articles {offset + 1 if count else 0}–{offset + count} of {total:,} in the current filters. "
                    f"This {'CLAIMS2 evidence' if layer == 'claims2' else 'historical label' if layer == 'annotations' else 'source'} view shows {len(visible['nodes'])} nodes and {len(visible['edges'])} typed relations "
                    f"from the article's full graph ({len(focused['nodes'])} nodes, {len(focused['edges'])} relations). "
                    "Download includes all articles on this page.")
        focused_ids = {rid for node in article_nodes(focused) for rid in node.get("record_ids", [])}
        messages = dict.fromkeys(
            warning if isinstance(warning, str) else warning.get("message", warning.get("code", "Source data requires review"))
            for warning in graph.get("warnings", [])
            if isinstance(warning, str) or not warning.get("record_id") or warning.get("record_id") in focused_ids
        )
        warnings = [html.P(message, className="scope-note") for message in messages]
        if layer == "annotations" and not annotation_options:
            warnings.insert(0, html.P("No historical annotations are available for this article. No labels or evidence have been inferred.", className="scope-note"))
        if layer == "claims2":
            warnings.insert(0, html.P(
                "Published matches only. Missing results are not negative classifications."
                if annotation_options else "No published CLAIMS2 matches for this article; classification or review may be pending.",
                className="scope-note"))
        figure = knowledge_figure(visible, selected)
        return (figure, coverage, warnings, _inspector(graph, visible, selected, links_enabled),
                selected, offset, offset == 0, offset + PAGE_SIZE >= total, options, focus, item_options,
                json.dumps(selected, sort_keys=True) if selected else None,
                _figure_style(figure), annotation_options, annotation_id,
                {"display": "block" if layer in {"annotations", "claims2"} and annotation_options else "none"})

    @app.callback(Output("knowledge-download", "data"), Output("knowledge-export-status", "children"),
                  Input("knowledge-export", "n_clicks"), State("native-network-data", "data"),
                  State("native-view", "value"), State("knowledge-offset", "data"), prevent_initial_call=True)
    def export(clicks, snapshot, view, offset):
        if not clicks or view != "relationships":
            raise PreventUpdate
        try:
            graph = load_graph(_filters(snapshot), _offset(offset))
            payload = {"export_scope": "current article page under current filters", **graph}
            # The public service applies source-link policy to every graph property.
            content = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        except Exception:
            return None, "Graph export is temporarily unavailable. Try again."
        return {"content": content, "filename": "article-knowledge-graph-page.json", "type": "application/json"}, "Downloaded the current article page graph."
