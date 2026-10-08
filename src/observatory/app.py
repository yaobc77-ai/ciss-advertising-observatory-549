"""Public Dash interface. Only explicitly selected public data reaches the browser."""

from __future__ import annotations

import csv
import inspect
import math
import os
import re
import secrets
import textwrap
from html import escape
from importlib.metadata import PackageNotFoundError, version
from io import StringIO
from pathlib import Path
from urllib.parse import quote, urlsplit

import plotly.graph_objects as go
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate
from flask import request, session

from observatory.analytics import (
    historical_label_distribution,
    sponsor_display,
    sponsor_publisher_csv,
)
from observatory.evaluation_report import published_report
from observatory.evaluation_ui import evaluation_panel
from observatory.models import Filters
from observatory.social_annotations import SCHEME, STATUS, social_state_options
from observatory.social_label_ui import (
    clicked_social_state,
    public_social_states,
    social_label_figure,
    social_label_note,
    social_label_snapshot,
    source_state_name,
)
from observatory.social_source_ui import (
    decorate_source_observation_cards,
    original_text_citation_matches,
)

UNKNOWN = "(Unknown)"
NATIVE_COLUMNS = ("url", "publisher", "title", "date", "sponsor", "keyword")
SOCIAL_COLUMNS = ("platform", "account", "sponsor", "title", "date", "url")
FILTER_NAMES = ("publishers", "sponsors", "platforms", "keywords", "labels", "accounts")
FILTER_VALUE_COUNT = len(FILTER_NAMES) + 3
COLORS = {"ink": "#202633", "teal": "#003262", "muted": "#687587", "amber": "#a86b25"}
QUERY_EXAMPLES = (
    ("example-outlet-count", "Count ads at an outlet", "How many native ads are from the New York Times?"),
    ("example-company-outlets", "Explore a company's publishers", "Which publishers is ExxonMobil working with?"),
    ("example-outlet-sponsors", "Explore an outlet's sponsors", "Which fossil fuel companies has the Washington Post worked with?"),
    ("example-company-claims", "Compare emissions claims", "How do ExxonMobil and Shell describe their efforts to reduce emissions in their native ads? What does each company emphasize? Please cite the relevant advertisements."),
)


def _page(pathname):
    return {
        None: "query",
        "/": "query",
        "/query": "query",
        "/data": "data",
        "/wireframe": "wireframe",
        "/evaluation": "evaluation",
    }.get((pathname.rstrip("/") or "/") if pathname else pathname, "not-found")


def _research_signature(question, scope, dataset, values):
    """Keep the submitted scope separate from controls that can change later."""
    filters = (
        Filters(dataset="all")
        if scope == "all"
        else _filters(dataset, *_collection_filter_values(dataset, values))
    )
    return {
        "question": (question or "").strip(),
        "filters": filters.model_dump(mode="json"),
    }


def _wireframe(health):
    """A structural diagram of this release, not screenshots or simulated records."""
    native_count = health.get("record_counts", {}).get("native", 0)
    social_count = health.get("countable_record_counts", health.get("record_counts", {})).get("social", 0)

    def block(title, note="", class_name=""):
        return html.Div(
            [html.Strong(title), html.Span(note) if note else None],
            className=f"wire-block {class_name}",
        )

    def screen(title, route, items):
        return html.Article(
            [
                html.Div(
                    [html.Span("○ ○ ○"), html.Code(route)], className="wire-browser"
                ),
                html.H3(title),
                block(
                    "Advertising Observatory · Shared navigation",
                    "Query  /  Data · Tools in the top-right corner",
                    "wire-nav",
                ),
                *items,
                dcc.Link(f"Open {title} →", href=route, className="wire-open"),
            ],
            className="wire-screen",
        )

    return [
        html.Div(
            [
                html.Span("Current product structure", className="eyebrow"),
                html.H2("Query, Data and Evaluation"),
                html.P(
                    "Query and Data share collection filters. Evaluation reports measured results and remaining review separately.",
                    className="muted",
                ),
                html.Div(
                    [
                        html.Span(
                            "Native · available"
                            if native_count
                            else "Native · no records loaded",
                            className="status-chip"
                            if native_count
                            else "status-chip status-pending",
                        ),
                        html.Span(
                            "Social · available"
                            if social_count
                            else "Social · no admitted records",
                            className="status-chip status-pending"
                            if not social_count
                            else "status-chip",
                        ),
                    ],
                    className="wire-status",
                ),
            ],
            className="wire-introduction",
        ),
        html.Div(
            [
                screen(
                    "Query",
                    "/query",
                    [
                        block(
                            "Collection + question + answer",
                            "Native / Social dropdown · research question · Generate answer",
                        ),
                        html.Div(
                            [
                                block(
                                    "Tools · closed by default",
                                    "Search scope / keyword search / shared filters / usage notes",
                                ),
                                html.Div(
                                    [
                                        block(
                                            "Research question",
                                            "Current selection or both collections",
                                        ),
                                        html.Div(
                                            [
                                                block(
                                                    "Generate answer",
                                                    "Primary action · uses API budget",
                                                ),
                                                block(
                                                    "Keyword search",
                                                    "Secondary action · no paid call",
                                                ),
                                            ],
                                            className="wire-actions",
                                        ),
                                        block(
                                            "Summary + evidence + limits",
                                            "SQL results or source-grounded findings · original references · separate web supplements",
                                        ),
                                        block(
                                            "Result status",
                                            "Last submitted scope · changes require a new search",
                                        ),
                                    ],
                                    className="wire-stack",
                                ),
                            ],
                            className="wire-stack",
                        ),
                    ],
                ),
                screen(
                    "Data",
                    "/data",
                    [
                        block("Collection selector", "Same selection as Query"),
                        block("Views", "Overview / Relationships / Records"),
                        html.Div(
                            [
                                block(
                                    "Tools · shared filters",
                                    "Collection-specific filters are preserved across pages",
                                ),
                                html.Div(
                                    [
                                        html.Div(
                                            [
                                                block("Selected"),
                                                block("Searchable"),
                                                block("Unknown date"),
                                            ],
                                            className="wire-metrics",
                                        ),
                                        html.Div(
                                            [
                                                block("Outlet / platform", "▂ ▅ ▃ ▇"),
                                                block("Sponsors", "▅ ▃ ▇ ▂"),
                                                block("Timeline", "▁ ▂ ▄ ▂ ▆"),
                                                block("Interactive network", "Sponsor → source records → outlet"),
                                            ],
                                            className="wire-chart-grid",
                                        ),
                                        block(
                                            "Record table + CSV export",
                                            "Server-side pages; SQL charts and export cover the full selection",
                                        ),
                                    ],
                                    className="wire-stack",
                                ),
                            ],
                            className="wire-stack",
                        ),
                    ],
                ),
                screen(
                    "Evaluation",
                    "/evaluation",
                    [
                        block(
                            "Requirements and measures",
                            "Definitions · denominators · calculation and review methods",
                        ),
                        block("Recorded measurements", "Frozen run and collection scope · missing results stay pending"),
                        block(
                            "Review coverage",
                            "Answered, failed, reviewed and pending cases remain separate",
                        ),
                        block(
                            "Acceptance boundaries",
                            "Engineering checks, semantic review and customer acceptance are distinct",
                        ),
                    ],
                ),
            ],
            className="wire-screens",
            **{"aria-label": "Wireframes for Query, Data and Evaluation"},
        ),
        html.Section(
            [
                html.Span("Behind the pages", className="eyebrow"),
                html.H2("Project data flow"),
                html.Div(
                    [
                        block(
                            "01 · Source material",
                            "Native advertisements / social exports / saved source materials",
                        ),
                        html.Span(
                            "→", className="flow-arrow", **{"aria-hidden": "true"}
                        ),
                        block(
                            "02 · Quality + versions",
                            "Source identity · accepted body ranges · immutable original text",
                        ),
                        html.Span(
                            "→", className="flow-arrow", **{"aria-hidden": "true"}
                        ),
                        block(
                            "03 · PostgreSQL + indexes",
                            "Records · source spans · keyword and vector retrieval",
                        ),
                        html.Span(
                            "→", className="flow-arrow", **{"aria-hidden": "true"}
                        ),
                        html.Div(
                            [
                                block(
                                    "Data page",
                                    "SQL counts → matrix / graph / charts → records / CSV",
                                    "wire-output",
                                ),
                                block(
                                    "Query page",
                                    "Interpret → scoped SQL or source retrieval → summary / evidence / limits",
                                    "wire-output",
                                ),
                                block(
                                    "Offline CLAIMS",
                                    "Saved classifier outputs → source checks and review states → published annotations",
                                    "wire-output",
                                ),
                                block(
                                    "Evaluation",
                                    "Frozen runs → measurement and review → results or pending status",
                                    "wire-output",
                                ),
                            ],
                            className="wire-stack",
                        ),
                    ],
                    className="wire-flow",
                ),
                html.P(
                    "Counts describe the selected records. Retrieved passages support what an advertiser said, not whether it is true. Queries read published CLAIMS annotations without rerunning classification. Evaluation is separate from answering.",
                    className="wire-caption",
                ),
            ],
            className="wire-flow-panel",
        ),
    ]


def _mapping(value):
    return value.model_dump() if hasattr(value, "model_dump") else dict(value)


def _url(value):
    """Allow only web destinations, including when the service returns extra data."""
    try:
        value = str(value or "").strip()
        parsed = urlsplit(value)
        return (
            value
            if parsed.scheme.lower() in {"http", "https"} and parsed.netloc
            else ""
        )
    except ValueError:
        return ""


def _public_rows(rows, links_enabled):
    result = []
    for raw in rows:
        item = _mapping(raw)
        row = {
            name: str(item.get(name) or UNKNOWN)
            for name in (
                "publisher",
                "title",
                "date",
                "sponsor",
                "keyword",
                "platform",
                "account",
            )
        }
        row.update(
            {
                name: str(item.get(name) or "")
                for name in ("record_id", "version_id", "dataset")
            }
        )
        row["sponsor_key"] = row["sponsor"]
        row["sponsor"] = sponsor_display(item.get("sponsor"))
        row["labels"] = [str(label) for label in (item.get("labels") or [])]
        row["retrievable"] = bool(item.get("retrievable"))
        row["url"] = _url(item.get("url")) if links_enabled else ""
        row["archive_url"] = _url(item.get("archive_url")) if links_enabled else ""
        if row["dataset"] == "social":
            row["collection_scope"] = str(item.get("collection_scope") or "collected_company_posts")
            row["count_unit"] = str(item.get("count_unit") or "source_record")
            row["paid_ad_status"] = "not_verified"
            try:
                row["social_historical_states"] = public_social_states(item)
                row["social_historical_scheme"] = SCHEME
                row["social_historical_status"] = STATUS
            except ValueError:
                row["social_historical_states"] = []
                row["social_historical_scheme"] = ""
                row["social_historical_status"] = ""
        result.append(row)
    return result


def _collection_filter_values(dataset, values):
    if dataset not in {"native", "social", "all"}:
        raise ValueError("Unknown collection")
    start = {"native": 0, "social": FILTER_VALUE_COUNT, "all": 2 * FILTER_VALUE_COUNT}[dataset]
    return values[start:start + FILTER_VALUE_COUNT]


def _social_available(health):
    counts = health.get("countable_record_counts", health.get("record_counts", {}))
    return counts.get("social", 0) > 0


def _social_admission(health):
    if health.get("status") != "ok" or not _social_available(health):
        raise ValueError("Social records are not admitted")
    counts = health.get("countable_record_counts", health.get("record_counts", {}))
    return {"count": counts["social"], "data_version": health.get("data_version")}


def _current_social_snapshot(service, filters, previous):
    """Validate a live Data selection; it is not a durable membership freeze."""
    scope = filters.model_dump(mode="json")
    if not isinstance(previous, dict) or previous.get("filters") != scope:
        raise ValueError("Refresh the current selection")
    admission = _social_admission(service.health())
    if previous.get("social_admission") != admission:
        raise ValueError("Social admission changed")
    dashboard = service.dashboard(filters, offset=0, limit=1)
    fresh = social_label_snapshot(dashboard.get("social_historical_labels"), scope)
    if (previous.get("social_historical_labels") != fresh
            or fresh["total"] != dashboard["stats"]["total"]
            or _social_admission(service.health()) != admission):
        raise ValueError("The source-state selection changed")
    return fresh


def _filters(dataset, *values):
    selections = dict(zip(FILTER_NAMES, values[:len(FILTER_NAMES)], strict=True))
    if dataset == "all":
        from .combined_ui import decode_company_selection

        if any(selections[name] for name in ("publishers", "platforms", "labels", "accounts")):
            raise ValueError("Collection-specific filters require a specific collection")
        selections["sponsors"] = decode_company_selection(selections["sponsors"] or [])
    date_from, date_to, unknown_dates = values[len(FILTER_NAMES):]
    filters = Filters(
        dataset=dataset,
        **{name: selected or [] for name, selected in selections.items()},
        date_from=date_from or None,
        date_to=date_to or None,
        include_unknown_dates="include" in (unknown_dates or []),
    )
    if filters.date_from and filters.date_to and filters.date_from > filters.date_to:
        raise ValueError("Start date must be on or before end date.")
    return filters


def _figure(message=None):
    figure = go.Figure()
    figure.update_layout(
        template="plotly_white",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={
            "family": "Segoe UI, Arial, sans-serif",
            "color": COLORS["ink"],
            "size": 14,
        },
        margin={"l": 16, "r": 18, "t": 18, "b": 35},
        height=340,
        xaxis={"gridcolor": "#e8ecf0", "zeroline": False, "title_font_size": 11},
        yaxis={"gridcolor": "#e8ecf0", "zeroline": False, "title_font_size": 11},
        bargap=0.3,
        hoverlabel={
            "bgcolor": "#142d4e",
            "font_color": "#ffffff",
            "bordercolor": "#142d4e",
        },
    )
    if message:
        figure.add_annotation(
            text=message,
            showarrow=False,
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            font={"size": 13, "color": COLORS["muted"]},
        )
        figure.update_xaxes(visible=False)
        figure.update_yaxes(visible=False)
    return figure


