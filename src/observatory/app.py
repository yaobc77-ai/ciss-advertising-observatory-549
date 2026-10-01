"""Public Dash interface. Only explicitly selected public data reaches the browser."""

from __future__ import annotations

import csv
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
from flask import session

from observatory.analytics import (
    historical_label_distribution,
    sponsor_display,
    sponsor_publisher_csv,
)
from observatory.models import Filters

UNKNOWN = "(Unknown)"
NATIVE_COLUMNS = ("url", "publisher", "title", "date", "sponsor", "keyword")
SOCIAL_COLUMNS = ("platform", "account", "sponsor", "title", "date", "url")
FILTER_NAMES = ("publishers", "sponsors", "platforms", "keywords", "labels")
COLORS = {"ink": "#202633", "teal": "#003262", "muted": "#687587", "amber": "#a86b25"}
QUERY_EXAMPLES = (
    ("example-outlet-count", "Count ads at an outlet", "How many native ads are from the New York Times?"),
    ("example-company-outlets", "Explore a company's publishers", "Which publishers is ExxonMobil working with?"),
    ("example-outlet-sponsors", "Explore an outlet's sponsors", "Which fossil fuel companies has the Washington Post worked with?"),
)


def _page(pathname):
    return {
        None: "query",
        "/": "query",
        "/query": "query",
        "/data": "data",
        "/wireframe": "wireframe",
    }.get((pathname.rstrip("/") or "/") if pathname else pathname, "not-found")


def _research_signature(question, scope, dataset, values):
    """Keep the submitted scope separate from controls that can change later."""
    filters = (
        Filters(dataset="all")
        if scope == "all"
        else _filters(dataset, *(values[:8] if dataset == "native" else values[8:]))
    )
    return {
        "question": (question or "").strip(),
        "filters": filters.model_dump(mode="json"),
    }


