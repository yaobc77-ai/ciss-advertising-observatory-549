"""Complete sponsor/outlet counts with a nearby, paged source-record view."""

from __future__ import annotations

import html as html_text
import textwrap

import plotly.graph_objects as go
from dash import Input, Output, State, ctx, dcc, html

from .analytics import sponsor_display
from .models import Filters
from .network_ui import record_cards

PAGE_SIZE = 10
ALL_RECORDS = "__all_matching_records__"


def explorer_panel():
    """The client's two directional questions stay visible above the matrix."""
    return html.Section([
        html.Span("COMPANIES & NEWS OUTLETS", className="eyebrow"),
        html.H2("Explore a company or a news outlet"),
        html.P("Which publishers appear with ExxonMobil? Which sponsors appear at the Washington Post?",
               className="chart-note"),
        html.Div([
            html.Div([
                html.Label("Start with", htmlFor="research-direction"),
                dcc.RadioItems(id="research-direction", options=[
                    {"label": "Company / sponsor", "value": "sponsor"},
                    {"label": "News outlet", "value": "publisher"},
                ], value="sponsor", inline=True, className="metric-toggle"),
            ]),
            html.Div([
                html.Label("Company / sponsor", id="research-anchor-label", htmlFor="research-anchor"),
                dcc.Dropdown(id="research-anchor", options=[], value=None, clearable=False,
                             placeholder="Choose a company / sponsor"),
            ], className="research-anchor"),
        ], className="research-controls"),
        html.H3(id="research-count-heading"),
        html.P(id="research-count-scope", className="scope-note", role="status"),
        html.Div([
            html.Div([
                html.Div(dcc.Graph(id="research-count-chart", figure=count_figure([], "publisher"),
                                  config={"displayModeBar": False, "responsive": True}),
                         className="research-counts-scroll"),
                html.Details([
                    html.Summary("View every count as a table"),
                    html.Div(id="research-count-table"),
                ], className="research-count-table"),
            ], className="research-counts"),
            html.Aside([
                html.H3("Matching articles"),
                html.Label("Show records for", htmlFor="research-counterpart"),
                dcc.Dropdown(id="research-counterpart", options=[], value=ALL_RECORDS,
                             clearable=False, placeholder="All matching articles"),
                html.Div(id="research-records", className="network-records", **{"aria-live": "polite"}),
                html.Div([
                    html.Button("Previous articles", id="research-prev", n_clicks=0, disabled=True,
                                className="button button-quiet"),
                    html.Button("Next articles", id="research-next", n_clicks=0, disabled=True,
                                className="button button-quiet"),
                ], className="pager"),
                dcc.Store(id="research-record-offset", data=0),
            ], className="record-sidebar research-records", **{"aria-label": "Matching articles"}),
        ], className="exploration-split research-results"),
        html.P("Counts use eligible records in the current filters, including records without searchable text. "
               "Sponsor names are source-listed organizations and may include trade groups or events. "
               "These records document advertisements; they do not establish a contractual partnership.",
               className="scope-note"),
    ], className="research-explorer", id="native-research-explorer")


def _field(direction):
    if direction not in ("sponsor", "publisher"):
        raise ValueError("Unsupported explorer direction")
    return direction + "s"


def _label(field, value):
    return sponsor_display(value) if field == "sponsor" else value


def _default(direction, options):
    values = [option["value"] for option in options]
    for value in values:
        if direction == "sponsor" and value.casefold() == "exxonmobil":
            return value
        # Preference chooses an existing source value; it never merges aliases.
        words = value.casefold().replace("_", " ").replace("-", " ").split()
        if direction == "publisher" and words in (["washington", "post"], ["the", "washington", "post"]):
            return value
    return values[0] if values else None


def _anchor_options(facets, filters, direction):
    field = _field(direction)
    allowed = getattr(filters, field)
    values = facets.get(field, [])
    values = {value for value in values if isinstance(value, str) and value and (not allowed or value in allowed)}
    return [{"label": _label(direction, value), "value": value}
            for value in sorted(values, key=lambda value: (_label(direction, value).casefold(), value))]