def _wrap_label(value, width=24):
    return "<br>".join(escape(line) for line in textwrap.wrap(str(value), width=width, break_long_words=False))


def _bars(items, metric="count"):
    items = sorted(items or [], key=lambda item: item.get("count", 0), reverse=True)[
        :10
    ]
    if not items:
        return _figure("No records in this selection")
    items.reverse()
    figure = _figure()
    figure.add_bar(
        y=[str(item.get("name") or UNKNOWN) for item in items],
        x=[item.get(metric, 0) for item in items],
        orientation="h",
        marker_color=COLORS["teal"],
        text=[item.get(metric, 0) for item in items],
        texttemplate="%{text:.1f}%" if metric == "percent" else "%{text:,}",
        textposition="outside",
        textangle=0,
        cliponaxis=False,
        customdata=[[item.get("count", 0), item.get("percent", 0)] for item in items],
        hovertemplate="%{y}<br>%{customdata[0]:,} records · %{customdata[1]:.1f}%<extra></extra>",
    )
    figure.update_xaxes(
        title="Share of selected records (%)" if metric == "percent" else "Records",
        rangemode="tozero",
    )
    figure.update_yaxes(automargin=True, showgrid=False, tickmode="array",
                        tickvals=[str(item.get("name") or UNKNOWN) for item in items],
                        ticktext=[_wrap_label(item.get("name") or UNKNOWN) for item in items])
    figure.update_layout(height=max(320, len(items) * 36 + 65), margin={"l": 16, "r": 50, "t": 18, "b": 45})
    return figure


def _timeline(items):
    if not items:
        return _figure("No records in this selection")
    figure = _figure()
    figure.add_bar(
        x=[item["year"] for item in items],
        y=[item["count"] for item in items],
        text=[item["count"] for item in items],
        textposition="outside",
        textangle=0,
        cliponaxis=False,
        marker_color=[
            COLORS["amber"] if item["year"] == "Unknown" else COLORS["teal"]
            for item in items
        ],
        hovertemplate="%{x}<br>%{y:,} records<extra></extra>",
    )
    figure.update_xaxes(title="Publication year", type="category", tickangle=0, automargin=True)
    figure.update_yaxes(title="Records", rangemode="tozero")
    return figure


def _relationships(items):
    if not items:
        return _figure("No sponsor–outlet relationships in this selection")
    sponsors, publishers = {}, {}
    for item in items:
        sponsor, publisher = (
            item.get("sponsor") or UNKNOWN,
            item.get("publisher") or UNKNOWN,
        )
        sponsors[sponsor] = sponsors.get(sponsor, 0) + item["count"]
        publishers[publisher] = publishers.get(publisher, 0) + item["count"]
    ys = sorted(sponsors, key=lambda s: (-sponsors[s], s))
    xs = sorted(publishers, key=lambda p: (-publishers[p], p))
    cells = {
        (item.get("sponsor") or UNKNOWN, item.get("publisher") or UNKNOWN): item[
            "count"
        ]
        for item in items
    }
    figure = _figure()
    figure.add_heatmap(
        x=xs,
        y=ys,
        z=[[cells.get((y, x), 0) for x in xs] for y in ys],
        customdata=[[{"kind": "edge", "sponsor": y, "publisher": x} for x in xs] for y in ys],
        text=[[escape(sponsor_display(y)) for x in xs] for y in ys],
        texttemplate="%{z}",
        textfont={"size": 14},
        colorscale=[[0, "#f3f5f6"], [0.15, "#d4e1ec"], [0.5, "#7597b4"], [1, COLORS["teal"]]],
        showscale=True,
        colorbar={
            "title": "Records",
            "tickformat": "d",
            "thickness": 12,
            "outlinewidth": 0,
        },
        xgap=2,
        ygap=2,
        hovertemplate="%{text} → %{x}<br>%{z:,} records · Select to view articles<extra></extra>",
    )
    figure.update_layout(
        height=max(380, 38 * len(ys) + 150),
        margin={"l": 16, "r": 22, "t": 90, "b": 45},
        clickmode="event", dragmode=False,
    )
    figure.update_xaxes(automargin=True, tickangle=0, side="top", tickmode="array", tickvals=xs,
                        ticktext=[_wrap_label(x.removeprefix("The "), 12) for x in xs], tickfont={"size": 12}, fixedrange=True, showgrid=False)
    figure.update_yaxes(automargin=True, autorange="reversed", tickmode="array",
                        tickvals=ys,
                        ticktext=[_wrap_label(sponsor_display(y), 22) for y in ys], fixedrange=True, showgrid=False)
    return figure


def _label_chart(rows=None, distribution=None):
    distribution = distribution if distribution is not None else historical_label_distribution(rows or [])
    items = distribution["items"]
    if not items:
        return _figure("No historical labels in this selection")
    figure = _figure()
    figure.add_bar(
        y=[
            item["name"].split(".")[-1].replace("_", " ").capitalize() for item in items
        ][::-1],
        x=[item["count"] for item in items][::-1],
        orientation="h",
        text=[item["count"] for item in items][::-1],
        customdata=[item["name"] for item in items][::-1],
        textposition="outside",
        textangle=0,
        cliponaxis=False,
        marker_color=COLORS["teal"],
    )
    figure.update_layout(height=max(340, 38 * len(items) + 70), margin={"l": 16, "r": 55, "t": 18, "b": 45})
    figure.update_xaxes(title="Records", rangemode="tozero")
    labels = [item["name"].split(".")[-1].replace("_", " ").capitalize() for item in items][::-1]
    figure.update_yaxes(automargin=True, showgrid=False, tickmode="array", tickvals=labels,
                        ticktext=[_wrap_label(label, 26) for label in labels])
    return figure


def _notice(title, message, kind="info"):
    return html.Div(
        [html.Strong(title), html.P(message)],
        className=f"notice notice-{kind}",
        role="status",
    )


def _scope_selections(filters):
    collection = {
        "native": "Native advertising",
        "social": "Company social posts",
        "all": "Both collections · all available records",
    }[filters.dataset]
    selections = [collection]
    for name in FILTER_NAMES:
        values = getattr(filters, name)
        if values:
            display = (sponsor_display if name == "sponsors" else
                       source_state_name if name == "labels" and filters.dataset == "social" else str)
            selections.append(
                f"{_filter_scope_name(name, filters.dataset)}: {', '.join(dict.fromkeys(display(v) for v in values))}"
            )
    if filters.date_from or filters.date_to:
        selections.append(
            f"Dates: {filters.date_from or 'any'} to {filters.date_to or 'any'}"
        )
    selections.append(
        "Unknown dates included"
        if filters.include_unknown_dates
        else "Unknown dates excluded"
    )
    if filters.record_ids:
        selections.append(f"Record subset: {len(filters.record_ids):,} selected record IDs")
    return selections


def _search_context(question, filters):
    selections = _scope_selections(filters)
    return html.Div(
        [
            html.Strong("Last submitted search"),
            html.P(question),
            html.P(" · ".join(selections)),
            html.Small("Run a new search after changing the question, scope or filters."),
        ],
        className="notice notice-info",
    )


def _answer_section(title, children, class_name="answer-section"):
    return html.Section([html.H4(title), *children], className=class_name)


def _answer_frame(title, summary, evidence, scope, *, eyebrow, trace=None, class_name=""):
    """One reading order for every successful query, with typed evidence beneath it."""
    return html.Div([
        html.Span(eyebrow, className="eyebrow"), html.H3(title),
        _answer_section("Summary", summary, "answer-summary"),
        _answer_section("Evidence", evidence) if evidence else None,
        _answer_section("Scope and limits", scope, "answer-section answer-scope") if scope else None,
        trace,
    ], className="answer-card answer-layout " + class_name)


def _answer_status(title, message, next_step, *, eyebrow=None, trace=None, extra=()):
    """An unresolved question gets a status and action, not a fabricated takeaway."""
    return html.Div([
        html.Span(eyebrow, className="eyebrow") if eyebrow else None,
        html.H3(title),
        _answer_section("Status", [html.P(message)], "answer-status"),
        *extra,
        _answer_section("Next step", [html.P(next_step)], "answer-next-step"),
        trace,
    ], className="answer-card answer-layout")


def _statistics_card(result, links_enabled, service, *, include_record_browser=True):
    """Display complete SQL categories and all records through scoped paging."""
    from observatory.query_records import statistics_records_panel

    data = result["structured_result"]
    if data.get("kind") == "social_historical_labels":
        filters = Filters.model_validate(data["filters"])
        snapshot = social_label_snapshot(data.get("distribution"), filters.model_dump(mode="json"))
        if (filters.dataset != "social"
                or data.get("collections") != [{"dataset": "social", "total": snapshot["total"]}]):
            raise ValueError("Historical source-state counts require the social selection")
        table = html.Div(html.Table([
            html.Caption("All 13 historical social source codes · each uses the selected-post denominator"),
            html.Thead(html.Tr([html.Th(label, scope="col") for label in (
                "Source code", "Source label", "Level", "Source True", "Source False", "Unknown annotation",
            )])),
            html.Tbody([html.Tr([
                html.Th(html.Code(item["key"]), scope="row"),
                html.Td(item["label"]), html.Td(item["level"]),
                *[html.Td(f"{item[state]:,}", className="count-value")
                  for state in ("source_true", "source_false", "unknown")],
            ]) for item in snapshot["items"]]),
        ]), className="statistics-table")
        return _answer_frame(
            "Historical social source states", [html.P(
                f"{snapshot['total']:,} selected posts · {snapshot['valid_annotation_records']:,} with a usable "
                f"historical annotation · {snapshot['unknown_annotation_records']:,} unknown.", className="answer-text"),
                html.P("Source True and Source False describe the original export's outputs; Unknown annotation means no usable current-text annotation binding.")],
            [table, statistics_records_panel(data, service, links_enabled) if include_record_browser else
             html.P("Submit this question part separately to inspect its matching records.", className="scope-note")],
            [html.P("Each code uses the same selected-post denominator. Codes can overlap; do not sum their counts. Source True/False are historical automated outputs, not reviewed themes, greenwashing or fact checks.", className="scope-note"),
             html.P("Unknown annotation means no usable annotation is bound to the current stored text. It does not mean Source False. These source states use a separate scheme from native labels and CLAIMS2.", className="scope-note"),
             *[html.P(note, className="scope-note") for note in data.get("scope_notes", [])]],
            eyebrow="Collection statistics · model-assisted query" if result.get("research_trace")
            else "Collection statistics · no model charge",
            trace=_research_steps(result), class_name="statistics-answer",
        )
    collections = data.get("collections", [])
    groups = data.get("groups", [])
    names = {"native": "Native ad records", "social": "Company posts"}
    model_query = bool(result.get("research_trace"))
    is_share = data.get("kind") == "share"
    is_time = data.get("kind") in {"list_years", "top_years", "compare_periods"}
    sections = []
    if is_share:
        sections.append(html.Div(html.Table([
            html.Caption("Matching records as a share of the current selection, by collection"),
            html.Thead(html.Tr([html.Th(label, scope="col") for label in (
                "Collection", "Matching records", "Current selection", "Share",
            )])),
            html.Tbody([html.Tr([
                html.Th(names[item["dataset"]], scope="row"),
                html.Td(f"{item['numerator']:,}", className="count-value"),
                html.Td(f"{item['denominator']:,}", className="count-value"),
                html.Td(f"{item['percentage']:.2f}%" if item.get("percentage") is not None
                        else "Undefined · empty selection", className="share-value"),
            ]) for item in collections]),
        ]), className="statistics-table"))
        denominator = Filters.model_validate(data["denominator_filters"])
        sections.append(html.Details([
            html.Summary("Denominator · current selection before question targets"),
            html.P(" · ".join(_scope_selections(denominator))),
        ], className="statistics-denominator"))
    if data.get("kind") == "compare_periods":
        sections.append(html.Div(html.Table([
            html.Caption("Counts for each requested period"),
            html.Thead(html.Tr([html.Th(label, scope="col") for label in (
                "Period", "Dates (inclusive)", "Collection", "Records",
            )])),
            html.Tbody([html.Tr([
                html.Th(period["label"], scope="row"),
                html.Td(f"{period['filters'].get('date_from') or 'Any start'} to "
                        f"{period['filters'].get('date_to') or 'Any end'}"),
                html.Td(names[item["dataset"]]), html.Td(f"{item['total']:,}", className="count-value"),
            ]) for period in data.get("periods", []) for item in period.get("collections", [])]),
        ]), className="statistics-table"))
    if groups:
        social_groups = all(group.get("dataset") == "social" for group in groups)
        sections.append(html.Div(html.Table([
            html.Caption({"publishers": "All publishers and counts", "sponsors": "All company affiliations and counts" if social_groups else "All source-listed sponsors / organizations and counts",
                          "platforms": "All platforms and counts", "accounts": "All source account names and counts", "years": "Years with the highest count (all ties)"
                          if data.get("kind") == "top_years" else "All years and counts"}.get(data.get("group_by"), "All categories and counts")),
            html.Thead(html.Tr([
                html.Th({"publishers": "News outlet", "sponsors": "Company affiliation" if social_groups else "Source-listed sponsor / organization", "platforms": "Platform", "accounts": "Source account name", "years": "Year"}.get(data.get("group_by"), "Category"), scope="col"),
                html.Th("Collection", scope="col"), html.Th("Records", scope="col"),
            ])),
            html.Tbody([html.Tr([
                html.Th(group.get("display_name") or group["name"], scope="row"),
                html.Td(names[group["dataset"]]), html.Td(f"{group['count']:,}", className="count-value"),
            ]) for group in groups]),
        ]), className="statistics-table"))
    notes = [
        f"{names[item['dataset']]}: {item['total']:,} matching · {item['retrievable']:,} searchable · {item['unknown_dates']:,} with unknown dates."
        for item in collections
    ]
    scope = [
        html.Ul([html.Li(note) for note in notes + data.get("scope_notes", [])], className="scope-note"),
        html.P("Computed from all matching stored records in this selection, including records without searchable text. Native articles and social posts are separate units. These are collection counts, not a census of all advertising.", className="scope-note"),
    ]
    date_note = (data.get("date_inference") or {}).get("note")
    if date_note:
        scope.append(html.P(date_note, className="scope-note"))
    sections.append(statistics_records_panel(data, service, links_enabled) if include_record_browser else
                    html.P("Submit this question part separately to inspect its matching records.", className="scope-note"))
    summary = [html.P(result["answer"], className="answer-text")]
    if groups:
        category_names = list(dict.fromkeys(group.get("display_name") or group["name"] for group in groups))
        summary.append(html.P(
            "Categories in this selection: " + ", ".join(category_names[:5])
            + (". Additional categories are listed below." if len(category_names) > 5 else ".")
        ))
    return _answer_frame(
        "Share of the current selection" if is_share else "Counts across dates" if is_time else "Records in this selection",
        summary, sections, scope,
        eyebrow="Collection statistics · model-assisted query" if model_query
        else "Collection statistics · no model charge",
        trace=_research_steps(result), class_name="statistics-answer",
    )


