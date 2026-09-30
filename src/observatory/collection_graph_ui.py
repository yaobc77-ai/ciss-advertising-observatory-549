"""Large interactive source map with server-resolved selections and record pages."""

from __future__ import annotations

import csv
import json
from io import StringIO

import dash_cytoscape as cyto
from dash import Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate

from .analytics import csv_safe_cell, sponsor_metadata
from .collection_graph_breakdown import (
    breakdown_figure,
    resolve_breakdown_slice,
    selection_breakdown,
    selection_relationships,
)
from .collection_graph_visual import (
    COLORS,
    METADATA_ONLY_COLOR,
    TEXT_READY_COLOR,
    TYPE_NAMES,
    map_elements,
    map_layout,
    map_stylesheet,
    selection_item,
    visible_map,
)
from .knowledge_map import validate_collection_map
from .models import Filters
from .network_ui import record_cards

PAGE_SIZE = 10
PREFIX = "collection-graph"
ALL_BUCKETS = "__all_selection_records__"
EXAMPLES = {"example-company": ("SponsorCandidate", "ExxonMobil"),
            "example-outlet": ("Outlet", "The Washington Post"),
            "example-count": ("Outlet", "The New York Times")}


def collection_graph_panel():
    def button(label, name, **props):
        return html.Button(label, id=f"{PREFIX}-{name}", n_clicks=0,
                           className="button button-quiet", **props)

    return html.Section([
        html.Div([html.Div([html.Span("SOURCE KNOWLEDGE GRAPH", className="eyebrow"),
                           html.H2("Advertising connections")]),
                  button("Expand view", "expand", **{"aria-label": "Expand or exit graph view"})],
                 className="section-heading"),
        html.P("Select a company or news outlet to see its connections, record counts and share of advertisements.", className="chart-note"),
        html.Div([html.Span("Try a client question:", className="muted"),
                  button("ExxonMobil → publishers", "example-company"),
                  button("Washington Post → sponsors", "example-outlet"),
                  button("New York Times → count", "example-count")], className="collection-graph-examples"),
        html.Div([
            html.Div([html.Label("Find a node or relationship", htmlFor=f"{PREFIX}-find"),
                      dcc.Dropdown(id=f"{PREFIX}-find", options=[], clearable=True, placeholder="Search the current graph")],
                     className="collection-graph-find"),
            dcc.RadioItems(id=f"{PREFIX}-view", options=[{"label": "Entities", "value": "entities"},
                                                        {"label": "Articles", "value": "articles"}],
                           value="entities", inline=True),
            html.Div([html.Label("Layout", htmlFor=f"{PREFIX}-layout"),
                      dcc.Dropdown(id=f"{PREFIX}-layout", options=[{"label": "By type", "value": "preset"},
                                                                                {"label": "Network", "value": "cose"},
                                                                                {"label": "Circle", "value": "circle"},
                                                                                {"label": "Grid", "value": "grid"}],
                                   value="preset", clearable=False, searchable=False)], className="collection-graph-layout"),
            html.Div([button("Fit", "fit", **{"aria-label": "Fit graph to view"}),
                      button("+", "zoom-in", **{"aria-label": "Zoom in"}),
                      button("−", "zoom-out", **{"aria-label": "Zoom out"}),
                      button("Reset", "reset", **{"aria-label": "Reset graph selection and layout"}),
                      button("PNG ↓", "png", **{"aria-label": "Download graph image"}),
                      button("JSON ↓", "export", **{"aria-label": "Download complete filtered source map"})],
                     className="collection-graph-actions"),
        ], className="collection-graph-toolbar"),
        html.Div([html.Span([html.I(className={"Outlet": "legend-outlet", "Article": "legend-article"}.get(kind, ""),
                                   style={"backgroundColor": color}, **{"aria-hidden": "true"}), TYPE_NAMES[kind]])
                  for kind, color in COLORS.items()], className="collection-graph-legend"),
        html.Div([html.Span([html.I(style={"backgroundColor": TEXT_READY_COLOR}, **{"aria-hidden": "true"}), "Ring: searchable text"]),
                  html.Span([html.I(style={"backgroundColor": METADATA_ONLY_COLOR}, **{"aria-hidden": "true"}), "Ring: metadata only"]),
                  html.Span("Inside a node: one dot per record. Line width: shared record count.")], className="collection-graph-legend collection-graph-ring-legend"),
        html.P("Company / sponsor — co-listed in the same advertising records — news outlet", className="collection-graph-relation-key"),
        html.Details([html.Summary("Relations, source fields & counting rules"),
                      html.P("Entity links summarize source-listed sponsor → advertising record → published outlet. In Articles view, directed lines show the separate source fields. Node size counts records; line width counts records co-listing two names. Other connections fade without being removed."),
                      html.P("Source-listed companies, associations and events remain distinct. These associations do not independently verify payment or business relationships.")],
                     className="collection-graph-reading-guide"),
        html.Div(id=f"{PREFIX}-coverage", className="collection-graph-coverage", role="status"),
        html.Div([
            html.Div(cyto.Cytoscape(
                id=f"{PREFIX}-canvas", elements=[], stylesheet=map_stylesheet(), layout=map_layout("preset"),
                # The built-in responsive pan calculation divides by the
                # previous viewport size, which can be zero while this tab is
                # hidden. Positive-size notifications below request a fresh
                # fit instead; Cytoscape still resizes its native canvas.
                responsive=False, minZoom=.06, maxZoom=4,
                userPanningEnabled=True, userZoomingEnabled=True, boxSelectionEnabled=False,
                style={"width": "100%", "height": "100%"},
            ), className="collection-graph-canvas"),
            html.Aside([
                html.Div(id=f"{PREFIX}-selection-heading", **{"aria-live": "polite"}),
                html.Div(id=f"{PREFIX}-relations", **{"aria-live": "polite"}),
                html.Div(id=f"{PREFIX}-counts"),
                dcc.Graph(id=f"{PREFIX}-breakdown", figure=breakdown_figure(None),
                          config={"displayModeBar": False, "responsive": True}, style={"display": "none", "height": "290px"}),
                html.P(id=f"{PREFIX}-breakdown-note", className="scope-note"),
                html.Div([html.Label("Show supporting records for", htmlFor=f"{PREFIX}-bucket"),
                          dcc.Dropdown(id=f"{PREFIX}-bucket", options=[], value=ALL_BUCKETS,
                                       clearable=False, searchable=False, disabled=True)], className="collection-graph-bucket"),
                html.Div([button("Download selected counts ↓", "counts-export"),
                          html.Span(id=f"{PREFIX}-counts-status", role="status")], className="collection-graph-count-actions"),
                html.Div([button("Previous records", "prev", disabled=True),
                          button("Next records", "next", disabled=True)], className="pager"),
                html.Div(id=f"{PREFIX}-inspector", **{"aria-live": "polite"}),
            ], className="collection-graph-aside", **{"aria-label": "Graph selection and supporting records"}),
        ], id=f"{PREFIX}-stage", className="collection-graph-stage"),
        html.Div(id=f"{PREFIX}-export-status", role="status"),
        dcc.Store(id=f"{PREFIX}-selection"), dcc.Store(id=f"{PREFIX}-offset", data=0),
        dcc.Store(id=f"{PREFIX}-bucket-selection", data=ALL_BUCKETS),
        dcc.Store(id=f"{PREFIX}-anchor"), dcc.Download(id=f"{PREFIX}-download"),
        dcc.Store(id=f"{PREFIX}-size"),
        dcc.Download(id=f"{PREFIX}-counts-download"),
    ], id=f"{PREFIX}-panel", className="collection-graph-panel")


