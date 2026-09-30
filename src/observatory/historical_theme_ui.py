"""Read actual records behind overlapping, unverified historical labels."""

from collections import defaultdict

from dash import Input, Output, State, ctx, dcc, html, no_update

from .models import Filters
from .network_ui import record_cards

PREFIX = "historical-theme"
PAGE_SIZE = 10
LABEL_NOTE = (
    "Historical automated labels are unverified and can overlap. "
    "They are not confirmed themes or greenwashing findings."
)


def _display_label(value):
    return value.split(".")[-1].replace("_", " ").capitalize()


def historical_theme_panel():
    return html.Aside([
        html.Div([
            html.H3("Read the labeled articles"),
            html.Button("Clear", id=f"{PREFIX}-reset", n_clicks=0,
                        className="button button-quiet"),
        ], className="section-heading"),
        html.Label("Historical label", htmlFor=f"{PREFIX}-label"),
        dcc.Dropdown(id=f"{PREFIX}-label", options=[], value=None, clearable=True,
                     placeholder="Select a bar or historical label"),
        html.P(LABEL_NOTE, className="scope-note"),
        html.Div(id=f"{PREFIX}-records", className="network-records", **{"aria-live": "polite"}),
        html.Div([
            html.Button("Previous records", id=f"{PREFIX}-prev", n_clicks=0, disabled=True,
                        className="button button-quiet"),
            html.Button("Next records", id=f"{PREFIX}-next", n_clicks=0, disabled=True,
                        className="button button-quiet"),
        ], className="pager"),
        dcc.Store(id=f"{PREFIX}-selection"), dcc.Store(id=f"{PREFIX}-offset", data=0),
    ], className="record-sidebar historical-theme-records",
        **{"aria-label": "Historical label selection and matching articles"})


def _empty(message="Select a bar or historical label to read its matching articles.", options=None):
    return html.P(message, className="muted"), None, 0, True, True, options if options is not None else [], None


def _active(pathname, view, dataset):
    return pathname in {"/data", "/data/"} and view == "overview" and dataset == "native"


def _offset(value):
    return max(0, value) // PAGE_SIZE * PAGE_SIZE if type(value) is int else 0


def _clicked_label(clicked):
    points = clicked.get("points") if isinstance(clicked, dict) else None
    point = points[0] if isinstance(points, list) and points and isinstance(points[0], dict) else {}
    # Display names may lose taxonomy prefixes; only exact canonical customdata
    # identifies a historical label. Client counts and record IDs are ignored.
    value = point.get("customdata")
    return value if isinstance(value, str) else None


def _membership(rows, canonical_labels, filters):
    members = defaultdict(set)
    allowed_ids = set(filters.record_ids)
    for row in rows:
        identifier = row.get("record_id")
        labels = row.get("labels")
        if (not isinstance(identifier, str) or not identifier
                or (allowed_ids and identifier not in allowed_ids)
                or not isinstance(labels, list)):
            continue
        for label in labels:
            if isinstance(label, str) and label in canonical_labels:
                members[label].add(identifier)
    return members


def register_historical_themes(app, service, links_enabled):
    @app.callback(
        Output(f"{PREFIX}-records", "children"), Output(f"{PREFIX}-selection", "data"),
        Output(f"{PREFIX}-offset", "data"), Output(f"{PREFIX}-prev", "disabled"),
        Output(f"{PREFIX}-next", "disabled"), Output(f"{PREFIX}-label", "options"),
        Output(f"{PREFIX}-label", "value"),
        Input("native-network-data", "data"), Input("page-location", "pathname"),
        Input("native-view", "value"), Input("active-dataset", "value"),
        Input("native-labels-chart", "clickData"), Input(f"{PREFIX}-label", "value"),
        Input(f"{PREFIX}-prev", "n_clicks"), Input(f"{PREFIX}-next", "n_clicks"),
        Input(f"{PREFIX}-reset", "n_clicks"),
        State(f"{PREFIX}-selection", "data"), State(f"{PREFIX}-offset", "data"),
    )
    def show_records(snapshot, pathname, view, dataset, clicked, label, previous, following, reset, selection, offset):
        if not _active(pathname, view, dataset):
            return _empty()
        trigger = ctx.triggered_id
        if trigger == f"{PREFIX}-reset":
            return _empty(options=no_update)
        options = []
        try:
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("filters"), dict):
                raise ValueError("Missing collection selection")
            filters = Filters.model_validate(snapshot["filters"])
            if filters.dataset != "native":
                raise ValueError("Wrong collection")
            facets = service.facets("native")
            canonical = {value for value in facets.get("labels", []) if isinstance(value, str) and value}
            rows = service.browse(filters)
            members = _membership(rows, canonical, filters)
            options = [{"label": f"{_display_label(value)} ({len(members[value]):,})", "value": value}
                       for value in sorted(members)]
            if "native-network-data.data" in ctx.triggered_prop_ids or trigger is None:
                selection, offset = None, 0
            elif trigger == "native-labels-chart":
                selection, offset = _clicked_label(clicked), 0
            elif trigger == f"{PREFIX}-label":
                selection, offset = label, 0
            else:
                offset = _offset(offset)
                if trigger == f"{PREFIX}-prev":
                    offset = max(0, offset - PAGE_SIZE)
                elif trigger == f"{PREFIX}-next":
                    offset += PAGE_SIZE
            if not isinstance(selection, str) or selection not in members:
                message = ("No historical labels are recorded in the current selection. This does not establish that themes are absent."
                           if not members else "Select a bar or historical label to read its matching articles.")
                return _empty(message, options)

            identifiers = sorted(members[selection])
            # Keep every current filter, particularly OR-based historical label
            # filters. Adding the exact matching IDs intersects this chosen label
            # with their result; replacing filters.labels would widen that result.
            record_filters = filters.model_copy(update={"record_ids": identifiers})
            offset = min(_offset(offset), (len(identifiers) - 1) // PAGE_SIZE * PAGE_SIZE)
            page = service.page(record_filters, offset=offset, limit=PAGE_SIZE)
            total, page_rows = page["total"], page["rows"]
            if type(total) is not int or total != len(identifiers) or not isinstance(page_rows, list):
                raise ValueError("Historical label selection changed during the read")
            seen = set()
            for row in page_rows:
                identifier = row.get("record_id")
                if (identifier not in members[selection] or identifier in seen
                        or not isinstance(row.get("labels"), list) or selection not in row["labels"]):
                    raise ValueError("Historical label page is outside the selected membership")
                seen.add(identifier)
            details = [
                html.H4(_display_label(selection)),
                html.Details([html.Summary("Technical label reference"), html.Code(selection)]),
                html.P(f"{total:,} matching records in the current filters · showing "
                       f"{offset + 1 if page_rows else 0:,}–{offset + len(page_rows):,}", className="scope-note"),
                *record_cards(page_rows, links_enabled),
            ]
            return details, selection, offset, offset == 0, offset + PAGE_SIZE >= total, options, selection
        except (TypeError, ValueError):
            return _empty("This historical label selection is unavailable. Refresh the current filters and try again.", options)
        except Exception:
            return _empty("Historical label records are temporarily unavailable. Try again.", options)