def _research_steps(result):
    """Public tool execution trace, rather than private model reasoning."""
    trace = result.get("research_trace") or {}
    if not trace:
        return None
    steps = trace.get("tools") or []
    calls = trace.get("model_calls") or []
    call_label = "model call" if len(calls) == 1 else "model calls"
    external = trace.get("external_web") or {}
    web_calls = external.get("model_calls", 0)
    web_note = f" · Web lookup: {web_calls} model call{'s' if web_calls != 1 else ''}" if external else ""
    return html.Details([
        html.Summary("How this question was answered"),
        html.P(f"Question interpretation: {len(calls)} {call_label}{web_note} · Total API cost ${result.get('cost_usd', 0):.5f} (includes retrieval, answer generation and web lookup when used). Database calculations and stored source reads do not call a model."),
        html.Ol([html.Li([
            html.Code(str(step.get("name") or step.get("tool") or "read-only tool")),
            html.Span(f" · {step.get('status') or step.get('result_status') or 'completed'}"),
        ]) for step in steps]),
        html.P("Tools preserve the current filters. Source relationships and historical annotations retain their review limits."),
    ], className="research-steps")


def _answer_references(indices, citations, available_indices=None):
    """Keep summary links tied to the same one-based visible quote numbering."""
    return [html.A(
        f"[{number}]", href=f"#answer-citation-{number}", className="answer-reference",
        **{"aria-label": f"Read supporting citation {number}"},
    ) for number in indices if isinstance(number, int) and not isinstance(number, bool)
            and 1 <= number <= len(citations)
            and (available_indices is None or number in available_indices)]


def _rag_scope_notes(result):
    """Keep source limits and known server disclosures outside model-written prose."""
    from observatory.date_inference import BASIS_NOTE, UNCHECKED_NOTE

    notes = [html.P(
        "This answer uses the retrieved advertisements within the current filters. Listed advertisements are retrieved candidates, not a complete list of matching ads. Missing results do not establish absence. It describes what those sources say; it does not independently verify their claims or establish full-collection totals.",
        className="scope-note",
    )]
    if result.get("media_evidence"):
        notes.append(html.P(
            "Image descriptions, OCR, captions and transcripts are derived evidence with their own locations. They are not original article quotations; image meaning, transcription and timing are not independently verified. Supplied video times identify segments, not individual words.",
            className="scope-note",
        ))
    text = str(result.get("answer") or "").rstrip()
    # Older service versions append these exact disclosures to the flat answer.
    # Preserve only known server text, not arbitrary provider/error details.
    legacy_date_note = (
        "Some dates are inferred, not taken from the source data: tier A from a date in "
        "the article URL, tier C from a web search. They are unreviewed estimates."
    )
    for warning in dict.fromkeys((BASIS_NOTE, UNCHECKED_NOTE, legacy_date_note)):
        if text.endswith(warning):
            notes.append(html.P(warning, className="scope-note"))
    data = result.get("structured_result") or {}
    if isinstance(data, dict):
        notes.extend(html.P(str(note), className="scope-note") for note in data.get("scope_notes", []))
    coverage = _evidence_coverage(result)
    if coverage is not None:
        notes.append(coverage)
    return notes


def _grounded_answer_parts(result, message):
    """Use validated structure for any content question; never resummarize old prose."""
    citations = result.get("citations") or []
    available_indices = _media_answer_citation_indices(result)
    summaries = result.get("summary") or []
    claims = result.get("cited_claims") or []
    sections = result.get("sections") or []
    if not summaries and not sections:
        return [html.P(message, className="answer-text")], []
    summary = [html.P([
        str(item.get("text") or ""), " ",
        *_answer_references(item.get("citation_indices") or [], citations, available_indices),
    ]) for item in summaries if isinstance(item, dict) and item.get("text")]
    groups = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        refs = set(section.get("citation_indices") or [])
        paragraphs = [html.P([
            str(claim.get("text") or ""), " ",
            *_answer_references(claim.get("citation_indices") or [], citations, available_indices),
        ]) for claim in claims if isinstance(claim, dict) and claim.get("text")
            and refs.intersection(claim.get("citation_indices") or [])]
        if paragraphs:
            groups.append(html.Section([
                html.H4(str(section.get("title") or "Supporting evidence")), *paragraphs,
            ], className="answer-group"))
    return summary or [html.P(message, className="answer-text")], (
        [html.Div(groups, className="answer-groups")] if groups else []
    )


def _grounded_answer_content(result, message):
    """Compatibility helper using the same neutral layout as the full answer card."""
    summary, findings = _grounded_answer_parts(result, message)
    return [
        _answer_section("Summary", summary, "answer-summary"),
        _answer_section("Findings", findings) if findings else None,
        _answer_section("Scope and limits", _rag_scope_notes(result), "answer-section answer-scope"),
    ]


def _external_research_card(result, enabled):
    """Web citations stay separate from exact quotes and collection records."""
    from observatory.service import safe_url

    research = result.get("external_research") or {}
    if not isinstance(research, dict) or not research:
        return None
    status = research.get("status")
    if status != "ok":
        notes = {
            "no_sources": "Web search did not return usable cited sources.",
            "unavailable": "Web search is temporarily unavailable. No outside evidence was added.",
            "limited": "The API budget or request limit prevented a web lookup.",
            "disabled": "Web lookup is not enabled for this deployment.",
        }
        message = ("External web research is not available for this collection or annotation scope."
                   if research.get("reason") == "scope_not_supported"
                   else notes.get(status, "No usable web evidence was added."))
        return html.P(message, className="scope-note")
    sources = {}
    for source in research.get("sources") or []:
        if not isinstance(source, dict):
            continue
        source_id = source.get("source_id")
        url = safe_url(source.get("url"))
        if isinstance(source_id, str) and re.fullmatch(r"W[1-9][0-9]*", source_id) and url:
            sources[source_id] = {**source, "url": url}
    passages = []
    for passage in research.get("passages") or []:
        if not isinstance(passage, dict) or not passage.get("text"):
            continue
        source_ids = [source_id for source_id in passage.get("source_ids") or [] if source_id in sources]
        if source_ids:
            parts = []
            linked = set()
            for part in re.split(r"(\[W[1-9][0-9]*\])", str(passage["text"])):
                source_id = part[1:-1] if part.startswith("[") and part.endswith("]") else ""
                if source_id in source_ids:
                    linked.add(source_id)
                    parts.append(html.A(part, href=sources[source_id]["url"], target="_blank",
                                        rel="noopener noreferrer", className="answer-reference")
                                 if enabled else part)
                else:
                    parts.append(part)
            for source_id in dict.fromkeys(source_ids):
                if source_id not in linked:
                    parts.extend([" ", html.A(f"[{source_id}]", href=sources[source_id]["url"],
                                             target="_blank", rel="noopener noreferrer", className="answer-reference")
                                  if enabled else f"[{source_id}]"])
            passages.append(html.P(parts))
    if not passages:
        return html.P("Web search did not return usable cited passages.", className="scope-note")
    scope = [html.P(
        "These are web-search summaries with provider citations, not stored advertisement quotes. They have not been added to collection counts or independently fact-checked. The web sources have not been verified against the current collection filters.",
        className="scope-note",
    )]
    if result.get("answer_mode") == "web_supplement":
        text = str(result.get("answer") or "")
        _, marker, reason = text.partition("\n\nWhy the collection could not answer: ")
        if marker and reason.strip():
            scope.append(html.P("Why the collection could not answer: " + reason, className="scope-note"))
    return _answer_frame(
        "Additional web sources", passages,
        [html.Details([
            html.Summary("Read web source links"),
            *[html.Article([
                html.Span(f"[{source_id}] · External web source", className="evidence-code"),
                html.H4(html.A(str(source.get("title") or source["url"]), href=source["url"],
                               target="_blank", rel="noopener noreferrer")
                        if enabled else str(source.get("title") or "Web source")),
                html.P("Not independently verified", className="scope-note"),
                html.P("Source links are disabled", className="scope-note") if not enabled else None,
            ], id=f"web-source-{source_id}", className="external-source-card")
              for source_id, source in sources.items()],
        ], open=True)],
        scope, eyebrow="Outside the advertising collection", class_name="external-research",
        trace=_research_steps(result) if result.get("answer_mode") == "web_supplement" else None,
    )


def _coverage_count(group, key):
    value = group.get(key)
    value = len(value) if isinstance(value, list) else value
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _group_missing_support(group):
    """A retrieved candidate does not substitute for support used in the answer."""
    for key in ("cited_records", "evidence_units", "passages"):
        if key in group:
            return _coverage_count(group, key) == 0
    return False


def _evidence_coverage(result):
    data = result.get("structured_result") or {}
    if not isinstance(data, dict) or data.get("kind") != "evidence_coverage":
        return None
    groups = [group for group in data.get("groups") or [] if isinstance(group, dict)]
    typed = any(any(key in group for key in (
        "image_units", "video_units", "media_status", "evidence_units", "cited_records",
    )) for group in groups)
    rows = []
    missing = []
    uncited = []
    no_candidates = []
    media_labels = {
        "ok": "Stored media retrieved", "not_configured": "Media collection not configured",
        "missing_material": "Requested media material not supplied", "no_match": "Supplied media did not match",
        "unavailable": "Media could not be verified", "disabled": "Media retrieval not enabled",
    }
    for group in groups:
        count = _coverage_count(group, "passages") if "passages" in group else 0
        if count is None:
            continue
        if _group_missing_support(group):
            missing.append(str(group.get("label") or "Search group"))
        if typed:
            images = _coverage_count(group, "image_units") or 0
            videos = _coverage_count(group, "video_units") or 0
            total = _coverage_count(group, "evidence_units")
            total = count + images + videos if total is None else total
            cited = _coverage_count(group, "cited_records")
            if total == 0:
                no_candidates.append(str(group.get("label") or "Search group"))
            elif cited == 0:
                uncited.append(str(group.get("label") or "Search group"))
            coverage = (f"{cited:,} cited record{'s' if cited != 1 else ''}" if cited is not None
                        else "Citation coverage not provided")
            media = media_labels.get(group.get("media_status"), "Media coverage not provided")
            rows.append(html.Tr([
                html.Th(str(group.get("label") or "Search group"), scope="row"),
                *[html.Td(f"{n:,}", className="count-value") for n in (count, images, videos, total)],
                html.Td([html.Span(coverage), html.Br(), html.Span(media)]),
            ]))
            continue
        rows.append(html.Tr([
            html.Th(str(group.get("label") or "Search group"), scope="row"),
            html.Td(f"{count:,}", className="count-value"),
            html.Td("No matching stored passage retrieved" if count == 0 else "Stored text retrieved"),
        ]))
    if not rows:
        return None
    return html.Details([
        html.Summary("Retrieval coverage"),
        html.P("No evidence retrieved for: " + ", ".join(no_candidates) + ".", className="coverage-warning") if typed and no_candidates else None,
        html.P("No source cited for: " + ", ".join(uncited) + ".", className="coverage-warning") if typed and uncited else None,
        html.P("No matching stored passages retrieved for: " + ", ".join(missing) + ".",
               className="coverage-warning") if not typed and missing else None,
        html.Div(html.Table([
            html.Thead(html.Tr([html.Th(label, scope="col") for label in (
                ("Question group", "Body passages", "Image units", "Video units", "Total evidence units", "Citations and media status")
                if typed else ("Question group", "Retrieved passages", "Coverage")
            )])),
            html.Tbody(rows),
        ]), className="statistics-table"),
        html.P("These are bounded retrieved evidence units, not advertisement counts or complete coverage. Missing material, an unconfigured collection or no text match does not prove absence from the advertisement. Retrieved candidates and cited support are reported separately."
               if typed else "These passages are a sample of stored text within the current filters. Zero retrieved passages does not prove that a company has no advertisements or no such claims.",
               className="scope-note"),
    ], open=bool(missing), className="answer-coverage research-steps")


def _partial_collection_answer(result):
    """A supported partial comparison is useful, but is not a complete answer."""
    data = result.get("structured_result") or {}
    return bool(result.get("status") == "insufficient_evidence" and result.get("summary")
                and isinstance(data, dict) and data.get("kind") == "evidence_coverage"
                and any(isinstance(group, dict) and _group_missing_support(group)
                        for group in data.get("groups") or []))