def _narrow(filters, field, value):
    """Intersect a source value with current dimension filters, never widen them."""
    plural = _field(field)
    allowed = getattr(filters, plural)
    if allowed and value not in allowed:
        raise ValueError("Selected source value is outside the current filters")
    return filters.model_copy(update={plural: [value]})


def _groups(stats, counterpart):
    groups = []
    for row in stats[_field(counterpart)]:
        name, count = row.get("name"), row.get("count")
        if not isinstance(name, str) or not name or isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("Invalid source count")
        groups.append({"name": name, "count": count})
    # SQL group counts must partition the same eligible-record population.
    if len({item["name"] for item in groups}) != len(groups) or sum(item["count"] for item in groups) != stats["total"]:
        raise ValueError("Source counts are inconsistent")
    return sorted(groups, key=lambda row: (-row["count"], _label(counterpart, row["name"]).casefold(), row["name"]))


def count_figure(groups, counterpart, selected=ALL_RECORDS):
    """All source groups have readable names and directly printed counts."""
    figure = go.Figure()
    if groups:
        labels = [_label(counterpart, item["name"]) for item in groups]
        ticks = ["<br>".join(html_text.escape(line) for line in textwrap.wrap(label, width=32) or [label]) for label in labels]
        figure.add_trace(go.Bar(
            x=[item["count"] for item in groups], y=list(range(len(groups))), orientation="h",
            customdata=[item["name"] for item in groups],
            text=[f"{item['count']:,}" for item in groups], textposition="outside", cliponaxis=False,
            marker_color=["#253a4b" if item["name"] == selected else "#647f95" for item in groups],
            hovertemplate="%{text} native ad records<extra></extra>",
        ))
        figure.update_yaxes(tickmode="array", tickvals=list(range(len(groups))), ticktext=ticks,
                            autorange="reversed", fixedrange=True)
        figure.update_xaxes(range=[0, max(item["count"] for item in groups) * 1.18 + 0.5],
                            title="Native ad records", tickformat=",d", fixedrange=True, rangemode="tozero")
    else:
        figure.add_annotation(text="No matching advertisements in the current filters.", showarrow=False,
                              x=0.5, y=0.5, xref="paper", yref="paper")
        figure.update_xaxes(visible=False)
        figure.update_yaxes(visible=False)
    figure.update_layout(height=max(230, 95 + 54 * len(groups)), margin={"l": 220, "r": 45, "t": 12, "b": 55},
                         paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                         font={"family": "Arial, sans-serif", "size": 14, "color": "#253a4b"},
                         bargap=0.42, showlegend=False, clickmode="event")
    return figure


def _count_table(groups, counterpart):
    return html.Table([
        html.Thead(html.Tr([html.Th("News outlet" if counterpart == "publisher" else "Company / sponsor", scope="col"),
                            html.Th("Native ad records", scope="col")])),
        html.Tbody([html.Tr([html.Th(_label(counterpart, row["name"]), scope="row"), html.Td(f"{row['count']:,}")])
                    for row in groups]),
    ])


def _empty(message, direction="sponsor", options=None, anchor=None):
    figure = count_figure([], "publisher" if direction == "sponsor" else "sponsor")
    return (figure, {"height": f"{figure.layout.height}px"}, "Explore the current selection", message,
            "Company / sponsor" if direction == "sponsor" else "News outlet", options or [], anchor,
            [], ALL_RECORDS, html.P(message, className="muted"), None, 0, True, True)


