"""Dash interactions for the source-field network and paged record drilldown."""

import json
from urllib.parse import quote

from dash import Input, Output, State, ctx, html

from .models import Filters
from .network import network_figure, relationship_label, select_relationship
from .service import safe_url


def record_cards(rows, links_enabled=True):
    """Shared compact record drilldown; keep raw references collapsed by default."""
    cards = []
    for row in rows:
        title = row.get("title") or "Untitled record"
        source = safe_url(row.get("url")) if links_enabled else ""
        metadata = [
            html.Span(relationship_label("sponsor", row.get("sponsor")), className="record-sponsor"),
            " · ",
            html.Span(relationship_label("publisher", row.get("publisher")), className="record-outlet"),
        ]
        cards.append(html.Article([
            html.Div([
                html.A(title, href="/records/" + quote(str(row["record_id"]), safe=""),
                       target="_blank", rel="noopener noreferrer", **{"aria-label": f"Open record: {title}"}),
                html.Span(row.get("date") or "Unknown date", className="muted"),
            ], className="network-record-heading"),
            html.P(metadata, className="muted record-metadata"),
            html.A("Original source ↗", href=source, target="_blank", rel="noopener noreferrer",
                   className="record-source-link") if source else None,
            html.Details([
                html.Summary("Technical reference"),
                html.Code(f"Record {row['record_id']} · Version {row.get('version_id') or 'unavailable'}"),
                html.P(f"Collection search term: {row['keyword']}") if row.get("keyword") else None,
            ]),
        ], className="network-record"))
    return cards


def register_network(app, service, links_enabled):
    @app.callback(
        Output("network-graph", "figure"), Output("network-coverage", "children"),
        Output("network-records", "children"), Output("network-selection", "data"),
        Output("network-offset", "data"), Output("network-prev", "disabled"),
        Output("network-next", "disabled"), Output("network-focus", "options"),
        Output("network-focus", "value"),
        Input("native-network-data", "data"), Input("network-graph", "clickData"),
        Input("network-focus", "value"), Input("network-reset", "n_clicks"),
        Input("network-limit", "value"), Input("network-prev", "n_clicks"),
        Input("network-next", "n_clicks"), State("network-selection", "data"),
        State("network-offset", "data"),
    )
    def explore(snapshot, clicked, focus, reset, limit, previous, following, selection, offset):
        snapshot = snapshot or {}
        relationships = snapshot.get("relationships", [])
        options = []
        for kind, title in (("sponsor", "Sponsor"), ("publisher", "Outlet")):
            for value in sorted({item[kind] for item in relationships}):
                label = relationship_label(kind, value)
                options.append({"label": f"{title}: {label}", "value": json.dumps({"kind": kind, kind: value}, sort_keys=True)})
        trigger = ctx.triggered_id
        offset = max(0, int(offset or 0))
        if trigger in ("native-network-data", "network-reset", None):
            selection, offset = None, 0
        elif trigger == "network-graph":
            points = (clicked or {}).get("points", [])
            selection = select_relationship(relationships, points[0].get("customdata") if points else None)
            offset = 0
        elif trigger == "network-focus":
            try:
                selection = select_relationship(relationships, json.loads(focus)) if focus else None
            except (ValueError, TypeError):
                selection = None
            offset = 0
        elif trigger == "network-next":
            offset += 10
        elif trigger == "network-prev":
            offset = max(0, offset - 10)
        selection = select_relationship(relationships, selection)
        figure, coverage = network_figure(relationships, selection, limit=limit or 24)
        note = (f"Showing {coverage['shown_relationships']:,} of {coverage['total_relationships']:,} links "
                f"covering {coverage['shown_records']:,} of {coverage['total_records']:,} records in this network view.")
        if coverage["shown_relationships"] < coverage["total_relationships"]:
            note += " Highest-count links are shown first. Use Focus to explore any organization or outlet."
        focus_value = json.dumps(selection, sort_keys=True) if selection and selection["kind"] != "edge" else None
        if not selection:
            return (figure, note, html.P("Select a sponsor, outlet, or count in the graph to view matching records.", className="muted network-empty"),
                    None, 0, True, True, options, None)
        filters = Filters.model_validate(snapshot.get("filters") or {})
        filters.dataset = "native"
        for field, plural in (("sponsor", "sponsors"), ("publisher", "publishers")):
            if field in selection:
                allowed = getattr(filters, plural)
                if allowed and selection[field] not in allowed:
                    return (figure, note, html.P("This selection has changed. Reset the network."),
                            None, 0, True, True, options, None)
                setattr(filters, plural, [selection[field]])
        try:
            page = service.page(filters, offset=offset, limit=10)
        except Exception:
            return (figure, note, html.P("Records are temporarily unavailable. Try again."),
                    selection, offset, offset == 0, True, options, focus_value)
        rows, total = page["rows"], page["total"]
        offset = page.get("offset", offset)
        heading = " → ".join(relationship_label(field, selection[field])
                             for field in ("sponsor", "publisher") if field in selection)
        cards = record_cards(rows, links_enabled)
        details = [html.H4(heading), html.P(f"{total:,} matching records · showing {offset + 1 if rows else 0:,}–{offset + len(rows):,}", className="muted"), *cards]
        return figure, note, details, selection, offset, offset == 0, offset + 10 >= total, options, focus_value