def _tools_card(result, links_enabled):
    """Render typed, bounded tool results beside their record references."""
    from observatory.network_ui import record_cards
    from observatory.service import safe_url

    data = result["structured_result"]
    kind = data.get("kind")
    sections, scope = [], [html.P(note, className="scope-note") for note in data.get("scope_notes", [])]
    summary, title = [], "Source records"
    if kind == "original_metadata":
        title = "Original source fields"
        summary = [html.P(result.get("answer") or "Stored original fields are shown below.", style={"whiteSpace": "pre-line"})]
        fields = data.get("original_fields") or {}
        review_fields = set(data.get("review_fields") or [])
        if "complete" in data:
            known = len(data.get("known_fields") or [])
            summary.insert(0, html.P(
                f"{known} of {len(fields)} requested fields available. "
                + ("All requested fields are available." if data["complete"]
                   else "Partial answer; missing or review fields remain unresolved."),
                className="scope-note"))

        def field_cells(name, item):
            review = (name in review_fields or item.get("status") == "needs_review"
                      or (item.get("provenance") or {}).get("status") == "needs_review")
            value = (str(item["value"]) if item.get("value") is not None
                     and item.get("status") == "recorded" and not review
                     else "Awaiting source review" if review else "Unknown")
            return html.Tr([html.Td(name.replace("_", " ").capitalize()), html.Td(value),
                            html.Td("Awaiting source review" if review else item.get("status") or "Unknown")])

        sections.append(html.Table([
            html.Thead(html.Tr([html.Th(label) for label in ("Field", "Stored original value", "Source status")])),
            html.Tbody([field_cells(name, item) for name, item in fields.items()]),
        ]))
        record = data.get("record") or {}
        if record.get("record_id"):
            sections.append(dcc.Link("Open source record →", href="/records/" + quote(record["record_id"], safe="")))
        scope.append(html.P("These are stored source fields. Missing or conflicting values remain unknown; outside pages do not replace them.", className="scope-note"))
    elif kind == "content_matches":
        from observatory.content_ui import (
            MEANING,
            content_record_cards,
            content_summary,
        )

        title = "Reviewed content matches"
        try:
            summary = [html.P(content_summary(data))]
            sections = content_record_cards(data, links_enabled)
            scope += [html.P(MEANING, className="scope-note"),
                      html.P("This page and export contain the shown records only. Whole-selection coverage is reported separately.", className="scope-note")]
            sections.append(dcc.Link("Browse reviewed content questions in Data →", href="/data"))
        except (KeyError, TypeError, ValueError):
            return _answer_status(title, "The reviewed content results cannot be verified.",
                                  "Refresh the question or browse the native collection.", trace=_research_steps(result))
    elif kind == "claims":
        from observatory.claims_ui import claims_record_cards, public_claims

        title = "Published CLAIMS2 assignments"
        try:
            claims = public_claims(data)
            if claims["state"] == "pending":
                return _answer_status(title, claims["note"], "Browse available records in Data or retry after results are published.", trace=_research_steps(result))
            summary = [html.P(
                f"{claims['total_records']:,} stored records have {claims['total_matches']:,} published CLAIMS2 assignments within this selection."
            )]
            scope.append(html.P(claims["note"], className="scope-note"))
            sections.extend(claims_record_cards(claims["records"], links_enabled))
            if not claims["records"]:
                sections.append(html.P("No published matching assignments are available in this selection."))
        except (TypeError, ValueError):
            return _answer_status(title, "These CLAIMS2 results are unavailable.", "Submit the question again or browse the published evidence in Data.", trace=_research_steps(result))
        sections.append(dcc.Link("Browse published evidence in Data →", href="/data"))
    elif kind == "graph":
        graph = data.get("graph") or {}
        nodes = {node["id"]: node for node in graph.get("nodes", [])}
        edges = graph.get("edges", [])
        title = "Source relationships"
        summary = [html.P(
            f"{len(edges):,} recorded relationship{'s' if len(edges) != 1 else ''} across {len(nodes):,} entities are shown on this page. Each relationship links to its originating record."
        )]
        scope.append(html.P("This is a page of recorded relationships. It is not the complete graph and does not establish corporate contracts or verified claims.", className="scope-note"))
        sections.extend([
            html.Div(html.Table([
                html.Caption("Typed relationships and their originating record"),
                html.Thead(html.Tr([html.Th(label, scope="col") for label in ("From", "Relationship", "To", "Source record")])),
                html.Tbody([html.Tr([
                    html.Td(nodes.get(edge["source"], {}).get("label", "Source")),
                    html.Td(edge.get("label") or edge["predicate"]),
                    html.Td(nodes.get(edge["target"], {}).get("label", "Target")),
                    html.Td(dcc.Link("Open record", href="/records/" + quote(str(edge.get("provenance", {}).get("record_id", "")), safe=""))),
                ]) for edge in edges]),
            ]), className="statistics-table"),
            dcc.Link("Explore the article knowledge graph →", href="/data"),
        ])
    elif kind in {"record", "sources"}:
        record = data.get("record") or {}
        title = "Article text" if kind == "record" else "Sources for this record"
        if record.get("record_id"):
            sections.extend(record_cards([record], links_enabled))
        if kind == "record":
            body = data.get("body") or {}
            summary = [html.P(
                f"Characters {body.get('start', 0):,}–{body.get('end', 0):,} of {body.get('total_characters', 0):,} are shown from this stored record."
                if body.get("text") else "No stored article text is available for this record."
            )]
            sections.append(html.Pre(body.get("text") or "No stored article text is available.", className="tool-article-text"))
            scope.append(html.P("This is a stored text interval; completeness of the original article has not been established.", className="scope-note"))
        else:
            artifacts = data.get("source_artifacts") or []
            summary = [html.P(f"{len(artifacts):,} stored source reference{'s are' if len(artifacts) != 1 else ' is'} available for this record.")]
            sources = []
            for artifact in artifacts:
                properties = artifact.get("properties") or {}
                url = safe_url(properties.get("url")) if links_enabled else ""
                sources.append(html.Li([
                    html.A(artifact.get("label", "Source reference"), href=url, target="_blank", rel="noopener noreferrer") if url else html.Span(artifact.get("label", "Source reference")),
                    html.Span(" · URL recorded; contents not independently verified"),
                ]))
            sections.append(html.Ul(sources) if sources else html.P("No public source references are available for this record."))
            scope.append(html.P("Historical annotations are unverified. The reviewed attachment adapter is not connected to this tool yet.", className="scope-note"))
    else:
        return _answer_status("Data result unavailable", "The returned tool result cannot be displayed.", "Submit the question again or browse the records in Data.", trace=_research_steps(result))
    return _answer_frame(title, summary, sections, scope,
                         eyebrow="Read-only data tools", trace=_research_steps(result))


def _render_answer_result(result, links_enabled, service):
    """All answer routes share presentation; failure statuses cannot bypass it."""
    status, mode = result.get("status"), result.get("answer_mode", "rag")
    data = result.get("structured_result") or {}
    if mode == "tools" and data.get("kind") == "composite":
        cards = [html.H3("Completed question parts" if data.get("complete") else "Partial answer")]
        if status in {"limited", "service_unavailable"}:
            title = "Paid answers temporarily limited" if status == "limited" else "Answer service unavailable"
            cards.append(_answer_status(
                title, "The remaining collection tasks could not be executed.",
                "Try again later. Completed collection parts are shown below; no web answer was substituted.",
            ))
        for part in data.get("parts", []):
            cards.append(html.H4(part["question_part"]))
            cards.extend(_render_answer_result({**part["answer"], "compound_part": True}, links_enabled, service))
        if data.get("pending_parts"):
            cards.append(html.H4("Still unresolved"))
            cards.append(html.Ul([html.Li(part["question_part"]) for part in data["pending_parts"]]))
        if data.get("failure_message"):
            cards.append(html.P("Why the remaining collection tasks could not be answered: " + data["failure_message"],
                                className="scope-note"))
        cards.append(_research_steps(result))
        return cards
    if mode == "tools" and data.get("kind") == "original_metadata":
        return [_tools_card(result, links_enabled)]
    if status == "answered" and result.get("structured_result"):
        if mode == "statistics":
            return [_statistics_card(result, links_enabled, service, include_record_browser=not result.get("compound_part"))]
        if mode == "tools":
            return [_tools_card(result, links_enabled)]
    external = _external_research_card(result, links_enabled)
    if status == "answered" and mode == "web_supplement":
        cards = []
        collection = (result.get("external_research") or {}).get("collection_answer") or {}
        local = {**result, "status": collection.get("status", "insufficient_evidence"),
                 "answer_mode": collection.get("answer_mode", "tools" if data.get("kind") in {"original_metadata", "composite"} else "rag"),
                 "answer": collection.get("answer", ""), "external_research": {}}
        has_local_data = data.get("kind") in {"original_metadata", "composite"}
        has_cited_partial = (_partial_collection_answer(local) and local.get("citations")
                             and (local.get("evidence") or local.get("media_evidence")))
        if has_local_data or has_cited_partial:
            cards.extend(item for item in _render_answer_result(local, links_enabled, service) if item is not None)
        cards.append(external if isinstance(external, html.Div) else _answer_status(
            "Web answer unavailable", "No usable cited web answer is available.", "Retry the question or browse the collection.",
        ))
        return cards
    partial = _partial_collection_answer(result)
    if mode == "rag" and (status == "answered" or partial):
        summary, findings = _grounded_answer_parts(result, str(result.get("answer") or ""))
        evidence = [_answer_section("Findings", findings)] if findings else []
        if result.get("evidence"):
            evidence.extend([
                html.H5("Records and quoted evidence", className="answer-evidence-title"),
                *_evidence_cards(result["evidence"], links_enabled, result.get("citations") or []),
            ])
        media_cards = _media_evidence_cards(result.get("media_evidence") or [], links_enabled,
                                             result.get("citations") or [])
        if media_cards:
            evidence.extend([
                html.H5("Image and video evidence", className="answer-evidence-title"), *media_cards,
            ])
        return [_answer_frame(
            "Partial collection evidence" if partial else "Answer with supporting evidence",
            summary, evidence, _rag_scope_notes(result),
            eyebrow="Generated answer", trace=_research_steps(result), class_name="grounded-answer",
        ), external]
    if status == "insufficient_evidence":
        title = "Clarify this question" if mode == "clarification" else "Insufficient evidence"
        message = str(result.get("answer") or "The available records do not support an answer.")
        next_step = ("Specify the collection, company, outlet or date range so the question can be answered within a clear scope."
                     if mode == "clarification" else
                     "Try a more specific question, change the filters or read the retrieved records. Missing evidence does not establish absence in the collection.")
    elif status == "limited":
        title, message = "Paid answers temporarily limited", "The model request limit or project API budget was reached."
        next_step = "Try again later. You can continue browsing the collection and using keyword search."
    elif result.get("failure_reason") == "media_evidence_mismatch":
        title = "Media evidence could not be verified"
        message = "Image or video evidence could not be checked against its current source. The answer was withheld."
        next_step = "Submit the question again. Stored records and keyword search remain available."
    else:
        title, message = "Answer service unavailable", "The answer service could not complete this request."
        next_step = "Submit the question again. You can continue browsing the collection and using keyword search."
    extras = []
    if result.get("evidence"):
        extras.append(_answer_section("Retrieved passages", _evidence_cards(
            result["evidence"], links_enabled,
        )))
    media_cards = _media_evidence_cards(result.get("media_evidence") or [], links_enabled)
    if media_cards:
        extras.append(_answer_section("Retrieved media evidence", media_cards))
        extras.append(_answer_section("Scope and limits", _rag_scope_notes(result),
                                      "answer-section answer-scope"))
    coverage = _evidence_coverage(result)
    if coverage is not None and not media_cards:
        extras.append(coverage)
    label = "model-assisted query" if result.get("research_trace") else "no model charge"
    eyebrow = {
        "clarification": f"Question needs clarification · {label}",
        "statistics": f"Collection statistics · {label}",
        "tools": "Read-only data tools",
    }.get(mode)
    return [_answer_status(title, message, next_step, eyebrow=eyebrow,
                           trace=_research_steps(result), extra=extras), external]


def _keyword_result(report, links_enabled):
    evidence = report["evidence"]
    diagnostics = _coverage_notice(report.get("diagnostics", {}))
    if not evidence:
        return _answer_status(
            "No matching evidence", "No matching stored passages were retrieved within the current filters. This does not establish that no such advertisements exist.",
            "Try a different term or widen the search scope.", extra=[diagnostics],
        )
    return _answer_frame(
        "Keyword search", [html.P(f"{len(evidence)} evidence passages found. No paid model call was made.")],
        [diagnostics, *_evidence_cards(evidence, links_enabled)],
        [html.P("This is a limited keyword retrieval within the current filters, not the number of advertisements in the collection or a generated answer.", className="scope-note")],
        eyebrow="Keyword search · no model charge",
    )


def _summary(stats, dataset=None):
    values = [
        (
            "Unique posts" if dataset == "social" else "Selected records",
            stats.get("total", 0),
            "One per platform and original post URL; source rows are preserved" if dataset == "social" else "Eligible records in this selection",
        ),
        (
            "Searchable records",
            stats.get("retrievable", 0),
            "With text available for retrieval",
        ),
        (
            "Unknown dates",
            stats.get("unknown_dates", 0),
            "Publication date not available",
        ),
    ]
    return [
        html.Div(
            [
                html.Span(label, className="stat-label"),
                html.Strong(f"{int(value):,}", className="stat-value"),
                html.Span(note, className="stat-note"),
            ],
            className="stat-card",
        )
        for label, value, note in values
    ]


def _source_links(item, enabled):
    if not enabled:
        return html.Span("Source links are disabled", className="source-muted")
    links = [
        html.A(
            label,
            href=url,
            target="_blank",
            rel="noopener noreferrer",
            className="source-link",
        )
        for label, value in [
            ("Original source ↗", item.get("url")),
            ("Archived source ↗", item.get("archive_url")),
        ]
        if (url := _url(value))
    ]
    return html.Div(
        links
        or [html.Span("No public source link available", className="source-muted")],
        className="source-links",
    )