def register_research_explorer(app, service, links_enabled):
    @app.callback(
        Output("research-count-chart", "figure"), Output("research-count-chart", "style"),
        Output("research-count-heading", "children"), Output("research-count-scope", "children"),
        Output("research-anchor-label", "children"), Output("research-anchor", "options"),
        Output("research-anchor", "value"), Output("research-counterpart", "options"),
        Output("research-counterpart", "value"), Output("research-records", "children"),
        Output("research-count-table", "children"), Output("research-record-offset", "data"),
        Output("research-prev", "disabled"), Output("research-next", "disabled"),
        Input("native-network-data", "data"), Input("research-direction", "value"),
        Input("research-anchor", "value"), Input("research-counterpart", "value"),
        Input("research-count-chart", "clickData"), Input("research-prev", "n_clicks"),
        Input("research-next", "n_clicks"), State("research-record-offset", "data"),
    )
    def explore(snapshot, direction, anchor, counterpart_value, clicked, previous, following, offset):
        trigger = ctx.triggered_id
        options = []
        try:
            _field(direction)
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("filters"), dict):
                raise ValueError("No current filter snapshot")
            filters = Filters.model_validate(snapshot["filters"])
            if filters.dataset != "native":
                raise ValueError("Wrong collection")
            options = _anchor_options(service.facets("native"), filters, direction)
            allowed = {option["value"] for option in options}
            changed_scope = "native-network-data.data" in ctx.triggered_prop_ids
            if trigger == "research-direction" or anchor is None or (changed_scope and anchor not in allowed):
                anchor = _default(direction, options)
            if anchor not in allowed:
                return _empty("Choose a company / sponsor or outlet within the current filters.", direction, options)
            filters = _narrow(filters, direction, anchor)
            opposite = "publisher" if direction == "sponsor" else "sponsor"
            stats = service.statistics(filters)
            groups = _groups(stats, opposite)
            group_values = {item["name"] for item in groups}
            if trigger in (None, "research-direction", "research-anchor") or changed_scope:
                counterpart_value, offset = ALL_RECORDS, 0
            elif trigger == "research-count-chart":
                points = clicked.get("points") if isinstance(clicked, dict) else None
                point = points[0] if isinstance(points, list) and points and isinstance(points[0], dict) else {}
                value = point.get("customdata")
                counterpart_value = value if isinstance(value, str) and value in group_values else ALL_RECORDS
                offset = 0
            elif trigger == "research-counterpart":
                offset = 0
            if counterpart_value != ALL_RECORDS and counterpart_value not in group_values:
                counterpart_value, offset = ALL_RECORDS, 0
            try:
                offset = max(0, int(offset or 0))
            except (TypeError, ValueError, OverflowError):
                offset = 0
            if trigger == "research-prev":
                offset = max(0, offset - PAGE_SIZE)
            elif trigger == "research-next":
                offset += PAGE_SIZE
            record_filters = filters if counterpart_value == ALL_RECORDS else _narrow(filters, opposite, counterpart_value)
            figure = count_figure(groups, opposite, counterpart_value)
            total = stats["total"]
            title = (f"Where does {_label(direction, anchor)} appear?" if direction == "sponsor"
                     else f"Which sponsors appear at {anchor}?")
            scope = (f"{total:,} native ad records across {len(groups):,} source-listed "
                     f"{'news outlets' if opposite == 'publisher' else 'sponsors'} · all current filters applied · all counts shown")
            if stats.get("unknown_dates"):
                scope += f" · {stats['unknown_dates']:,} records have an unknown date"
            counterpart_options = [{"label": f"All matching articles ({total:,})", "value": ALL_RECORDS},
                                   *[{"label": f"{_label(opposite, row['name'])} ({row['count']:,})", "value": row["name"]}
                                     for row in groups]]
            try:
                page = service.page(record_filters, offset=offset, limit=PAGE_SIZE)
                rows, page_total = page["rows"], page["total"]
                offset = page.get("offset", offset)
                records = [html.P(f"{page_total:,} matching records · showing {offset + 1 if rows else 0:,}–{offset + len(rows):,}",
                                  className="scope-note"), *record_cards(rows, links_enabled)]
                if not rows:
                    records.append(html.P("No matching articles in the current filters.", className="muted"))
                prev_disabled, next_disabled = offset == 0, offset + PAGE_SIZE >= page_total
            except Exception:
                records = html.P("Article details are temporarily unavailable. Try again.", className="muted")
                prev_disabled, next_disabled = offset == 0, True
            return (figure, {"height": f"{figure.layout.height}px"}, title, scope,
                    "Company / sponsor" if direction == "sponsor" else "News outlet", options, anchor,
                    counterpart_options, counterpart_value, records, _count_table(groups, opposite), offset,
                    prev_disabled, next_disabled)
        except (TypeError, ValueError):
            return _empty("The current selection is unavailable. Refresh the filters and try again.", direction)
        except Exception:
            return _empty("Counts are temporarily unavailable. Try again.", direction, options, anchor)
