"""Collection tabs and matrix record drilldown within the current filter scope."""

from dash import Input, Output, State, ctx, html

from .analytics import UNKNOWN, sponsor_display
from .models import Filters
from .network_ui import record_cards

PAGE_SIZE = 10


def _select_cell(relationships, selection):
    """Accept any displayed cell, including a zero-count sponsor/outlet pair."""
    if not isinstance(selection, dict) or selection.get("kind") != "edge":
        return None
    sponsor, publisher = selection.get("sponsor"), selection.get("publisher")
    if not isinstance(sponsor, str) or not isinstance(publisher, str):
        return None
    values = {"sponsor": set(), "publisher": set()}
    for item in relationships:
        if not isinstance(item, dict):
            continue
        count = item.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            continue
        for field in values:
            raw = item.get(field)
            values[field].add(str(raw).strip() if raw is not None and str(raw).strip() else UNKNOWN)
    if sponsor in values["sponsor"] and publisher in values["publisher"]:
        return {"kind": "edge", "sponsor": sponsor, "publisher": publisher}
    return None


def _empty(message="Select a matrix cell to see its matching records."):
    return html.P(message, className="muted"), None, 0, True, True


def register_data_views(app, service, links_enabled):
    """Register tab visibility and the native matrix's paged record panel."""
    def register_tabs(dataset):
        @app.callback(
            Output(f"{dataset}-overview", "hidden"),
            Output(f"{dataset}-relationships", "hidden"),
            Output(f"{dataset}-records", "hidden"),
            Input(f"{dataset}-view", "value"),
        )
        def show_view(view):
            allowed = ("overview", "records") if dataset == "social" else ("overview", "relationships", "records")
            view = view if view in allowed else "overview"
            return tuple(view != name for name in ("overview", "relationships", "records"))

    for dataset in ("native", "social"):
        register_tabs(dataset)

    @app.callback(
        Output("matrix-records", "children"),
        Output("matrix-selection", "data"),
        Output("matrix-offset", "data"),
        Output("matrix-prev", "disabled"),
        Output("matrix-next", "disabled"),
        Input("native-network-data", "data"),
        Input("native-relationships-chart", "clickData"),
        Input("matrix-prev", "n_clicks"),
        Input("matrix-next", "n_clicks"),
        Input("matrix-reset", "n_clicks"),
        State("matrix-selection", "data"),
        State("matrix-offset", "data"),
    )
    def show_matrix_records(snapshot, clicked, previous, following, reset, selection, offset):
        trigger = ctx.triggered_id
        if "native-network-data.data" in ctx.triggered_prop_ids or trigger in ("matrix-reset", None):
            return _empty()
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("relationships"), list):
            return _empty("The matrix selection is unavailable. Refresh the filters and try again.")
        try:
            if not isinstance(snapshot.get("filters"), dict):
                raise ValueError("Missing filter snapshot")
            filters = Filters.model_validate(snapshot["filters"])
            if filters.dataset != "native":
                raise ValueError("Wrong collection")
        except (TypeError, ValueError):
            return _empty("The matrix selection is unavailable. Refresh the filters and try again.")
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError, OverflowError):
            offset = 0
        if trigger == "native-relationships-chart":
            points = clicked.get("points") if isinstance(clicked, dict) else None
            point = points[0] if isinstance(points, list) and points and isinstance(points[0], dict) else {}
            # Dash drops object customdata for heatmaps because pointNumber is
            # [row, column]. Raw x/y coordinates survive its event filtering.
            selection = point.get("customdata") if "customdata" in point else {
                "kind": "edge", "sponsor": point.get("y"), "publisher": point.get("x"),
            }
            offset = 0
        elif trigger == "matrix-prev":
            offset = max(0, offset - PAGE_SIZE)
        elif trigger == "matrix-next":
            offset += PAGE_SIZE
        selection = _select_cell(snapshot["relationships"], selection)
        if selection is None:
            return _empty()
        for field, plural in (("sponsor", "sponsors"), ("publisher", "publishers")):
            allowed = getattr(filters, plural)
            if allowed and selection[field] not in allowed:
                return _empty("This cell is outside the current filters. Select a cell in the updated matrix.")
            setattr(filters, plural, [selection[field]])
        try:
            page = service.page(filters, offset=offset, limit=PAGE_SIZE)
            rows, total = page["rows"], page["total"]
            offset = page.get("offset", offset)
            heading = f"{sponsor_display(selection['sponsor'])} → {selection['publisher']}"
            details = [html.H4(heading)]
            if total == 0:
                details.append(html.P("No matching records for this cell under the current filters.", className="muted"))
            else:
                details.append(html.P(
                    f"{total:,} matching records · showing {offset + 1 if rows else 0:,}–{offset + len(rows):,}",
                    className="muted",
                ))
                details.extend(record_cards(rows, links_enabled))
            return details, selection, offset, offset == 0, offset + PAGE_SIZE >= total
        except Exception:
            return (html.P("Records are temporarily unavailable. Try again.", className="muted"),
                    selection, offset, offset == 0, True)