def _coverage_notice(diagnostics):
    if not diagnostics:
        return None
    if diagnostics.get("operator") == "literal_phrase":
        return _notice(
            "Saved text phrase match",
            diagnostics.get("reason", "Matched the literal phrase in saved social text. Retrieval rank is not a confidence score."),
        )
    if diagnostics.get("status") == "unavailable":
        return _notice(
            "Keyword coverage unavailable",
            diagnostics.get("reason", "Coverage could not be measured."),
        )
    missing_scope = diagnostics.get("missing_from_scope", [])
    missing_results = [
        term
        for term in diagnostics.get("missing_from_results", [])
        if term not in missing_scope
    ]
    return html.Div(
        [
            html.Strong("Search term coverage"),
            html.P(
                "Keyword search uses OR matching and English word stems. A passage can match only part of your question. Retrieval rank is not a confidence score."
            ),
            html.P(
                "No indexed-body matches in this selection: "
                + ", ".join(missing_scope)
                + ". This does not establish absence from uncaptured or excluded article text.",
                className="coverage-warning",
            )
            if missing_scope
            else None,
            html.P(
                "Found elsewhere in this selection, but not in these returned passages: "
                + ", ".join(missing_results)
            )
            if missing_results
            else None,
            html.Ul(
                [
                    html.Li(
                        f"{term['term']}: {term['matching_records']} searchable records"
                    )
                    for term in diagnostics.get("term_details", [])
                ]
            ),
            html.P(diagnostics.get("reason", ""))
            if diagnostics.get("status") == "partial"
            else None,
        ],
        className="notice notice-info",
    )


def _evidence_cards(evidence, enabled, citations=()):
    citation_quotes = {}
    for number, citation in enumerate(citations, start=1):
        citation = _mapping(citation)
        evidence_id = citation.get("evidence_id")
        quote = citation.get("quote")
        if (isinstance(evidence_id, str) and evidence_id and isinstance(quote, str)
                and citation.get("evidence_type", "article_text") == "article_text"
                and citation.get("origin", "original_text") == "original_text"):
            citation_quotes.setdefault(evidence_id, []).append((number, quote))
    cards = []
    for rank, value in enumerate(evidence, start=1):
        item = _mapping(value)
        passage = str(item.get("text") or "")
        supported = [
            (number, quote)
            for number, quote in citation_quotes.get(item.get("evidence_id"), [])
            if quote.strip() and quote in passage
        ]
        if supported:
            quote_blocks = [
                html.Div(
                    [
                        html.Span(f"Citation [{number}]", className="evidence-code"),
                        html.Blockquote(quote),
                    ],
                    className="citation-quote",
                    id=f"answer-citation-{number}",
                )
                for number, quote in supported
            ]
        else:
            excerpt = passage[:520] + ("…" if len(passage) > 520 else "")
            quote_blocks = [html.Blockquote(excerpt)]
        cards.append(
            html.Article(
                [
                    html.Div(
                        [
                            html.Span(
                                "Native advertising"
                                if item.get("dataset") == "native"
                                else "Company social posts",
                                className="dataset-chip",
                            ),
                            html.Span(
                                f"Retrieval rank {rank}",
                                className="evidence-code",
                            ),
                        ],
                        className="evidence-heading",
                    ),
                    html.H4(str(item.get("title") or "Untitled record")),
                    html.P(
                        " · ".join(
                            [
                                str(item.get("publisher") or UNKNOWN),
                                sponsor_display(item.get("sponsor")),
                                str(
                                    item.get("published_at")
                                    or "Publication date unknown"
                                ),
                            ]
                        ),
                        className="muted",
                    ),
                    html.P(
                        "Matched search terms: "
                        + ", ".join(item.get("matched_terms") or [])
                        if item.get("matched_terms")
                        else "Ordered by retrieval relevance; rank is not a confidence score.",
                        className="match-note",
                    ),
                    *([html.P(str(item["date_notice"]), className="scope-note")]
                      if item.get("date_notice") else []),
                    *quote_blocks,
                    html.Details(
                        [
                            html.Summary("Read retrieved passage"),
                            html.P(passage, className="passage"),
                        ]
                    ),
                    html.Details(
                        [
                            html.Summary("Technical details"),
                            html.Div(
                                [
                                    html.Span(
                                        f"Evidence {item.get('evidence_id', '')}"
                                    ),
                                    html.Span(f"Record {item.get('record_id', '')}"),
                                    html.Span(f"Version {item.get('version_id', '')}"),
                                    html.Span(
                                        f"Character range {item.get('start', '')}–{item.get('end', '')}"
                                    ),
                                    html.Span(
                                        f"Retrieval score {item.get('score', 0):.5f}; not a probability"
                                    ),
                                ],
                                className="record-reference",
                            ),
                        ]
                    ),
                    html.A(
                        "View record and archived materials →",
                        href="/records/" + str(item.get("record_id") or ""),
                        target="_blank",
                        rel="noopener",
                    ),
                    _source_links(item, enabled),
                ],
                className="evidence-card",
            )
        )
    return decorate_source_observation_cards(cards, evidence, citations)


_MEDIA_ORIGINS = {
    "image": {"ocr": "OCR text", "vision": "Model image description", "human_description": "Human image description"},
    "video": {"publisher_caption": "Publisher captions", "automatic_caption": "Automatic captions",
              "transcript": "Speech transcription", "human_description": "Human video description"},
}


def _media_citation_matches(citation, item):
    return (citation.get("evidence_id") == item.get("evidence_id")
            and citation.get("evidence_type") == item.get("media_type")
            and citation.get("origin") == item.get("origin")
            and isinstance(citation.get("quote"), str) and citation["quote"].strip()
            and citation["quote"] in str(item.get("evidence_text") or ""))


def _media_answer_citation_indices(result):
    """Mixed answers keep common numbering without links to rejected media quotes."""
    if not result.get("media_evidence"):
        return None
    available = set()
    body = [_mapping(item) for item in result.get("evidence") or []]
    media = [_mapping(item) for item in result.get("media_evidence") or []]
    for number, value in enumerate(result.get("citations") or [], start=1):
        citation = _mapping(value)
        if any(_media_citation_matches(citation, item) and item.get("quote_from_original_body") is False
               and item.get("origin") in _MEDIA_ORIGINS.get(item.get("media_type"), {}) for item in media):
            available.add(number)
        elif (citation.get("evidence_type", "article_text") == "article_text"
              and citation.get("origin", "original_text") == "original_text"
              and isinstance(citation.get("quote"), str) and citation["quote"].strip()
              and any(original_text_citation_matches(citation, item) for item in body)):
            available.add(number)
    return available


def _media_location(item):
    """Display supplied locations, never infer a region or per-word timing."""
    location = item.get("locator") or {}
    if not isinstance(location, dict):
        location = _mapping(location)
    if item.get("media_type") == "image":
        page = location.get("page_number")
        labels = [f"Page {page}" if isinstance(page, int) and not isinstance(page, bool) and page > 0 else "Page not provided"]
        region = location.get("region")
        valid = (location.get("kind") == "image_region" and isinstance(region, list) and len(region) == 4
                 and all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in region)
                 and 0 <= region[0] < region[2] <= 1 and 0 <= region[1] < region[3] <= 1)
        labels.append("Image region (normalized): " + ", ".join(f"{n:.2f}" for n in region)
                      if valid else "Image region not provided")
        frame = location.get("video_time_ms")
        if location.get("video_asset_id") and isinstance(frame, int) and not isinstance(frame, bool) and frame >= 0:
            labels.append(f"Supplied video frame: {_media_time(frame)}")
        return " · ".join(labels)
    start, end = location.get("start_ms"), location.get("end_ms")
    if (location.get("kind") == "video_time" and all(isinstance(n, int) and not isinstance(n, bool) for n in (start, end))
            and 0 <= start < end):
        return f"Supplied video segment: {_media_time(start)}–{_media_time(end)}. Segment timing is not per-word alignment."
    return "Video time not provided; no word or segment alignment is inferred."


def _media_time(milliseconds):
    minutes, seconds = divmod(milliseconds / 1000, 60)
    return f"{int(minutes):02d}:{seconds:06.3f}"


def _media_evidence_cards(evidence, enabled, citations=()):
    """Derived media text has its own cards and must never enter body quote cards."""
    cards = []
    for value in evidence:
        item = _mapping(value)
        medium, origin = item.get("media_type"), item.get("origin")
        label = _MEDIA_ORIGINS.get(medium, {}).get(origin)
        if not label or item.get("quote_from_original_body") is not False or not item.get("evidence_text"):
            continue
        description = origin in {"vision", "human_description"}
        kind = "Derived description" if description else "Derived text quotation"
        matched = [(number, _mapping(citation)["quote"]) for number, citation in enumerate(citations, start=1)
                   if _media_citation_matches(_mapping(citation), item)]
        blocks = [html.Div([
            html.Span(f"Citation [{number}] · {kind}", className="evidence-code"),
            html.P(text, className="passage") if description else html.Blockquote(text),
        ], id=f"answer-citation-{number}", className="citation-quote") for number, text in matched]
        text = str(item["evidence_text"])
        if not blocks:
            blocks = [html.P(text[:520] + ("…" if len(text) > 520 else ""), className="passage")]
        url = _url(item.get("source_url")) if enabled else ""
        if url and (urlsplit(url).username or urlsplit(url).password):
            url = ""
        details = [html.Span(f"{name.replace('_', ' ').title()}: {item[name]}") for name in (
            "evidence_id", "asset_id", "record_id", "version_id", "asset_sha256", "text_artifact_id",
            "artifact_sha256", "derived_start", "derived_end",
        ) if name in item]
        cards.append(html.Article([
            html.Div([html.Span(("Image evidence" if medium == "image" else "Video evidence") + " · "
                               + ("Native advertising" if item.get("dataset") == "native" else "Company social posts"), className="dataset-chip"),
                      html.Span(label, className="evidence-code")], className="evidence-heading"),
            html.H4(str(item.get("title") or "Untitled record")),
            html.P(f"{kind}; not an original article quotation.", className="scope-note"),
            html.P(_media_location(item), className="match-note"),
            *blocks,
            html.Details([html.Summary("Read derived evidence"), html.P(text, className="passage")]),
            html.P(str(item.get("quality_label") or "Derived evidence; wording and visual meaning are not independently verified."), className="scope-note"),
            html.Details([html.Summary("Technical details"), html.Div(details, className="record-reference")]),
            html.A("View record and archived materials →", href="/records/" + quote(str(item.get("record_id") or ""), safe=""), target="_blank", rel="noopener noreferrer"),
            html.Div([html.A("Original source ↗", href=url, target="_blank", rel="noopener noreferrer")
                      if url else html.Span("No public source link available" if enabled else "Source links are disabled", className="source-muted")], className="source-links"),
        ], className="evidence-card media-evidence-card"))
    return cards


def _filter_inputs(dataset, dependency=Input):
    return [dependency(f"{dataset}-{name}", "value") for name in FILTER_NAMES] + [
        dependency(f"{dataset}-dates", "start_date"),
        dependency(f"{dataset}-dates", "end_date"),
        dependency(f"{dataset}-unknown-dates", "value"),
    ]


def _filter_scope_name(name, dataset):
    if name == "sponsors" and dataset == "all":
        return "Sponsor / company affiliation"
    if name == "sponsors" and dataset == "social":
        return "Company affiliation"
    if name == "labels" and dataset == "social":
        return "Historical source states (OR)"
    return name.replace("_", " ").title()


def _filter_panel(dataset, facets):
    native = dataset == "native"
    names = {
        "publishers": "News outlet",
        "sponsors": "Sponsor / advertiser" if native else "Company affiliation",
        "platforms": "Platform",
        "keywords": "Collection search term",
        "labels": "Historical automated label" if native else "Historical source state (OR)",
        "accounts": "Account",
    }
    controls = {}
    for field in FILTER_NAMES:
        hidden = (native and field in {"platforms", "accounts"}) or (
            not native and field == "publishers"
        )
        controls[field] = html.Div(
                [
                    html.Label(names[field], htmlFor=f"{dataset}-{field}"),
                    dcc.Dropdown(
                        id=f"{dataset}-{field}",
                        options=social_state_options() if not native and field == "labels" else [
                            {
                                "label": sponsor_display(value)
                                if field == "sponsors"
                                else value,
                                "value": value,
                            }
                            for value in facets.get(field, [])
                        ],
                        value=[],
                        multi=True,
                        placeholder="All",
                        className="filter-dropdown",
                        persistence=True,
                        persistence_type="session",
                    ),
                ],
                className="filter-field",
                style={"display": "none"} if hidden else {},
        )
    date_control = html.Div(
            [
                html.Label("Publication date"),
                dcc.DatePickerRange(
                    id=f"{dataset}-dates",
                    clearable=True,
                    minimum_nights=0,
                    number_of_months_shown=1,
                    display_format="MMM D, YYYY",
                    start_date_placeholder_text="Start date",
                    end_date_placeholder_text="End date",
                    persistence=True,
                    persistence_type="session",
                ),
            ],
            className="filter-field date-filter",
        )
    date_control.children.append(dcc.Checklist(
            id=f"{dataset}-unknown-dates",
            options=[{"label": " Include unknown dates", "value": "include"}],
            value=["include"],
            className="unknown-toggle",
            persistence=True,
            persistence_type="session",
        ))
    about_filters = html.Details(
            [
                html.Summary("About these filters"),
                html.P(
                    "Historical labels come from earlier automated classification runs. Collection search terms describe how records were collected; they are not sponsor identities or article themes."
                    if native else
                    "Accounts group the source channel.name values, not unique channel IDs. Use Platform with Account to narrow names shared across platforms; names can also be shared within a platform. Company affiliation comes from the source and does not verify paid sponsorship. Unknown values remain selectable. Historical source states use OR: a post matches any selected state. Source True/False are unreviewed automated outputs, not verified themes or greenwashing judgments. Unknown annotation does not mean Source False."
                ),
            ],
            className="filter-note",
        )
    return html.Aside(
        [
            html.Div([controls["sponsors"], controls["publishers"] if native else controls["platforms"],
                      controls["accounts"], date_control], className="common-filters"),
            html.P("Account groups source channel.name values; company affiliation does not establish paid sponsorship.",
                   className="scope-note") if not native else None,
            html.Details([
                html.Summary("More filters"),
                html.Div([controls["keywords"], controls["labels"], controls["platforms"] if native else controls["publishers"]], className="advanced-filter-fields"),
                about_filters,
            ], className="advanced-filters"),
            html.Button("Clear filters", id=f"{dataset}-clear-filters", n_clicks=0, className="button button-quiet clear-filters"),
        ],
        id=f"{dataset}-filter-panel",
        className="filters-panel",
        style={} if native else {"display": "none"},
        **{
            "aria-label": "Native advertising filters"
            if native
            else "Company social post filters"
        },
    )