def _wireframe(health):
    """A structural diagram of this release, not screenshots or simulated records."""
    native_count = health.get("record_counts", {}).get("native", 0)
    social_count = health.get("record_counts", {}).get("social", 0)

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
                html.H2("Three pages, one collection scope"),
                html.P(
                    "Query and Data share collection filters. Navigation keeps the current question and results in place.",
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
                            else "Social · awaiting dataset",
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
                                            "Answer + original evidence",
                                            "Source links · record and version · exact quotes",
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
                    "Project wireframe",
                    "/wireframe",
                    [
                        block(
                            "Page relationship diagram",
                            "Query ↔ Data ↔ Project wireframe",
                        ),
                        block("Page structures", "Component layout and shared scope"),
                        block(
                            "Data flow",
                            "Sources → quality / versions → index → interface",
                        ),
                        block(
                            "Delivery boundaries",
                            "Native data available · social data slot reserved",
                        ),
                    ],
                ),
            ],
            className="wire-screens",
            **{"aria-label": "Wireframes for the three application pages"},
        ),
        html.Section(
            [
                html.Span("Behind the pages", className="eyebrow"),
                html.H2("Project data flow"),
                html.Div(
                    [
                        block(
                            "01 · Source material",
                            "Native records / archived articles / supplied exports",
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
                                    "SQL counts → network / charts → paged records / CSV",
                                    "wire-output",
                                ),
                                block(
                                    "Query page",
                                    "Retrieve → optional model answer → verified source quotes",
                                    "wire-output",
                                ),
                            ],
                            className="wire-stack",
                        ),
                    ],
                    className="wire-flow",
                ),
                html.P(
                    "Counts describe the eligible collection. Retrieved passages support what an advertiser said; they do not establish whether a claim is true. Historical CLAIMS labels remain filterable legacy annotations.",
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
        result.append(row)
    return result


def _filters(
    dataset,
    publishers,
    sponsors,
    platforms,
    keywords,
    labels,
    date_from,
    date_to,
    unknown_dates,
):
    filters = Filters(
        dataset=dataset,
        publishers=publishers or [],
        sponsors=sponsors or [],
        platforms=platforms or [],
        keywords=keywords or [],
        labels=labels or [],
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
        "social": "Social advertising",
        "all": "Both collections · all eligible records",
    }[filters.dataset]
    selections = [collection]
    for name in FILTER_NAMES:
        values = getattr(filters, name)
        if values:
            selections.append(
                f"{name.replace('_', ' ').title()}: {', '.join(sponsor_display(v) for v in values) if name == 'sponsors' else ', '.join(values)}"
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


def _statistics_card(result, links_enabled):
    """Display complete SQL categories and bounded example record references."""
    from observatory.network_ui import record_cards

    data = result["structured_result"]
    collections = data.get("collections", [])
    groups = data.get("groups", [])
    records = data.get("records", [])
    names = {"native": "Native ad records", "social": "Social ad records"}
    model_query = bool(result.get("research_trace"))
    is_share = data.get("kind") == "share"
    sections = [
        html.Span("Collection statistics · model-assisted query" if model_query
                  else "Collection statistics · no model charge", className="eyebrow"),
        html.H3("Share of the current selection" if is_share else "Records in this selection"),
        html.P(result["answer"], className="answer-text"),
    ]
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
    if groups:
        sections.append(html.Div(html.Table([
            html.Caption({"publishers": "All publishers and counts", "sponsors": "All source-listed sponsors / organizations and counts",
                          "platforms": "All platforms and counts"}.get(data.get("group_by"), "All categories and counts")),
            html.Thead(html.Tr([
                html.Th({"publishers": "News outlet", "sponsors": "Source-listed sponsor / organization", "platforms": "Platform"}.get(data.get("group_by"), "Category"), scope="col"),
                html.Th("Collection", scope="col"), html.Th("Records", scope="col"),
            ])),
            html.Tbody([html.Tr([
                html.Th(group.get("display_name") or group["name"], scope="row"),
                html.Td(names[group["dataset"]]), html.Td(f"{group['count']:,}", className="count-value"),
            ]) for group in groups]),
        ]), className="statistics-table"))
    notes = [
        f"{names[item['dataset']]}: {item['total']:,} eligible · {item['retrievable']:,} searchable · {item['unknown_dates']:,} with unknown dates."
        for item in collections
    ]
    sections.extend([
        html.Ul([html.Li(note) for note in notes + data.get("scope_notes", [])], className="scope-note"),
        html.P("Computed from all eligible stored records in this selection, including records without searchable text. Native articles and social posts are separate units. These are collection counts, not a census of all advertising.", className="scope-note"),
    ])
    if records:
        total = sum(item["total"] for item in collections)
        sections.append(html.Details([
            html.Summary(f"Inspect matching records · showing {len(records):,} of {total:,}"),
            *record_cards(records, links_enabled),
            dcc.Link("Explore the full collection →", href="/data"),
        ], className="statistics-records"))
    sections.append(_research_steps(result))
    return html.Div(sections, className="answer-card statistics-answer")


def _research_steps(result):
    """Public tool execution trace, rather than private model reasoning."""
    trace = result.get("research_trace") or {}
    if not trace:
        return None
    steps = trace.get("tools") or []
    calls = trace.get("model_calls") or []
    call_label = "model call" if len(calls) == 1 else "model calls"
    return html.Details([
        html.Summary("How this question was answered"),
        html.P(f"Model-assisted interpretation · {len(calls)} {call_label} · API cost ${result.get('cost_usd', 0):.5f}. Database calculations and source reads do not call a model."),
        html.Ol([html.Li([
            html.Code(str(step.get("name") or step.get("tool") or "read-only tool")),
            html.Span(f" · {step.get('status') or step.get('result_status') or 'completed'}"),
        ]) for step in steps]),
        html.P("Tools preserve the current filters. Source relationships and historical annotations retain their review limits."),
    ], className="research-steps")


def _tools_card(result, links_enabled):
    """Render typed, bounded tool results beside their record references."""
    from observatory.network_ui import record_cards
    from observatory.service import safe_url

    data = result["structured_result"]
    kind = data.get("kind")
    sections = [html.Span("Read-only data tools", className="eyebrow")]
    if kind == "claims":
        from observatory.claims_ui import claims_record_cards, public_claims

        sections.extend([html.H3("Published CLAIMS2 assignments"), html.P(result["answer"], className="answer-text")])
        try:
            claims = public_claims(data)
            sections.append(html.P(claims["note"], className="scope-note"))
            sections.extend(claims_record_cards(claims["records"], links_enabled))
            if not claims["records"]:
                sections.append(html.P("No published matching assignments are available in this selection."))
        except (TypeError, ValueError):
            sections.append(html.P("These CLAIMS2 results are unavailable. Submit the question again.", className="scope-note"))
        sections.append(dcc.Link("Browse published evidence in Data →", href="/data"))
    elif kind == "graph":
        graph = data.get("graph") or {}
        nodes = {node["id"]: node for node in graph.get("nodes", [])}
        sections.extend([
            html.H3("Source relationships"),
            html.P("This is a page of recorded relationships. It is not the complete graph and does not establish corporate contracts or verified claims.", className="scope-note"),
            html.Div(html.Table([
                html.Caption("Typed relationships and their originating record"),
                html.Thead(html.Tr([html.Th(label, scope="col") for label in ("From", "Relationship", "To", "Source record")])),
                html.Tbody([html.Tr([
                    html.Td(nodes.get(edge["source"], {}).get("label", "Source")),
                    html.Td(edge.get("label") or edge["predicate"]),
                    html.Td(nodes.get(edge["target"], {}).get("label", "Target")),
                    html.Td(dcc.Link("Open record", href="/records/" + quote(str(edge.get("provenance", {}).get("record_id", "")), safe=""))),
                ]) for edge in graph.get("edges", [])]),
            ]), className="statistics-table"),
            dcc.Link("Explore the article knowledge graph →", href="/data"),
        ])
    elif kind in {"record", "sources"}:
        record = data.get("record") or {}
        sections.append(html.H3("Article text" if kind == "record" else "Sources for this record"))
        if record.get("record_id"):
            sections.extend(record_cards([record], links_enabled))
        if kind == "record":
            body = data.get("body") or {}
            sections.extend([
                html.P(f"Characters {body.get('start', 0):,}–{body.get('end', 0):,} of {body.get('total_characters', 0):,} · stored text completeness has not been established.", className="scope-note"),
                html.Pre(body.get("text") or "No stored article text is available.", className="tool-article-text"),
            ])
        else:
            artifacts = data.get("source_artifacts") or []
            sources = []
            for artifact in artifacts:
                properties = artifact.get("properties") or {}
                url = safe_url(properties.get("url")) if links_enabled else ""
                sources.append(html.Li([
                    html.A(artifact.get("label", "Source reference"), href=url, target="_blank", rel="noopener noreferrer") if url else html.Span(artifact.get("label", "Source reference")),
                    html.Span(" · URL recorded; contents not independently verified"),
                ]))
            sections.append(html.Ul(sources) if sources else html.P("No public source references are available for this record."))
            sections.append(html.P("Historical annotations are unverified. The reviewed attachment adapter is not connected to this tool yet.", className="scope-note"))
    sections.extend([html.Ul([html.Li(note) for note in data.get("scope_notes", [])], className="scope-note"), _research_steps(result)])
    return html.Div(sections, className="answer-card")


def _summary(stats):
    values = [
        (
            "Selected records",
            stats.get("total", 0),
            "Eligible records in this selection",
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
    for citation in citations:
        citation = _mapping(citation)
        citation_quotes.setdefault(citation.get("evidence_id"), []).append(
            str(citation.get("quote") or "")
        )
    cards = []
    for rank, value in enumerate(evidence, start=1):
        item = _mapping(value)
        passage = str(item.get("text") or "")
        supported = [
            quote
            for quote in citation_quotes.get(item.get("evidence_id"), [])
            if quote and quote in passage
        ]
        excerpt = supported[0] if supported else passage
        excerpt = excerpt[:520] + ("…" if len(excerpt) > 520 else "")
        cards.append(
            html.Article(
                [
                    html.Div(
                        [
                            html.Span(
                                "Native advertising"
                                if item.get("dataset") == "native"
                                else "Social advertising",
                                className="dataset-chip",
                            ),
                            html.Span(
                                f"Result {rank}",
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
                    html.Blockquote(excerpt),
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
    return cards


def _filter_inputs(dataset, dependency=Input):
    return [dependency(f"{dataset}-{name}", "value") for name in FILTER_NAMES] + [
        dependency(f"{dataset}-dates", "start_date"),
        dependency(f"{dataset}-dates", "end_date"),
        dependency(f"{dataset}-unknown-dates", "value"),
    ]


def _filter_panel(dataset, facets):
    native = dataset == "native"
    names = {
        "publishers": "News outlet",
        "sponsors": "Sponsor / advertiser",
        "platforms": "Platform",
        "keywords": "Collection search term",
        "labels": "Historical automated label",
    }
    controls = []
    for field in FILTER_NAMES:
        hidden = (native and field == "platforms") or (
            not native and field == "publishers"
        )
        controls.append(
            html.Div(
                [
                    html.Label(names[field], htmlFor=f"{dataset}-{field}"),
                    dcc.Dropdown(
                        id=f"{dataset}-{field}",
                        options=[
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
        )
    controls += [
        html.Div(
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
        ),
        dcc.Checklist(
            id=f"{dataset}-unknown-dates",
            options=[{"label": " Include unknown dates", "value": "include"}],
            value=["include"],
            className="unknown-toggle",
            persistence=True,
            persistence_type="session",
        ),
        html.Details(
            [
                html.Summary("About these filters"),
                html.P(
                    "Historical labels come from earlier automated classification runs. Collection search terms describe how records were collected; they are not sponsor identities or article themes."
                ),
            ],
            className="filter-note",
        ),
    ]
    controls[5].children.append(controls[6])
    return html.Aside(
        [
            html.Div([controls[1], controls[0] if native else controls[2], controls[5]], className="common-filters"),
            html.Details([
                html.Summary("More filters"),
                html.Div([controls[3], controls[4], controls[2] if native else controls[0]], className="advanced-filter-fields"),
                controls[7],
            ], className="advanced-filters"),
            html.Button("Clear filters", id=f"{dataset}-clear-filters", n_clicks=0, className="button button-quiet clear-filters"),
        ],
        id=f"{dataset}-filter-panel",
        className="filters-panel",
        style={} if native else {"display": "none"},
        **{
            "aria-label": "Native advertising filters"
            if native
            else "Social advertising filters"
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
    agent_enabled = bool(getattr(settings, "research_agent_enabled", False))
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
                                {"label": "Native advertising", "value": "native"},
                                {"label": "Social advertising", "value": "social"},
                            ],
                            value="native",
                            clearable=False,
                            searchable=False,
                            persistence=True,
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
                            placeholder="Ask a question about these ads…",
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
                            "label": "Both collections · all eligible records",
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
                            "Question understanding uses the project's API budget, including counting questions. Database calculations, source reads and keyword search are free. Questions can contain up to 2,000 characters." if agent_enabled else
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
                                                        dcc.Loading(
                                                            html.Div(
                                                                id="research-results",
                                                                **{
                                                                    "aria-live": "polite"
                                                                },
                                                            ),
                                                            type="circle",
                                                            color=COLORS["teal"],
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

    @app.callback(
        Output("query-page", "hidden"),
        Output("data-page", "hidden"),
        Output("wireframe-page", "hidden"),
        Output("not-found-page", "hidden"),
        Output("collection-workspace", "hidden"),
        Output("page-title", "children"),
        Output("page-description", "children"),
        Output("nav-query", "className"),
        Output("nav-data", "className"),
        Output("nav-wireframe", "className"),
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
            "not-found": (
                "Page not found",
                "Return to one of the three Observatory pages.",
            ),
        }[page]
        return (
            page != "query",
            page != "data",
            page != "wireframe",
            page != "not-found",
            page not in {"query", "data"},
            title,
            description,
            *[
                "nav-link is-active" if page == target else "nav-link"
                for target in ("query", "data")
            ],
            "toolbox-link is-active" if page == "wireframe" else "toolbox-link",
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
    )
    def current_scope(dataset, scope, pathname, *values):
        try:
            filters = _filters(
                dataset, *(values[:8] if dataset == "native" else values[8:])
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
                selected.append(
                    f"{field.title()}: {', '.join(sponsor_display(v) for v in getattr(filters, field)) if field == 'sponsors' else ', '.join(getattr(filters, field))}"
                )
        if filters.date_from or filters.date_to:
            selected.append(
                f"Dates: {filters.date_from or 'any'} – {filters.date_to or 'any'}"
            )
        if not filters.include_unknown_dates:
            selected.append("Unknown dates excluded")
        heading = "Native advertising" if dataset == "native" else "Social advertising"
        message = " · ".join(selected)
        if dataset == "social":
            try:
                if service.health().get("record_counts", {}).get("social", 0) == 0:
                    message = "Dataset not connected. " + message
            except Exception:  # noqa: BLE001 - no internal health details in the UI.
                pass
        if _page(pathname) == "query" and scope == "all":
            return [
                html.Strong("Both collections"),
                html.Span(" · Collection filters are bypassed for this query."),
            ]
        if not message and _page(pathname) == "data":
            message = "All sponsors and outlets · All publication dates · Unknown dates included"
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
        Output("shared-filters", "style"),
        Output("collection-layout", "style"),
        Input("active-dataset", "value"),
    )
    def change_collection(dataset):
        missing = (
            dataset == "social"
            and service.health().get("record_counts", {}).get("social", 0) == 0
        )
        return (
            {} if dataset == "native" else {"display": "none"},
            {} if dataset == "social" else {"display": "none"},
            {} if dataset == "native" else {"display": "none"},
            {} if dataset == "social" and not missing else {"display": "none"},
            {"display": "none"} if missing else {},
            {"gridTemplateColumns": "minmax(0, 1fr)"} if missing else {},
        )

    def register_collection(dataset):
        @app.callback(
            *[Output(f"{dataset}-{name}", "value") for name in FILTER_NAMES],
            Output(f"{dataset}-dates", "start_date"), Output(f"{dataset}-dates", "end_date"),
            Output(f"{dataset}-unknown-dates", "value"),
            Input(f"{dataset}-clear-filters", "n_clicks"), prevent_initial_call=True,
        )
        def clear_filters(clicks):
            if not clicks:
                raise PreventUpdate
            return [], [], [], [], [], None, None, ["include"]

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
            *_filter_inputs(dataset),
            Input(f"{dataset}-metric", "value"),
            Input(f"{dataset}-page-prev", "n_clicks"),
            Input(f"{dataset}-page-next", "n_clicks"),
            Input(f"{dataset}-page-size", "value"),
            Input(f"{dataset}-sort", "value"),
            State(f"{dataset}-page-offset", "data"),
            State(f"{dataset}-network-data", "data"),
        )
        def update_collection(*values):
            try:
                filters = _filters(dataset, *values[:8])
                size = values[11] if values[11] in (20, 50, 100) else 20
                offset = max(0, int(values[13] or 0))
                if ctx.triggered_id == f"{dataset}-page-next":
                    offset += size
                elif ctx.triggered_id == f"{dataset}-page-prev":
                    offset = max(0, offset - size)
                else:
                    offset = 0
                sort_value = values[12] if values[12] in ("date:desc", "date:asc", "title:asc", "sponsor:asc") else "date:desc"
                sort_by, direction = sort_value.split(":")
                dashboard = service.dashboard(filters, offset=offset, limit=size, sort_by=sort_by, descending=direction == "desc")
                stats, page = dashboard["stats"], dashboard["page"]
                network_payload = {"relationships": stats.get("relationships", []), "filters": filters.model_dump(mode="json")}
                source_rows = page["rows"]
                offset = page["offset"]
                rows = _public_rows(source_rows, enabled)
                for row in rows:
                    row["archive_status"] = (
                        "Online archive"
                        if row.get("archive_url")
                        else "No verified archived copy"
                    )
                if record_details is not None:
                    summaries = record_details.summaries(source_rows)
                    for row in rows:
                        row.update(summaries.get(row["record_id"], {}))
                matrix = dashboard["matrix"]
                labels = dashboard["labels"]
                health = service.health()
                empty_social = (
                    dataset == "social"
                    and health.get("record_counts", {}).get("social", 0) == 0
                )
                notice = (
                    _notice(
                        "Social advertising is not connected",
                        "This collection will become available when the project's social advertising dataset is added.",
                    )
                    if empty_social
                    else None
                )
                if not stats["total"] and not empty_social:
                    notice = _notice(
                        "No matching records",
                        "Try widening the dates or clearing a filter.",
                    )
                metric = "percent" if values[8] == "percent" else "count"
                return (
                    rows,
                    _summary(stats),
                    _bars(
                        stats.get("publishers" if dataset == "native" else "platforms"),
                        metric,
                    ),
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
                    f"{stats['total']:,} eligible records in the current selection. Charts and downloads use the full selection.",
                    _label_chart(distribution=labels),
                    f"Historical automated labels; a record can have several. {labels['unlabeled_records']:,} of {labels['total']:,} selected records have no historical label. These are not verified themes.",
                    matrix["table_rows"],
                    matrix["table_columns"],
                    {"display": "none"} if empty_social else {},
                    {
                        "height": f"{max(380, 38 * len(matrix['sponsors']) + 150)}px",
                        "width": "100%",
                    },
                    {
                        "height": f"{max(340, 38 * len(labels['items']) + 70)}px",
                        "width": "100%",
                    },
                    offset == 0,
                    offset + size >= page["total"],
                    f"{offset + 1 if rows else 0:,}–{offset + len(rows):,} of {page['total']:,}",
                    offset,
                    no_update if network_payload == values[14] else network_payload,
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
            State("page-location", "pathname"),
            prevent_initial_call=True,
        )
        def export_collection(clicks, *values):
            if not clicks or _page(values[-1]) != "data":
                raise PreventUpdate
            values = values[:-1]
            try:
                rows = _public_rows(service.browse(_filters(dataset, *values)), enabled)
                fields = list(
                    NATIVE_COLUMNS if dataset == "native" else SOCIAL_COLUMNS
                ) + ["record_id", "version_id", "labels", "retrievable"]
                if enabled:
                    fields.append("archive_url")
                output = StringIO(newline="")
                writer = csv.DictWriter(
                    output, fieldnames=fields, extrasaction="ignore"
                )
                writer.writeheader()
                for row in rows:
                    row = dict(row, labels="; ".join(row["labels"]))
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
                    "filename": f"{dataset}-advertising.csv",
                    "type": "text/csv",
                }, f"Downloaded {len(rows):,} selected records."
            except Exception:  # noqa: BLE001 - public boundary must hide unexpected service details.
                return (
                    None,
                    "Download is unavailable. Your current filters are preserved.",
                )

    register_collection("native")
    register_collection("social")

    from observatory.knowledge_ui import register_knowledge_graph

    register_knowledge_graph(app, service, enabled, record_details=record_details, require_open=True)
    from observatory.collection_graph_ui import register_collection_graph

    register_collection_graph(app, service, enabled)
    from observatory.data_ui import register_data_views

    register_data_views(app, service, enabled)

    from observatory.research_explorer import register_research_explorer

    register_research_explorer(app, service, enabled)
    from observatory.historical_theme_ui import register_historical_themes

    register_historical_themes(app, service, enabled)
    from observatory.claims_ui import register_claims_browser

    register_claims_browser(app, service, enabled)

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
        Output("research-results", "children"),
        Output("research-submission", "data"),
        Input("search-free", "n_clicks"),
        Input("answer-paid", "n_clicks"),
        State("research-question", "value"),
        State("search-scope", "value"),
        State("active-dataset", "value"),
        *_filter_inputs("native", State),
        *_filter_inputs("social", State),
        State("page-location", "pathname"),
        prevent_initial_call=True,
        running=[
            (Output("search-free", "disabled"), True, False),
            (Output("answer-paid", "disabled"), True, False),
        ],
    )
    def research(search_clicks, answer_clicks, question, scope, dataset, *values):
        pathname, values = values[-1], values[:-1]
        if pathname is None or _page(pathname) != "query":
            raise PreventUpdate
        if ctx.triggered_id not in {"search-free", "answer-paid"}:
            raise PreventUpdate
        clicks = search_clicks if ctx.triggered_id == "search-free" else answer_clicks
        if not isinstance(clicks, int) or isinstance(clicks, bool) or clicks <= 0:
            raise PreventUpdate
        result = run_research(question, scope, dataset, values)
        try:
            submitted = _research_signature(question, scope, dataset, values)
        except ValueError:
            submitted = None
        return result, submitted

    def run_research(question, scope, dataset, values):
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
                    dataset, *(values[:8] if dataset == "native" else values[8:])
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
                report = (
                    service.search_report(question, filters, limit=5)
                    if hasattr(service, "search_report")
                    else {
                        "evidence": service.search(question, filters, limit=5),
                        "diagnostics": {},
                    }
                )
                evidence = report["evidence"]
                diagnostics = _coverage_notice(report.get("diagnostics", {}))
                return (
                    [
                        context,
                        _notice(
                            "Keyword search",
                            f"{len(evidence)} evidence passages found. No paid model call was made.",
                        ),
                        diagnostics,
                        *_evidence_cards(evidence, enabled),
                    ]
                    if evidence
                    else [
                        context,
                        _notice(
                            "No matching evidence",
                            "Try a different term or widen the search scope.",
                        ),
                        diagnostics,
                    ]
                )
            except Exception:  # noqa: BLE001 - public boundary must hide unexpected service details.
                return [
                    context,
                    _notice(
                        "Search temporarily unavailable",
                        "The data service could not complete this search. Please try again later.",
                        "warning",
                    ),
                ]
        try:
            if "visitor_id" not in session:
                session["visitor_id"] = secrets.token_urlsafe(24)
            result = _mapping(
                service.answer(question, filters, visitor=session["visitor_id"])
            )
            if result.get("answer_mode") == "statistics" and result.get("structured_result"):
                effective = Filters.model_validate(result["structured_result"]["filters"])
                return [_search_context(question, effective), _statistics_card(result, enabled)]
            if result.get("answer_mode") == "tools" and result.get("structured_result"):
                effective = Filters.model_validate(result["structured_result"].get("filters") or filters.model_dump())
                return [_search_context(question, effective), _tools_card(result, enabled)]
            status = result.get("status", "service_unavailable")
            labels = {
                "answered": "Answer with supporting evidence",
                "insufficient_evidence": "Insufficient evidence",
                "service_unavailable": "Answer service unavailable",
                "limited": "Paid answers temporarily limited",
            }
            if status not in labels:
                status = "service_unavailable"
            if status in {"service_unavailable", "limited"}:
                # Never surface provider exception strings, internal locations or configuration.
                message = (
                    "The model request limit or project API budget was reached. Try again later. "
                    "You can continue browsing the collection and using keyword search."
                    if status == "limited" else
                    "You can continue browsing the collection and using keyword search."
                )
            else:
                message = str(
                    result.get("answer")
                    or "The available records do not support an answer."
                )
            return [
                context,
                html.Div(
                    [
                        html.Span("Question needs clarification · model-assisted query"
                                  if result.get("answer_mode") == "clarification" and result.get("research_trace") else
                                  "Question needs clarification · no model charge"
                                  if result.get("answer_mode") == "clarification" else
                                  "Read-only data tools"
                                  if result.get("answer_mode") == "tools" else
                                  "Collection statistics · no model charge"
                                  if result.get("answer_mode") == "statistics" else
                                  "Generated answer", className="eyebrow"),
                        html.H3("Clarify this question" if result.get("answer_mode") == "clarification"
                                else labels.get(status, labels["service_unavailable"])),
                        html.P(message, className="answer-text"),
                        _research_steps(result),
                    ],
                    className="answer-card",
                ),
                *_evidence_cards(
                    result.get("evidence") or [], enabled, result.get("citations") or []
                ),
            ]
        except Exception:  # noqa: BLE001 - public boundary must hide unexpected service details.
            try:
                evidence = service.search(question, filters, limit=5)
            except Exception:  # noqa: BLE001 - public boundary must hide unexpected service details.
                evidence = []
            return [
                context,
                _notice(
                    "Answer service unavailable",
                    "Keyword results are shown when available. You can continue browsing and searching.",
                    "warning",
                ),
                *_evidence_cards(evidence, enabled),
            ]

    return app