def _filters(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("filters"), dict):
        raise ValueError("Missing trusted collection selection")
    if snapshot["filters"].get("dataset") != "native":
        raise ValueError("Unsupported collection")
    return Filters.model_validate(snapshot["filters"])


def _active(pathname, view, dataset):
    return pathname in {"/data", "/data/"} and view == "relationships" and dataset == "native"


def _offset(value):
    return max(0, value) // PAGE_SIZE * PAGE_SIZE if type(value) is int else 0


def _from_json(value):
    try:
        return json.loads(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _options(graph, mode, anchor, record_ids=None, selected=None):
    nodes, edges = visible_map(graph, mode, anchor, record_ids=record_ids)
    selection, item = selection_item(graph, selected)
    # A valid Article/source-edge focus can outlive a switch to Entities; a
    # summary-edge focus can be expanded as Articles. Keep that canonical
    # option so the controlled dropdown does not clear its value and trigger
    # a second callback that erases the relationship being inspected.
    if item and selection["kind"] == "node" and all(node["id"] != item["id"] for node in nodes):
        nodes = [*nodes, item]
    elif item and selection["kind"] == "edge" and all(edge["id"] != item["id"] for edge in edges):
        edges = [*edges, item]
    lookup = {node["id"]: node for node in graph["nodes"]}
    options = [{"label": f"{TYPE_NAMES[node['type']]}: {node['label']}",
                "value": json.dumps({"kind": "node", "id": node["id"]}, sort_keys=True)} for node in nodes]
    options.extend({"label": f"Relation: {lookup[edge['source']]['label']} → {lookup[edge['target']]['label']}",
                    "value": json.dumps({"kind": "edge", "id": edge["id"]}, sort_keys=True)} for edge in edges)
    return options


def _focus_title(graph, selected, category=None):
    selection, item = selection_item(graph, selected)
    if not item:
        return ""
    lookup = {node["id"]: node for node in graph["nodes"]}
    if category and category.get("selection"):
        return _focus_title(graph, category["selection"])
    title = item["label"] if selection["kind"] == "node" else f"{lookup[item['source']]['label']} → {lookup[item['target']]['label']}"
    if category:
        if category["missing"] and item.get("type") == "Outlet":
            return f"{category['label']} → {title}"
        if item.get("type") == "SponsorCandidate":
            return f"{title} → {category['label']}"
        return f"{title} · {category['label']}"
    return title


def _selection_heading(graph, selected, category=None):
    selection, item = selection_item(graph, selected)
    if not item:
        return [html.H3("Explore a connection"), html.P("Select a labeled node, a line, or one of the client questions above.", className="muted")]
    count = category["count"] if category else len(item["record_ids"])
    relation = selection["kind"] == "edge" or category and category.get("edge_id")
    heading = [html.P("Selected source-record relationship" if relation else TYPE_NAMES[item["type"]], className="eyebrow"),
               html.H3(_focus_title(graph, selected, category)),
               html.P(f"{count:,} supporting records", className="collection-graph-total")]
    if selection["kind"] == "node" and item["type"] == "SponsorCandidate":
        metadata = sponsor_metadata(item["properties"]["source_value"])
        if metadata["entity_type"] in {"conference/event", "industry association"}:
            heading.append(html.P(f"Source category: {metadata['entity_type']}. " + metadata["note"], className="scope-note"))
    # The DOM bridge resets the inspector scroll only when the focus changes,
    # keeping paging and viewport adjustments at the user's reading position.
    return html.Div(heading, **{"data-collection-focus": json.dumps([selection, category["id"] if category else ALL_BUCKETS], sort_keys=True)})


def _relations(graph, selected, category=None):
    focus = selection_relationships(graph, selected, record_ids=category["record_ids"] if category else None)
    if not focus:
        return None
    selection, item = selection_item(graph, selected)
    parts = []
    if selection["kind"] == "node" and item["type"] != "Article" and category is None:
        kind = "news outlets" if item["type"] == "SponsorCandidate" else "sponsor / organization names"
        parts.append(html.P(f"{item['label']} is co-listed with {len(focus['relations']):,} named {kind} in these records. Select a connection below to inspect its articles.", className="scope-note"))
    else:
        labels = {"derived_source_association": "Co-listed in source records",
                  "source_lists_sponsor": "Source lists sponsor", "published_in": "Published in"}
        parts.append(html.H4("Article source relationships" if focus["article_paths"] else "Selected relationship"))
        for row in focus["relations"]:
            parts.append(html.Div([
                html.Span(row["source"], className="collection-graph-relation-source"),
                html.Span("→ " + labels[row["predicate"]] + " →", className="collection-graph-relation-predicate"),
                html.Strong(row["target"]),
                html.P(f"{row['count']:,} supporting records", className="scope-note"),
                *[html.P(f"{share['label']}: {share['count']:,} of {share['total_records']:,} records ({share['share']:.1%})", className="scope-note")
                  for share in row["endpoint_shares"]],
            ], className="collection-graph-relation" + (" is-selected" if row["selected"] else ""),
                **{"data-predicate": row["predicate"], "data-relation-id": row["id"]}))
    for field in ("sponsor", "outlet"):
        missing = focus[f"missing_{field}_records"]
        if missing:
            parts.append(html.P(f"{missing:,} selected records have no source-listed {field}. No relationship is asserted for that missing field.", className="scope-note"))
    return parts


def _bucket_options(detail):
    if not detail:
        return []
    return [{"label": f"All supporting records ({detail['total_records']:,})", "value": ALL_BUCKETS},
            *[{"label": f"{row['label']} · {row['count']:,} ({row['share']:.1%})", "value": row["id"]}
              for row in detail["categories"]]]


def _count_table(detail, bucket):
    if not detail or not detail["categories"]:
        return None
    return html.Table([
        html.Caption(detail["dimension"], className="visually-hidden"),
        html.Thead(html.Tr([html.Th("Source name" if detail["kind"] in {"SponsorCandidate", "Outlet"} else "Published", scope="col"),
                            html.Th("Records", scope="col"), html.Th("Share", scope="col")])),
        html.Tbody([html.Tr([
            html.Th(html.Button([html.I(style={"backgroundColor": row.get("color", "#647f95")}, **{"aria-hidden": "true"}), row["label"]],
                                className="collection-graph-category", type="button",
                                **{"data-collection-bucket": row["id"], "aria-pressed": "true" if row["id"] == bucket else "false"}), scope="row"),
            html.Td(f"{row['count']:,}"), html.Td(f"{row['share']:.1%}")], className="is-selected" if row["id"] == bucket else "")
                    for row in detail["categories"]]),
        html.Tfoot(html.Tr([html.Th("All supporting records", scope="row"), html.Td(f"{detail['total_records']:,}"), html.Td("100%")]))],
        className="collection-graph-count-table")


def _counts(detail, bucket):
    if not detail:
        return None
    title = {"SponsorCandidate": "Connected outlets", "Outlet": "Connected sponsors / organizations"}.get(detail["kind"], "Publication years")
    return [html.H4(title), html.P(f"Distribution for {detail['title']} · all {detail['total_records']:,} supporting records", className="scope-note"),
            _count_table(detail, bucket)]


def _inspector(graph, selected, offset, links_enabled, bucket=ALL_BUCKETS):
    selection, item = selection_item(graph, selected)
    if not item:
        return (html.P("Select a sponsor, outlet or relationship. You can also search by name with the keyboard.", className="muted"),
                0, True, True)
    category = resolve_breakdown_slice(graph, selected, bucket) if bucket != ALL_BUCKETS else None
    ids = set(category["record_ids"] if category else item.get("record_ids", []))
    rows = [row for row in graph["records"] if row["record_id"] in ids]
    rows.sort(key=lambda row: (not bool(row.get("date")), -(int((row.get("date") or "0000-00-00").replace("-", ""))), row["record_id"]))
    total = len(rows)
    unknown = sum(not row.get("date") for row in rows)
    offset = min(_offset(offset), max(0, (total - 1) // PAGE_SIZE * PAGE_SIZE))
    page = rows[offset:offset + PAGE_SIZE]
    parts = []
    if selection["kind"] == "edge":
        parts = [html.P(graph["predicate_definitions"][item["predicate"]]["description"], className="scope-note"),
                 html.Details([html.Summary("Relation provenance"),
                               html.Pre(json.dumps(item.get("provenance", {}), ensure_ascii=False, indent=2))])]
    parts.extend([html.H4("Supporting articles" if category is None else f"Articles: {category['label']}"),
                  html.P(f"{total:,} matching records · {unknown:,} unknown dates", className="scope-note"),
                  html.P(f"Showing {offset + 1 if page else 0:,}–{offset + len(page):,} of {total:,}", className="muted"),
                  *record_cards(page, links_enabled),
                  html.Details([html.Summary("Graph reference"), html.Code(item["id"])])])
    return parts, offset, offset == 0, offset + PAGE_SIZE >= total


def _coverage(graph, mode, anchor, record_ids=None):
    c, counts = graph["coverage"], graph["counts"]
    nodes, edges = visible_map(graph, mode, anchor, record_ids=record_ids)
    heading = ("Complete current selection" if not c.get("truncated") and c["shown_records"] == c["total_records"]
               else "Partial current selection")
    detail = (f"{heading}: {c['shown_records']:,} of {c['total_records']:,} eligible records · "
            f"{counts['sponsors']:,} sponsor / organization names · {counts['outlets']:,} news outlets · "
            f"{counts['summary_edges']:,} counted source associations. "
            f"This view shows {len(nodes):,} nodes and {len(edges):,} relations. "
            f"Missing source fields: {c['missing_sponsor_records']:,} sponsor, {c['missing_outlet_records']:,} outlet. "
            + ("Articles are limited to the explicitly expanded selection. " if mode == "articles" and anchor else "")
            + "Counts describe this stored collection, not all advertising published elsewhere.")
    summary = (f"{c['shown_records']:,} records · {counts['sponsors']:,} sponsor names · "
               f"{counts['outlets']:,} outlets · {counts['summary_edges']:,} associations · "
               f"{len(nodes):,} nodes / {len(edges):,} lines")
    if mode == "articles" and anchor:
        expanded = sum(node["type"] == "Article" for node in nodes)
        summary = (f"{expanded:,} of {c['shown_records']:,} records expanded · "
                   f"{len(nodes):,} nodes / {len(edges):,} lines")
    if heading.startswith("Partial"):
        summary = "Partial selection · " + summary
    return [html.Span(summary), html.Details([html.Summary("Scope & missing fields"), html.P(detail)])]


def register_collection_graph(app, service, links_enabled):
    def load(snapshot):
        filters = _filters(snapshot)
        graph = service.knowledge_map(filters)
        validate_collection_map(graph)
        return graph

    @app.callback(
        Output(f"{PREFIX}-canvas", "elements"), Output(f"{PREFIX}-canvas", "stylesheet"),
        Output(f"{PREFIX}-canvas", "layout"), Output(f"{PREFIX}-canvas", "zoom"), Output(f"{PREFIX}-canvas", "pan"),
        Output(f"{PREFIX}-coverage", "children"), Output(f"{PREFIX}-inspector", "children"),
        Output(f"{PREFIX}-selection", "data"), Output(f"{PREFIX}-offset", "data"),
        Output(f"{PREFIX}-prev", "disabled"), Output(f"{PREFIX}-next", "disabled"),
        Output(f"{PREFIX}-find", "options"), Output(f"{PREFIX}-find", "value"), Output(f"{PREFIX}-anchor", "data"),
        Output(f"{PREFIX}-selection-heading", "children"), Output(f"{PREFIX}-breakdown", "figure"),
        Output(f"{PREFIX}-breakdown", "style"), Output(f"{PREFIX}-breakdown-note", "children"),
        Output(f"{PREFIX}-bucket", "options"), Output(f"{PREFIX}-bucket", "value"),
        Output(f"{PREFIX}-bucket", "disabled"), Output(f"{PREFIX}-bucket-selection", "data"),
        Output(f"{PREFIX}-counts", "children"),
        Output(f"{PREFIX}-relations", "children"),
        Input("native-network-data", "data"), Input("page-location", "pathname"),
        Input("native-view", "value"), Input("active-dataset", "value"),
        Input(f"{PREFIX}-canvas", "tapNodeData"), Input(f"{PREFIX}-canvas", "tapEdgeData"),
        Input(f"{PREFIX}-find", "value"), Input(f"{PREFIX}-view", "value"), Input(f"{PREFIX}-layout", "value"),
        Input(f"{PREFIX}-fit", "n_clicks"), Input(f"{PREFIX}-reset", "n_clicks"),
        Input(f"{PREFIX}-prev", "n_clicks"), Input(f"{PREFIX}-next", "n_clicks"),
        Input(f"{PREFIX}-zoom-in", "n_clicks"), Input(f"{PREFIX}-zoom-out", "n_clicks"),
        Input(f"{PREFIX}-breakdown", "clickData"), Input(f"{PREFIX}-bucket", "value"),
        Input(f"{PREFIX}-size", "data"),
        *[Input(f"{PREFIX}-{name}", "n_clicks") for name in EXAMPLES],
        State(f"{PREFIX}-selection", "data"), State(f"{PREFIX}-offset", "data"), State(f"{PREFIX}-anchor", "data"),
        State(f"{PREFIX}-canvas", "zoom"), State(f"{PREFIX}-canvas", "pan"),
        State(f"{PREFIX}-bucket-selection", "data"),
        State(f"{PREFIX}-canvas", "elements"),
    )
    def explore(snapshot, pathname, view, dataset, node, edge, find, mode, layout_name, fit, reset,
                previous, following, zoom_in, zoom_out, clicked, bucket_value, viewport_size, company_example, outlet_example,
                count_example, selected, offset, anchor, zoom, pan, stored_bucket, current_elements):
        if not _active(pathname, view, dataset):
            raise PreventUpdate
        trigger = ctx.triggered_id
        if trigger == f"{PREFIX}-size" and (
            not isinstance(viewport_size, dict)
            or any(type(viewport_size.get(key)) is not int or not 0 < viewport_size[key] <= 100000
                   for key in ("width", "height", "revision"))
        ):
            raise PreventUpdate
        mode = "articles" if mode == "articles" else "entities"
        fresh = trigger in {"native-network-data", "page-location", "native-view", "active-dataset", None}
        # A resize/selection request can supersede an initial loading request
        # before Dash installs its elements. Recover an empty canvas using the
        # trusted graph; populated canvases retain their dragged positions.
        # Client element contents are never used to construct graph data.
        empty_canvas = not isinstance(current_elements, list) or not current_elements
        rebuild = fresh or empty_canvas or trigger in {f"{PREFIX}-view", f"{PREFIX}-layout", f"{PREFIX}-reset"}
        try:
            graph = load(snapshot)
            selected, _ = selection_item(graph, selected)
            anchor, _ = selection_item(graph, anchor)
            offset = _offset(offset)
            bucket = stored_bucket or ALL_BUCKETS
            example = EXAMPLES.get(str(trigger).removeprefix(f"{PREFIX}-"))
            if trigger == "native-network-data" or trigger == f"{PREFIX}-reset":
                selected, anchor, offset, bucket = None, None, 0, ALL_BUCKETS
            elif trigger == f"{PREFIX}-canvas":
                prop = next(iter(ctx.triggered_prop_ids), "")
                tapped, kind = (edge, "edge") if prop.endswith("tapEdgeData") else (node, "node")
                selected, _ = selection_item(graph, {"kind": kind, "id": tapped.get("id")} if isinstance(tapped, dict) else None)
                offset, bucket = 0, ALL_BUCKETS
            elif trigger == f"{PREFIX}-find":
                selected, _ = selection_item(graph, _from_json(find))
                offset, bucket = 0, ALL_BUCKETS
            elif example:
                selected = next(({"kind": "node", "id": item["id"]} for item in graph["nodes"]
                                 if item["type"] == example[0] and item["label"] == example[1]), None)
                offset, bucket = 0, ALL_BUCKETS
            elif trigger == f"{PREFIX}-breakdown":
                points = clicked.get("points") if isinstance(clicked, dict) else None
                point = points[0] if isinstance(points, list) and points and isinstance(points[0], dict) else {}
                bucket = point.get("customdata", ALL_BUCKETS)
                offset = 0
            elif trigger == f"{PREFIX}-bucket":
                bucket, offset = bucket_value, 0
            elif trigger == f"{PREFIX}-prev":
                offset = max(0, offset - PAGE_SIZE)
            elif trigger == f"{PREFIX}-next":
                offset += PAGE_SIZE
            detail = selection_breakdown(graph, selected)
            category = resolve_breakdown_slice(graph, selected, bucket) if bucket != ALL_BUCKETS else None
            if not category:
                bucket = ALL_BUCKETS
            effective_selection = (category.get("selection") or selected) if category else selected
            record_ids = category["record_ids"] if category else None
            # Every explicit selection in Articles view replaces the expanded
            # scope, including clicks on nodes/edges inside a previous subset.
            # Otherwise the sidebar would describe B while the canvas keeps A.
            if trigger == f"{PREFIX}-view" or mode == "articles" and (
                example or trigger in {f"{PREFIX}-find", f"{PREFIX}-canvas"}
            ):
                anchor = selected if mode == "articles" else None
                offset = 0
                rebuild = True
            if mode == "articles" and trigger in {f"{PREFIX}-breakdown", f"{PREFIX}-bucket"} and bucket != stored_bucket:
                rebuild = True
                anchor = selected
            inspector, offset, prev_disabled, next_disabled = _inspector(graph, selected, offset, links_enabled, bucket)
            out_zoom, out_pan = no_update, no_update
            out_layout = map_layout(layout_name) if rebuild else no_update
            if trigger == f"{PREFIX}-fit":
                # Preset keeps manually dragged positions and asks Cytoscape to
                # fit again. Alternating 1px padding also makes repeated presses observable.
                out_layout = {**map_layout("preset"), "padding": 22 + (fit or 0) % 2}
            elif trigger == f"{PREFIX}-size":
                # Distinct padding makes each completed resize observable to
                # React without resetting dragged positions or the selection.
                out_layout = {**map_layout("preset"), "padding": 22.5 + viewport_size["revision"] % 2 * .25}
            elif trigger in {f"{PREFIX}-zoom-in", f"{PREFIX}-zoom-out"}:
                z = zoom if isinstance(zoom, (int, float)) and not isinstance(zoom, bool) else 1
                out_zoom = min(4, max(.06, z * (1.25 if trigger.endswith("zoom-in") else .8)))
            if trigger == f"{PREFIX}-reset":
                out_pan = {"x": 0, "y": 0}
            note = (f"{detail['dimension']} · denominator: all {detail['total_records']:,} supporting records in the current filters. "
                    "Select a slice or a row to read its articles." if detail else "")
            if detail and category:
                note = (f"The chart and table retain the full distribution for {detail['title']} ({detail['total_records']:,} records). "
                        f"The selected relationship and article list contain {category['count']:,} records. " + note)
            if detail and detail["unknown_date_records"]:
                note += f" {detail['unknown_date_records']:,} records have an unknown date."
            heading = _selection_heading(graph, selected, category)
            if example and not selected:
                heading = [html.H3(example[1]), html.P("No supporting records in the current filters. Adjust the collection filters to explore this name.", className="muted")]
            return (map_elements(graph, mode, anchor, record_ids=record_ids) if rebuild else no_update,
                    map_stylesheet(graph, mode, effective_selection, anchor, record_ids=record_ids), out_layout, out_zoom, out_pan,
                    _coverage(graph, mode, anchor, record_ids), inspector, selected, offset, prev_disabled, next_disabled,
                    _options(graph, mode, anchor, record_ids, selected), json.dumps(selected, sort_keys=True) if selected else None, anchor,
                    heading, breakdown_figure(detail, bucket), {"height": "290px"} if detail and detail["categories"] else {"display": "none", "height": "290px"},
                    note, _bucket_options(detail), bucket, not bool(detail and detail["categories"]), bucket,
                    _counts(detail, bucket), _relations(graph, selected, category))
        except Exception:
            message = "The source graph is temporarily unavailable. Your collection filters are preserved; try again."
            return ([], map_stylesheet(), map_layout(), no_update, no_update, message, html.P(message),
                    None, 0, True, True, [], None, None, html.H3("Selection unavailable"), breakdown_figure(None),
                    {"display": "none"}, "", [], ALL_BUCKETS, True, ALL_BUCKETS, None, None)

    @app.callback(Output(f"{PREFIX}-panel", "className"), Output(f"{PREFIX}-expand", "children"),
                  Input(f"{PREFIX}-expand", "n_clicks"), prevent_initial_call=True)
    def expand(clicks):
        active = type(clicks) is int and clicks % 2 == 1
        return ("collection-graph-panel collection-graph-expanded" if active else "collection-graph-panel",
                "Exit expanded view" if active else "Expand view")

    @app.callback(Output(f"{PREFIX}-canvas", "generateImage"), Input(f"{PREFIX}-png", "n_clicks"),
                  prevent_initial_call=True)
    def image(clicks):
        if not clicks:
            raise PreventUpdate
        return {"type": "png", "action": "download", "filename": "advertising-source-map"}

    @app.callback(Output(f"{PREFIX}-counts-download", "data"), Output(f"{PREFIX}-counts-status", "children"),
                  Input(f"{PREFIX}-counts-export", "n_clicks"), Input("native-network-data", "data"),
                  Input(f"{PREFIX}-selection", "data"), Input(f"{PREFIX}-bucket-selection", "data"), State("page-location", "pathname"),
                  State("native-view", "value"), State("active-dataset", "value"), prevent_initial_call=True)
    def export_counts(clicks, snapshot, selected, bucket, pathname, view, dataset):
        if ctx.triggered_id != f"{PREFIX}-counts-export":
            return no_update, ""
        if not clicks or not _active(pathname, view, dataset):
            raise PreventUpdate
        try:
            graph = load(snapshot)
            detail = selection_breakdown(graph, selected)
            if not detail:
                return None, "Select a node or relationship first."
            category = resolve_breakdown_slice(graph, selected, bucket) if bucket != ALL_BUCKETS else None
            categories = [category] if category else detail["categories"]
            title = _focus_title(graph, selected, category)
            output = StringIO()
            writer = csv.writer(output)
            writer.writerow(["Selection", "Dimension", "Source category", "Exact source value", "Records", "Share", "Denominator", "Missing source field"])
            for row in categories:
                writer.writerow([csv_safe_cell(value) for value in [title, detail["dimension"], row["label"],
                                 row.get("source_value", ""), row["count"], row["share"],
                                 detail["total_records"], row["missing"]]])
            return {"content": "\ufeff" + output.getvalue(), "filename": "advertising-selection-counts.csv", "type": "text/csv"}, \
                   f"Prepared {len(categories):,} categories for {sum(row['count'] for row in categories):,} supporting records. Shares use the parent denominator of {detail['total_records']:,}."
        except Exception:
            return None, "Count download is temporarily unavailable."

    @app.callback(Output(f"{PREFIX}-download", "data"), Output(f"{PREFIX}-export-status", "children"),
                  Input(f"{PREFIX}-export", "n_clicks"), State("native-network-data", "data"),
                  State("page-location", "pathname"), State("native-view", "value"), State("active-dataset", "value"),
                  prevent_initial_call=True)
    def export(clicks, snapshot, pathname, view, dataset):
        if not clicks or not _active(pathname, view, dataset):
            raise PreventUpdate
        try:
            graph = load(snapshot)
            return {"content": json.dumps(graph, ensure_ascii=False, indent=2),
                    "filename": "native-selected-source-map.json", "type": "application/json"}, \
                   f"Prepared complete filtered map: {graph['coverage']['total_records']:,} eligible records."
        except Exception:
            return None, "Source map download is temporarily unavailable."