def _panel(dataset, links_enabled):
    from observatory.data_layout import data_panel
    return data_panel(dataset, links_enabled, _figure)


def create_app(service, settings, record_details=None) -> Dash:
    """Build the UI against the small public Service contract."""
    if record_details is None and hasattr(service, "db"):
        from observatory.records import RecordDetails

        record_details = RecordDetails(service.db, settings)
    enabled = bool(settings.show_source_links)
    agent_enabled = bool(getattr(settings, "research_agent_enabled", False)
                         or getattr(settings, "question_intent_enabled", False))
    app = Dash(
        __name__,
        assets_folder=str(Path(__file__).parent / "assets"),
        title="Advertising Observatory",
        update_title="Loading…",
        suppress_callback_exceptions=False,
    )
    app.server.secret_key = getattr(
        settings, "cookie_secret", None
    ) or secrets.token_hex(32)
    app.server.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(getattr(settings, "secure_cookies", False)),
    )
    from observatory.query_progress import ProgressRegistry

    progress_registry = ProgressRegistry()

    @app.server.before_request
    def query_visitor():
        # Establish the owner cookie before a long answer callback starts, so
        # concurrent polling uses the same session from its first request.
        if request.path in {"/_dash-layout", "/_dash-update-component"} and "visitor_id" not in session:
            session["visitor_id"] = secrets.token_urlsafe(24)

    from observatory.record_view import register_record_page

    register_record_page(app.server, record_details, service=service)
    from observatory.knowledge_routes import register_knowledge_routes

    register_knowledge_routes(app.server, service, record_details)
    if record_details is not None:
        from observatory.records import register_record_routes

        register_record_routes(app.server, record_details)

    @app.server.get("/healthz")
    def health_endpoint():
        state = service.health()
        try:
            app_version = version("ciss-observatory")
        except PackageNotFoundError:
            app_version = "uninstalled"
        commit = os.getenv("RAILWAY_GIT_COMMIT_SHA", "")
        release = {
            "version": app_version,
            "commit": commit if re.fullmatch(r"[0-9a-fA-F]{40}", commit) else None,
            "features": {
                "collection_graph": True,
                "graph_breakdowns": True,
                "research_agent": agent_enabled,
                "claims_read_view": True,
            },
        }
        public_state = {
            key: state[key]
            for key in (
                "status", "record_counts", "chunks", "active_profile",
                "source_data_version", "index_version", "data_version",
            )
            if key in state
        }
        return {**public_state, "application": release}, (200 if state.get("status") == "ok" else 503)

    def layout():
        try:
            health = service.health()
        except Exception:  # noqa: BLE001 - hide internal service errors at the public boundary.
            health = {"status": "unavailable"}
        facets = {}
        for dataset in ("native", "social"):
            try:
                facets[dataset] = service.facets(dataset)
            except Exception:  # noqa: BLE001 - leave controls usable during service failures.
                facets[dataset] = {}

        from .combined_ui import combined_filters, combined_panel

        combined_facets = {
            name: sorted(set(facets["native"].get(name, []) + facets["social"].get(name, [])))
            for name in FILTER_NAMES
        }

        collection_toolbar = html.Div(
            [
                html.Div(
                    [
                        html.Label(
                            "Collection",
                            htmlFor="active-dataset",
                            className="collection-label",
                        ),
                        dcc.Dropdown(
                            id="active-dataset",
                            options=[
                                {"label": "All records", "value": "all"},
                                {"label": "Native advertising", "value": "native"},
                                {"label": "Company social posts", "value": "social"},
                            ],
                            value="all",
                            clearable=False,
                            searchable=False,
                            persistence="combined-collections-v1",
                            persistence_type="session",
                            className="collection-picker",
                        ),
                    ],
                    className="collection-selector",
                ),
                html.Div(
                    [
                        html.Label(
                            "Research question",
                            htmlFor="research-question",
                            className="visually-hidden",
                        ),
                        dcc.Textarea(
                            id="research-question",
                            value="",
                            placeholder="Ask a question about these records…",
                            maxLength=2000,
                            className="question-input",
                            persistence=True,
                            persistence_type="session",
                        ),
                        html.Button(
                            "Generate answer",
                            id="answer-paid",
                            n_clicks=0,
                            className="button button-primary",
                            title="Understand your question, use read-only data tools, and return a source-linked answer" if agent_enabled else
                                  "Get database counts or a source-linked answer; semantic answers use the project's API budget",
                            **{"aria-describedby": "query-cost"},
                        ),
                    ],
                    id="query-composer",
                    className="query-composer",
                ),
            ],
            className="collection-toolbar",
        )
        query_options = html.Div(
            [
                html.H3("Search scope"),
                dcc.RadioItems(
                    id="search-scope",
                    options=[
                        {
                            "label": "Current collection and its filters",
                            "value": "current",
                        },
                        {
                            "label": "Both collections · all available records",
                            "value": "all",
                        },
                    ],
                    value="current",
                    className="search-scope",
                    persistence=True,
                    persistence_type="session",
                ),
                html.Button(
                    "Search keywords",
                    id="search-free",
                    n_clicks=0,
                    className="button button-secondary",
                ),
            ],
            id="query-options",
            className="query-options",
        )
        toolbox = html.Details(
            [
                html.Summary(
                    html.Img(src="/assets/tools.svg", width=22, height=22, alt=""),
                    className="toolbox-trigger",
                    title="Tools",
                    **{"aria-label": "Tools"},
                ),
                html.Div(
                    [
                        html.Div(
                            [
                                html.H2("Tools"),
                                html.Button(
                                    "×",
                                    type="button",
                                    className="toolbox-close",
                                    **{"aria-label": "Close tools"},
                                ),
                            ],
                            className="toolbox-heading",
                        ),
                        query_options,
                        html.A("Collection filters", href="#collection-filters", className="toolbox-link", id="open-collection-filters"),
                        html.P(
                            "Question understanding uses the project's API budget, including counting questions. Database calculations, stored source reads and keyword search are free. When enabled, web lookup also uses the API budget and is shown separately from collection evidence. Questions can contain up to 2,000 characters." if agent_enabled else
                            "Record counts and sponsor / outlet lists are free database queries. Evidence summaries use the project's API budget. Keyword search is free. Questions can contain up to 2,000 characters.",
                            id="query-cost",
                            className="search-help",
                        ),
                        dcc.Link(
                            "Project wireframe",
                            href="/wireframe",
                            id="nav-wireframe",
                            className="toolbox-link",
                        ),
                        dcc.Link(
                            "Evaluation",
                            href="/evaluation",
                            id="nav-evaluation",
                            className="toolbox-link",
                        ),
                    ],
                    className="toolbox-panel",
                    **{"aria-label": "Research tools"},
                ),
            ],
            id="toolbox",
            className="toolbox",
        )
        return html.Div(
            [
                dcc.Location(id="page-location", refresh=False),
                dcc.Store(id="research-submission"),
                dcc.Store(id="research-progress-token", data=secrets.token_urlsafe(24)),
                dcc.Interval(id="research-progress-poll", interval=600, disabled=True),
                html.A("Skip to content", href="#main", className="skip-link"),
                html.Header(
                    [
                        dcc.Link(
                            html.Span(
                                "Advertising Observatory", className="brand-name"
                            ),
                            href="/query",
                            className="brand",
                        ),
                        html.Div(
                            [
                                html.Nav(
                                    [
                                        dcc.Link(
                                            "Query",
                                            href="/query",
                                            id="nav-query",
                                            className="nav-link is-active",
                                        ),
                                        dcc.Link(
                                            "Data",
                                            href="/data",
                                            id="nav-data",
                                            className="nav-link",
                                        ),
                                    ],
                                    className="page-nav",
                                    **{"aria-label": "Main navigation"},
                                ),
                                toolbox,
                            ],
                            className="header-actions",
                        ),
                    ],
                    className="site-header",
                ),
                html.Main(
                    [
                        html.Section(
                            [
                                html.H1("Query the evidence", id="page-title"),
                                html.P(
                                    "",
                                    id="page-description",
                                    className="hero-description",
                                ),
                            ],
                            className="hero",
                        ),
                        _notice(
                            "Data service unavailable",
                            "The collection could not be loaded. Please try again when the data service is available.",
                            "warning",
                        )
                        if health.get("status") == "unavailable"
                        else None,
                        html.Div(
                            [
                                collection_toolbar,
                                html.Details([
                                    html.Summary("Collection filters"),
                                    html.Div([
                                        _filter_panel("native", facets["native"]),
                                        _filter_panel("social", facets["social"]),
                                        combined_filters(combined_facets),
                                    ], id="shared-filters", className="shared-filters"),
                                ], id="collection-filters", className="collection-filters"),
                                html.Div(
                                    id="current-scope",
                                    className="current-scope",
                                    role="status",
                                ),
                                html.Div(
                                    [
                                        html.Div(
                                            [
                                                html.Section(
                                                    [
                                                        html.Div([
                                                            html.Span("Try a question"),
                                                            *[html.Button(label, id=identifier, n_clicks=0,
                                                                          title=question, className="query-example")
                                                              for identifier, label, question in QUERY_EXAMPLES],
                                                            html.Small("Choose an example, then select Generate answer. The model interprets your question; data tools calculate counts and retrieve sources." if agent_enabled else
                                                                       "Choose an example, then select Generate answer. Counts and lists use the database at no model charge."),
                                                        ], className="query-examples"),
                                                        html.Div(
                                                            id="research-stale",
                                                            role="status",
                                                        ),
                                                        html.Div([
                                                            html.Span(className="query-progress-spinner", **{"aria-hidden": "true"}),
                                                            html.Span("Preparing your search", id="research-progress-label"),
                                                        ], id="research-progress", className="query-progress",
                                                            style={"display": "none"}, role="status",
                                                            **{"aria-live": "polite", "aria-atomic": "true"}),
                                                        html.Div(
                                                            id="research-results",
                                                            **{"aria-live": "polite"},
                                                        ),
                                                    ],
                                                    id="query-page",
                                                    className="research-panel",
                                                    **{"aria-label": "Query"},
                                                ),
                                                html.Section(
                                                    [
                                                        _panel("native", enabled),
                                                        _panel("social", enabled),
                                                        combined_panel(enabled, _figure),
                                                    ],
                                                    id="data-page",
                                                    hidden=True,
                                                    **{"aria-label": "Data"},
                                                ),
                                            ],
                                            className="route-content",
                                        ),
                                    ],
                                    id="collection-layout",
                                    className="collection-layout",
                                ),
                            ],
                            id="collection-workspace",
                        ),
                        html.Section(
                            _wireframe(health),
                            id="wireframe-page",
                            hidden=True,
                            **{"aria-label": "Project wireframe"},
                        ),
                        html.Section(
                            evaluation_panel(published_report(settings)),
                            id="evaluation-page",
                            hidden=True,
                            **{"aria-label": "Evaluation"},
                        ),
                        html.Section(
                            [
                                _notice(
                                    "Page not found",
                                    "Choose Query or Data from the navigation.",
                                ),
                                dcc.Link("Go to Query →", href="/query"),
                            ],
                            id="not-found-page",
                            hidden=True,
                        ),
                    ],
                    id="main",
                    className="page-shell",
                ),
            ],
            id="observatory-app",
            className="view-query",
        )

    app.layout = layout
    from observatory.query_records import register_query_records, validation_panel

    # Answer-specific record controls appear only after a statistics answer.
    # Keep strict callback validation while declaring those dynamic components.
    app.validation_layout = html.Div([layout(), validation_panel()])
    register_query_records(app, service, enabled)

    @app.callback(
        Output("query-page", "hidden"),
        Output("data-page", "hidden"),
        Output("wireframe-page", "hidden"),
        Output("evaluation-page", "hidden"),
        Output("not-found-page", "hidden"),
        Output("collection-workspace", "hidden"),
        Output("page-title", "children"),
        Output("page-description", "children"),
        Output("nav-query", "className"),
        Output("nav-data", "className"),
        Output("nav-wireframe", "className"),
        Output("nav-evaluation", "className"),
        Output("observatory-app", "className"),
        Output("shared-filters", "hidden"),
        Output("query-composer", "hidden"),
        Output("query-options", "hidden"),
        Output("query-cost", "hidden"),
        Output("collection-filters", "open"),
        Input("page-location", "pathname"),
    )
    def change_page(pathname):
        page = _page(pathname)
        title, description = {
            "query": (
                "Query the evidence",
                "",
            ),
            "data": (
                "Explore the collection",
                "Compare sponsors and publishers, trace changes over time, and export the records in your selection.",
            ),
            "wireframe": (
                "Project wireframe",
                "See the page layouts, shared controls, and data flow behind the Observatory.",
            ),
            "evaluation": (
                "Evaluation",
                "What we measure, how we check it, and what remains to be evaluated.",
            ),
            "not-found": (
                "Page not found",
                "Return to Query, Data or a page in Tools.",
            ),
        }[page]
        return (
            page != "query",
            page != "data",
            page != "wireframe",
            page != "evaluation",
            page != "not-found",
            page not in {"query", "data"},
            title,
            description,
            *[
                "nav-link is-active" if page == target else "nav-link"
                for target in ("query", "data")
            ],
            "toolbox-link is-active" if page == "wireframe" else "toolbox-link",
            "toolbox-link is-active" if page == "evaluation" else "toolbox-link",
            f"view-{page}",
            page not in {"query", "data"},
            page != "query",
            page != "query",
            page != "query",
            page == "data",
        )

    @app.callback(
        Output("current-scope", "children"),
        Input("active-dataset", "value"),
        Input("search-scope", "value"),
        Input("page-location", "pathname"),
        *_filter_inputs("native"),
        *_filter_inputs("social"),
        *_filter_inputs("all"),
    )
    def current_scope(dataset, scope, pathname, *values):
        try:
            filters = _filters(
                dataset, *_collection_filter_values(dataset, values)
            )
        except ValueError:
            return _notice(
                "Check the filters",
                "The start date must be on or before the end date.",
                "warning",
            )
        selected = []
        for field in FILTER_NAMES:
            if getattr(filters, field):
                display = (sponsor_display if field == "sponsors" else
                           source_state_name if field == "labels" and dataset == "social" else str)
                selected.append(
                    f"{_filter_scope_name(field, dataset)}: {', '.join(dict.fromkeys(display(v) for v in getattr(filters, field)))}"
                )
        if filters.date_from or filters.date_to:
            selected.append(
                f"Dates: {filters.date_from or 'any'} – {filters.date_to or 'any'}"
            )
        if not filters.include_unknown_dates:
            selected.append("Unknown dates excluded")
        heading = {"native": "Native advertising", "social": "Company social posts", "all": "All records"}[dataset]
        message = " · ".join(selected)
        if dataset == "social":
            try:
                if not _social_available(service.health()):
                    message = "No admitted social records. " + message
            except Exception:  # noqa: BLE001 - no internal health details in the UI.
                pass
        if _page(pathname) == "query" and scope == "all":
            return [
                html.Strong("Both collections"),
                html.Span(" · Collection filters are bypassed for this query."),
            ]
        if not message and _page(pathname) == "data":
            message = {"native": "All sponsors and outlets", "social": "All accounts, platforms and company affiliations", "all": "Native advertisements and company social posts"}[dataset] + " · All publication dates · Unknown dates included"
        elif _page(pathname) == "data" and filters.include_unknown_dates:
            message += " · Unknown dates included"
        if not message:
            return None
        return [
            html.Strong(heading),
            html.Span(f" · {message}"),
        ]

    @app.callback(
        Output("research-stale", "children"),
        Input("research-submission", "data"),
        Input("research-question", "value"),
        Input("search-scope", "value"),
        Input("active-dataset", "value"),
        *_filter_inputs("native"),
        *_filter_inputs("social"),
        *_filter_inputs("all"),
    )
    def research_stale(submitted, question, scope, dataset, *values):
        if not submitted:
            return None
        try:
            current = _research_signature(question, scope, dataset, values)
        except ValueError:
            current = None
        if current == submitted:
            return None
        return _notice(
            "Results are from an earlier selection",
            "The question or search scope has changed. Run a new search to update the results below.",
            "warning",
        )

    @app.callback(
        Output("native-panel", "style"),
        Output("social-panel", "style"),
        Output("native-filter-panel", "style"),
        Output("social-filter-panel", "style"),
        Output("all-panel", "style"),
        Output("all-filter-panel", "style"),
        Output("shared-filters", "style"),
        Output("collection-layout", "style"),
        Input("active-dataset", "value"),
    )
    def change_collection(dataset):
        missing = (
            dataset == "social"
            and not _social_available(service.health())
        )
        return (
            {} if dataset == "native" else {"display": "none"},
            {} if dataset == "social" else {"display": "none"},
            {} if dataset == "native" else {"display": "none"},
            {} if dataset == "social" and not missing else {"display": "none"},
            {} if dataset == "all" else {"display": "none"},
            {} if dataset == "all" else {"display": "none"},
            {"display": "none"} if missing else {},
            {"gridTemplateColumns": "minmax(0, 1fr)"} if missing else {},
        )

    def register_collection(dataset):
        def social_admission_now():
            health = service.health()
            if health.get("status") != "ok":
                raise RuntimeError("Current collection state unavailable")
            return health, _social_admission(health) if _social_available(health) else None

        def public_page_rows(source_rows):
            rows = _public_rows(source_rows, enabled)
            for row in rows:
                row["archive_status"] = (
                    "Online archive" if row.get("archive_url") else "No verified archived copy"
                )
            if record_details is not None:
                summaries = record_details.summaries(source_rows)
                for row in rows:
                    row.update(summaries.get(row["record_id"], {}))
            return rows

        def read_dashboard(filters, **page_options):
            version_reader = getattr(service, "social_source_state_version", None)
            if dataset != "social" or not callable(version_reader):
                dashboard = service.dashboard(filters, **page_options)
                return dashboard, public_page_rows(dashboard["page"]["rows"]), service.health(), False
            # One retry tolerates a concurrent import or annotation update. Persistent
            # changes withhold the response instead of mixing old charts and new rows.
            for attempt in range(2):
                _, before = social_admission_now()
                dashboard = service.dashboard(filters, **page_options)
                rows = public_page_rows(dashboard["page"]["rows"])
                health, after = social_admission_now()
                stable = before == after
                if stable and after:
                    try:
                        snapshot = social_label_snapshot(dashboard.get("social_historical_labels"), filters.model_dump(mode="json"))
                    except ValueError:
                        snapshot = None  # Invalid source states are hidden, never treated as zero.
                    if snapshot:
                        stable = version_reader(filters) == snapshot["source_state_version"]
                health, final = social_admission_now()
                if stable and final == after:
                    return dashboard, rows, health, bool(attempt)
                page_options["offset"] = 0
            raise RuntimeError("Collection changed during the current read")

        @app.callback(
            *[Output(f"{dataset}-{name}", "value") for name in FILTER_NAMES],
            Output(f"{dataset}-dates", "start_date"), Output(f"{dataset}-dates", "end_date"),
            Output(f"{dataset}-unknown-dates", "value"),
            Input(f"{dataset}-clear-filters", "n_clicks"), prevent_initial_call=True,
        )
        def clear_filters(clicks):
            if not clicks:
                raise PreventUpdate
            return [*([] for _ in FILTER_NAMES), None, None, ["include"]]

        @app.callback(
            Output(f"{dataset}-grid", "rowData"),
            Output(f"{dataset}-summary", "children"),
            Output(f"{dataset}-primary-chart", "figure"),
            Output(f"{dataset}-sponsors-chart", "figure"),
            Output(f"{dataset}-timeline-chart", "figure"),
            Output(f"{dataset}-relationships-chart", "figure"),
            Output(f"{dataset}-status", "children"),
            Output(f"{dataset}-record-count", "children"),
            Output(f"{dataset}-labels-chart", "figure"),
            Output(f"{dataset}-labels-chart-note", "children"),
            Output(f"{dataset}-matrix", "rowData"),
            Output(f"{dataset}-matrix", "columnDefs"),
            Output(f"{dataset}-content", "style"),
            Output(f"{dataset}-relationships-chart", "style"),
            Output(f"{dataset}-labels-chart", "style"),
            Output(f"{dataset}-page-prev", "disabled"),
            Output(f"{dataset}-page-next", "disabled"),
            Output(f"{dataset}-page-label", "children"),
            Output(f"{dataset}-page-offset", "data"),
            Output(f"{dataset}-network-data", "data"),
            Output(f"{dataset}-platforms-chart", "figure"),
            *_filter_inputs(dataset),
            Input(f"{dataset}-metric", "value"),
            Input(f"{dataset}-page-prev", "n_clicks"),
            Input(f"{dataset}-page-next", "n_clicks"),
            Input(f"{dataset}-page-size", "value"),
            Input(f"{dataset}-sort", "value"),
            Input("active-dataset", "value"),
            Input("page-location", "pathname"),
            State(f"{dataset}-page-offset", "data"),
            State(f"{dataset}-network-data", "data"),
        )
        def update_collection(*values):
            (metric_value, _previous, _next, page_size, sort, active_dataset,
             pathname, page_offset, previous_snapshot) = values[FILTER_VALUE_COUNT:]
            if active_dataset != dataset or _page(pathname) != "data":
                raise PreventUpdate
            try:
                filters = _filters(dataset, *values[:FILTER_VALUE_COUNT])
                scope = filters.model_dump(mode="json")
                metric = "percent" if metric_value == "percent" else "count"
                size = page_size if page_size in (20, 50, 100) else 20
                offset = max(0, int(page_offset or 0))
                if ctx.triggered_id == f"{dataset}-page-next":
                    offset += size
                elif ctx.triggered_id == f"{dataset}-page-prev":
                    offset = max(0, offset - size)
                else:
                    offset = 0
                sort_value = sort if sort in ("date:desc", "date:asc", "title:asc", "sponsor:asc") else "date:desc"
                sort_by, direction = sort_value.split(":")
                page_refreshed = False
                guard_key = "social_page_guard"
                version_reader = getattr(service, "social_source_state_version", None)
                page_trigger = ctx.triggered_id in {
                    "social-page-next", "social-page-prev", "social-page-size", "social-sort",
                }
                if dataset == "social" and page_trigger and callable(version_reader):
                    guard = session.get(guard_key)
                    _, admission = social_admission_now()
                    previous = previous_snapshot if isinstance(previous_snapshot, dict) else {}
                    labels = previous.get("social_historical_labels") or {}
                    # The signed session records a server-rendered selection. Browser stores
                    # alone cannot authorize reusing its statistics or source-state chart.
                    if (isinstance(guard, dict) and admission and admission.get("data_version")
                            and guard.get("filters") == scope and guard.get("metric") == metric
                            and guard.get("admission") == admission
                            and previous.get("filters") == scope
                            and previous.get("social_admission") == admission
                            and isinstance(labels, dict)
                            and labels.get("source_state_version") == guard.get("source_state_version")
                            and labels.get("total") == guard.get("total")
                            and version_reader(filters) == guard.get("source_state_version")):
                        page = service.page(filters, offset=offset, limit=size,
                                            sort_by=sort_by, descending=direction == "desc")
                        rows = public_page_rows(page["rows"])
                        if (page["total"] == guard["total"]
                                and version_reader(filters) == guard["source_state_version"]
                                and social_admission_now()[1] == admission):
                            offset = page["offset"]
                            result = [no_update] * 21
                            result[0] = rows
                            result[15:19] = [
                                offset == 0, offset + size >= page["total"],
                                f"{offset + 1 if rows else 0:,}–{offset + len(rows):,} of {page['total']:,}",
                                offset,
                            ]
                            return tuple(result)
                    # A changed or missing guard requires current aggregates, not a stale page.
                    offset, page_refreshed = 0, True
                if dataset == "social":
                    session.pop(guard_key, None)
                dashboard, rows, health, retried = read_dashboard(filters, offset=offset, limit=size,
                                                                  sort_by=sort_by, descending=direction == "desc")
                page_refreshed = page_refreshed or retried
                if dataset == "social" and ctx.triggered_id in {"social-page-next", "social-page-prev"}:
                    try:
                        current_labels = social_label_snapshot(dashboard.get("social_historical_labels"), filters.model_dump(mode="json"))
                        current_admission = _social_admission(health)
                    except ValueError:
                        current_labels = None
                        current_admission = None
                    if (not isinstance(previous_snapshot, dict)
                            or previous_snapshot.get("social_historical_labels") != current_labels
                            or previous_snapshot.get("social_admission") != current_admission
                            or current_labels is None):
                        if dashboard["page"]["offset"]:
                            dashboard, rows, health, _ = read_dashboard(filters, offset=0, limit=size,
                                                                        sort_by=sort_by, descending=direction == "desc")
                        page_refreshed = True
                stats, page = dashboard["stats"], dashboard["page"]
                offset = page["offset"]
                matrix = dashboard["matrix"]
                labels = dashboard["labels"]
                empty_social = (
                    dataset == "social"
                    and not _social_available(health)
                )
                notice = (
                    _notice(
                        "No admitted company social posts",
                        "These collected company posts are not verified paid advertisements. Exploration becomes available after their source records are admitted.",
                    )
                    if empty_social
                    else _notice("Collected company posts", "Counts use one post per platform and original URL. Company affiliation is source metadata; paid advertising is not verified. Conflicting source values remain unknown, and conflicting text is excluded from retrieval.") if dataset == "social" else None
                )
                if not stats["total"] and not empty_social:
                    notice = _notice(
                        "No matching records",
                        "Try widening the dates or clearing a filter.",
                    )
                elif page_refreshed and not empty_social:
                    notice = _notice("Selection refreshed", "The source-state selection changed. Showing the current first page; your filters are preserved.")
                primary = _bars(stats.get("publishers" if dataset == "native" else "accounts"), metric)
                network_payload = {"relationships": stats.get("relationships", []), "filters": filters.model_dump(mode="json")}
                labels_figure = _label_chart(distribution=labels) if dataset == "native" else _figure("Source states unavailable")
                labels_note = (f"Historical automated labels; a record can have several. {labels['unlabeled_records']:,} of {labels['total']:,} selected records have no historical label. These are not verified themes."
                               if dataset == "native" else "Historical source-state distribution is unavailable. Refresh the selection when source annotations are available; missing coverage is not zero.")
                labels_style = {"height": f"{max(340, 38 * len(labels['items']) + 70)}px", "width": "100%"}
                if dataset == "social":
                    network_payload["accounts"] = list(primary.data[0].y) if primary.data else []
                    network_payload["social_historical_labels"] = None
                    network_payload["social_admission"] = None
                    if empty_social:
                        labels_note = "No admitted social records. Historical source-state counts are unavailable."
                    else:
                        network_payload["social_admission"] = _social_admission(health)
                        try:
                            label_snapshot = social_label_snapshot(dashboard.get("social_historical_labels"), network_payload["filters"])
                            if label_snapshot["total"] != stats["total"]:
                                raise ValueError("Source-state denominator differs from the selection")
                            network_payload["social_historical_labels"] = label_snapshot
                            labels_figure = social_label_figure(label_snapshot, metric) if stats["total"] else _figure("No matching posts")
                            labels_note = social_label_note(label_snapshot)
                            admission = network_payload["social_admission"]
                            if admission and admission.get("data_version") and callable(version_reader):
                                session[guard_key] = {
                                    "filters": scope, "metric": metric, "admission": admission,
                                    "source_state_version": label_snapshot["source_state_version"],
                                    "total": stats["total"],
                                }
                        except ValueError:
                            pass
                    labels_style = {"height": "760px", "width": "100%"}
                return (
                    rows,
                    _summary(stats, dataset),
                    primary,
                    _bars(
                        [
                            dict(item, name=sponsor_display(item["name"]))
                            for item in stats.get("sponsors", [])
                        ],
                        metric,
                    ),
                    _timeline(dashboard["timeline"]),
                    _relationships(stats.get("relationships")),
                    notice,
                    f"{stats['total']:,} {'unique posts' if dataset == 'social' else 'records'} in the current selection. Charts and downloads use the full selection.",
                    labels_figure,
                    labels_note,
                    matrix["table_rows"],
                    matrix["table_columns"],
                    {"display": "none"} if empty_social else {},
                    {
                        "height": f"{max(380, 38 * len(matrix['sponsors']) + 150)}px",
                        "width": "100%",
                    },
                    labels_style,
                    offset == 0,
                    offset + size >= page["total"],
                    f"{offset + 1 if rows else 0:,}–{offset + len(rows):,} of {page['total']:,}",
                    offset,
                    no_update if network_payload == previous_snapshot else network_payload,
                    _bars(stats.get("platforms"), metric),
                )
            except ValueError:
                message = _notice(
                    "Check the filters",
                    "Use a valid date range with the start date before the end date.",
                    "warning",
                )
            except Exception:  # noqa: BLE001 - public boundary must hide unexpected service details.
                message = _notice(
                    "Collection temporarily unavailable",
                    "Your filters are preserved. Please try again when the data service is available.",
                    "warning",
                )
            return (
                [],
                _summary({}),
                _figure("Unavailable"),
                _figure("Unavailable"),
                _figure("Unavailable"),
                _figure("Unavailable"),
                message,
                "Records unavailable",
                _figure("Unavailable"),
                "Labels unavailable",
                [],
                [],
                {"display": "none"},
                {"height": "340px", "width": "100%"},
                {"height": "300px", "width": "100%"},
                True, True, "Unavailable", 0, {"relationships": [], "filters": {}},
                _figure("Unavailable"),
            )

        @app.callback(
            Output(f"{dataset}-matrix-download", "data"),
            Output(f"{dataset}-matrix-export-status", "children"),
            Input(f"{dataset}-matrix-export", "n_clicks"),
            *_filter_inputs(dataset, State),
            State("page-location", "pathname"),
            prevent_initial_call=True,
        )
        def export_matrix(clicks, *values):
            if not clicks or _page(values[-1]) != "data":
                raise PreventUpdate
            try:
                rows = service.browse(_filters(dataset, *values[:-1]))
                return {
                    "content": sponsor_publisher_csv(rows),
                    "filename": f"{dataset}-sponsor-outlet-cross-tab.csv",
                    "type": "text/csv",
                }, f"Downloaded cross-tab for {len(rows):,} selected records."
            except Exception:
                return None, "Cross-tab download is temporarily unavailable."

        @app.callback(
            Output(f"{dataset}-download", "data"),
            Output(f"{dataset}-export-status", "children"),
            Input(f"{dataset}-export", "n_clicks"),
            *_filter_inputs(dataset, State),
            *([State("social-network-data", "data")] if dataset == "social" else []),
            State("page-location", "pathname"),
            prevent_initial_call=True,
        )
        def export_collection(clicks, *values):
            if not clicks or _page(values[-1]) != "data":
                raise PreventUpdate
            values = values[:-1]
            try:
                previous = values[-1] if dataset == "social" else None
                filter_values = values[:-1] if dataset == "social" else values
                filters = _filters(dataset, *filter_values)
                snapshot = _current_social_snapshot(service, filters, previous) if dataset == "social" else None
                source_rows = service.browse(filters)
                if dataset == "social":
                    identifiers = set()
                    for raw in source_rows:
                        row = _mapping(raw)
                        states = public_social_states(row)
                        if (not row.get("record_id") or not row.get("version_id")
                                or row["record_id"] in identifiers
                                or (filters.labels and not set(filters.labels).intersection(states))):
                            raise ValueError("Export membership differs from the selection")
                        identifiers.add(row["record_id"])
                    if len(source_rows) != snapshot["total"] or _current_social_snapshot(service, filters, previous) != snapshot:
                        raise ValueError("Export selection changed")
                rows = _public_rows(source_rows, enabled)
                fields = list(
                    NATIVE_COLUMNS if dataset == "native" else SOCIAL_COLUMNS
                ) + ["record_id", "version_id", "labels", "retrievable"]
                if dataset == "social":
                    fields[fields.index("sponsor")] = "company_affiliation"
                    fields.extend(["collection_scope", "count_unit", "paid_ad_status", "social_historical_states", "social_historical_scheme", "social_historical_status"])
                if enabled:
                    fields.append("archive_url")
                output = StringIO(newline="")
                writer = csv.DictWriter(
                    output, fieldnames=fields, extrasaction="ignore"
                )
                writer.writeheader()
                for row in rows:
                    row = dict(row, labels="; ".join(row["labels"]))
                    if dataset == "social":
                        row["company_affiliation"] = row.pop("sponsor")
                        row["social_historical_states"] = "; ".join(row["social_historical_states"])
                    # Keep spreadsheet applications from interpreting source text as formulas.
                    writer.writerow(
                        {
                            k: "'" + v
                            if isinstance(v, str)
                            and v.startswith(("=", "+", "-", "@", "\t", "\r"))
                            else v
                            for k, v in row.items()
                        }
                    )
                return {
                    "content": output.getvalue(),
                    "filename": "social-company-posts.csv" if dataset == "social" else "native-advertising.csv",
                    "type": "text/csv",
                }, f"Downloaded {len(rows):,} selected records."
            except Exception:  # noqa: BLE001 - public boundary must hide unexpected service details.
                return (
                    None,
                    "Download is unavailable. Refresh the current source-state selection and retry; your filters are preserved."
                    if dataset == "social" else "Download is unavailable. Your current filters are preserved.",
                )

    register_collection("native")
    register_collection("social")

    @app.callback(
        Output("social-accounts", "value", allow_duplicate=True),
        Output("social-view", "value"),
        Input("social-primary-chart", "clickData"),
        State("social-network-data", "data"),
        *_filter_inputs("social", State),
        prevent_initial_call=True,
    )
    def select_social_account(click, snapshot, *values):
        try:
            filters = _filters("social", *values)
            if not snapshot or snapshot.get("filters") != filters.model_dump(mode="json"):
                raise PreventUpdate
            points = (click or {}).get("points") or []
            if len(points) != 1:
                raise PreventUpdate
            point = points[0]
            index = point.get("pointNumber")
            curve = point.get("curveNumber")
            names = snapshot.get("accounts") or []
            if (not isinstance(names, list) or not isinstance(curve, int)
                    or isinstance(curve, bool) or curve != 0 or not isinstance(index, int)
                    or isinstance(index, bool) or not 0 <= index < len(names)
                    or not isinstance(names[index], str) or point.get("y") != names[index]):
                raise PreventUpdate
            account = names[index]
            if filters.accounts and account not in filters.accounts:
                raise PreventUpdate
            return [account], "records"
        except (TypeError, ValueError, AttributeError, KeyError):
            raise PreventUpdate from None

    @app.callback(
        Output("social-labels", "value", allow_duplicate=True),
        Output("social-view", "value", allow_duplicate=True),
        Output("social-label-click-status", "children"),
        Input("social-labels-chart", "clickData"),
        State("social-network-data", "data"),
        *_filter_inputs("social", State),
        State("page-location", "pathname"),
        State("active-dataset", "value"),
        State("social-view", "value"),
        prevent_initial_call=True,
    )
    def select_social_label(click, previous, *values):
        if values[-3:] != ("/data", "social", "overview"):
            raise PreventUpdate
        try:
            filters = _filters("social", *values[:-3])
            current = _current_social_snapshot(service, filters, previous)
            identifier = clicked_social_state(click, current)
            if filters.labels and identifier not in filters.labels:
                return no_update, no_update, "This segment is outside the selected OR states. Change or clear the Historical source state dropdown before exploring it."
            return [identifier], "records", f"Showing posts with {source_state_name(identifier)}. Other filters are preserved."
        except Exception:  # noqa: BLE001 - do not expose service details or change scope on a stale click.
            raise PreventUpdate from None

    from observatory.knowledge_ui import register_knowledge_graph

    register_knowledge_graph(app, service, enabled, record_details=record_details, require_open=True)
    from observatory.collection_graph_ui import register_collection_graph

    register_collection_graph(app, service, enabled)
    from observatory.data_ui import register_data_views

    register_data_views(app, service, enabled)

    from .combined_ui import register_combined

    register_combined(app, service, enabled, record_details=record_details)

    from observatory.research_explorer import register_research_explorer

    register_research_explorer(app, service, enabled)
    from observatory.historical_theme_ui import register_historical_themes

    register_historical_themes(app, service, enabled)
    from observatory.claims_ui import register_claims_browser

    register_claims_browser(app, service, enabled)
    from observatory.content_ui import register_content_browser

    register_content_browser(app, service, enabled)

    @app.callback(
        Output("research-question", "value"),
        *[Input(identifier, "n_clicks") for identifier, _, _ in QUERY_EXAMPLES],
        prevent_initial_call=True,
    )
    def choose_example(*clicks):
        for (identifier, _, question), clicked in zip(QUERY_EXAMPLES, clicks):
            if ctx.triggered_id == identifier and isinstance(clicked, int) and clicked > 0:
                return question
        raise PreventUpdate

    @app.callback(
        Output("research-progress-label", "children"),
        Input("research-progress-poll", "n_intervals"),
        State("research-progress-token", "data"),
        prevent_initial_call=True,
    )
    def show_research_progress(_ticks, token):
        state = progress_registry.snapshot(session.get("visitor_id"), token)
        return state["label"] if state["status"] == "running" else "Preparing your search"

    @app.callback(
        Output("research-results", "children"),
        Output("research-submission", "data"),
        Input("search-free", "n_clicks"),
        Input("answer-paid", "n_clicks"),
        State("research-question", "value"),
        State("search-scope", "value"),
        State("active-dataset", "value"),
        *_filter_inputs("native", State),
        *_filter_inputs("social", State),
        *_filter_inputs("all", State),
        State("research-progress-token", "data"),
        State("page-location", "pathname"),
        prevent_initial_call=True,
        running=[
            (Output("search-free", "disabled"), True, False),
            (Output("answer-paid", "disabled"), True, False),
            (Output("research-progress", "style"), {}, {"display": "none"}),
            (Output("research-progress-poll", "disabled"), False, True),
        ],
    )
    def research(search_clicks, answer_clicks, question, scope, dataset, *values):
        pathname, token, values = values[-1], values[-2], values[:-2]
        if pathname is None or _page(pathname) != "query":
            raise PreventUpdate
        if ctx.triggered_id not in {"search-free", "answer-paid"}:
            raise PreventUpdate
        clicks = search_clicks if ctx.triggered_id == "search-free" else answer_clicks
        if not isinstance(clicks, int) or isinstance(clicks, bool) or clicks <= 0:
            raise PreventUpdate
        owner = session["visitor_id"]
        request_id = progress_registry.begin(owner, token)
        progress = progress_registry.reporter(owner, token, request_id)
        try:
            result = run_research(question, scope, dataset, values, progress)
        except Exception:
            progress_registry.finish(owner, token, request_id, status="failed")
            raise
        finally:
            progress_registry.finish(owner, token, request_id)
        try:
            submitted = _research_signature(question, scope, dataset, values)
        except ValueError:
            submitted = None
        return result, submitted

    def run_research(question, scope, dataset, values, progress):
        question = (question or "").strip()
        if not question or len(question) > 2000:
            return _notice(
                "Enter a research question",
                "Use between 1 and 2,000 characters.",
                "warning",
            )
        try:
            filters = (
                Filters(dataset="all")
                if scope == "all"
                else _filters(
                    dataset, *_collection_filter_values(dataset, values)
                )
            )
        except ValueError:
            return _notice(
                "Check the filters",
                "Use a valid date range before searching.",
                "warning",
            )
        context = _search_context(question, filters)
        if ctx.triggered_id == "search-free":
            try:
                progress("database")
                report = (
                    service.search_report(question, filters, limit=5)
                    if hasattr(service, "search_report")
                    else {
                        "evidence": service.search(question, filters, limit=5),
                        "diagnostics": {},
                    }
                )
                return [context, _keyword_result(report, enabled)]
            except Exception:  # noqa: BLE001 - public boundary must hide unexpected service details.
                return [
                    context,
                    _answer_status(
                        "Search temporarily unavailable",
                        "The data service could not complete this search.",
                        "Please try again later or browse the records in Data.",
                    ),
                ]
        try:
            if "visitor_id" not in session:
                session["visitor_id"] = secrets.token_urlsafe(24)
            arguments = {"visitor": session["visitor_id"]}
            parameters = inspect.signature(service.answer).parameters
            if "progress" in parameters or any(parameter.kind == inspect.Parameter.VAR_KEYWORD
                                                 for parameter in parameters.values()):
                arguments["progress"] = progress
            result = _mapping(
                service.answer(question, filters, **arguments)
            )
            progress("organizing")
            if result.get("status") == "answered" and result.get("answer_mode") in {"statistics", "tools"} and result.get("structured_result"):
                effective = Filters.model_validate(result["structured_result"].get("filters") or filters.model_dump())
                context = _search_context(question, effective)
            return [context, *_render_answer_result(result, enabled, service)]
        except Exception:  # noqa: BLE001 - public boundary must hide unexpected service details.
            try:
                evidence = service.search(question, filters, limit=5)
            except Exception:  # noqa: BLE001 - public boundary must hide unexpected service details.
                evidence = []
            return [
                context,
                _answer_status(
                    "Answer service unavailable",
                    "The answer service could not complete this request. Keyword results are shown when available.",
                    "You can continue browsing and searching; submit the question again to retry.",
                ),
                *_evidence_cards(evidence, enabled),
            ]

    return app
