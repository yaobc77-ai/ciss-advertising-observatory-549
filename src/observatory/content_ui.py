"""Browse reviewed client-question membership separately from RAG candidates."""

from __future__ import annotations

import json
from urllib.parse import quote

from dash import Input, Output, State, ctx, dcc, html, no_update

from .analytics import sponsor_display
from .content_assessment import QUESTION_IDS
from .models import Filters
from .records import _safe_url

PREFIX = "content-reviewed"
PAGE_SIZE = 10
MEANING = "Reviewed content judgments describe the advertisement, not its factual truth or legal status."


def content_summary(result):
    """Keep membership coverage and pagination as distinct display statements."""
    if not isinstance(result, dict) or result.get("status") != "ok" or not result.get("available"):
        return "Reviewed content decisions are awaiting publication. Missing results are not negative findings."
    totals, coverage = result["totals"], result["coverage"]
    fields = ("scope_records", "relevant", "not_relevant", "unknown")
    if any(type(totals.get(key)) is not int or totals[key] < 0 for key in fields):
        raise ValueError("Invalid reviewed membership totals")
    if sum(totals[key] for key in fields[1:]) != totals["scope_records"]:
        raise ValueError("Membership totals must retain the complete current denominator")
    if type(coverage.get("classification_complete")) is not bool:
        raise ValueError("Reviewed coverage must be explicit")
    if coverage["classification_complete"] and totals["unknown"]:
        raise ValueError("Unknown members prevent a complete matching list")
    text = (f"{totals['relevant']:,} reviewed matches · {totals['not_relevant']:,} reviewed nonmatches · "
            f"{totals['unknown']:,} unknown · {totals['scope_records']:,} current records. ")
    text += ("Complete classification for this question and selection. " if coverage["classification_complete"]
             else "Partial reviewed list; additional matches may be among unknown records. ")
    text += ("All reviewed matches are shown on this page." if result.get("page_complete")
             else f"Showing {len(result.get('records', [])):,} reviewed matches on this page.")
    return text


def content_record_cards(result, links_enabled):
    """Render only the public source projection supplied by the checked reader."""
    rendered = []
    for row in result.get("records", []):
        identifier = row["record_id"]
        href = "/records/" + quote(identifier, safe="")
        evidence = []
        for role, label in (("support", "Supporting text"), ("limiting", "Qualifications or limitations"),
                            ("consensus", "Separate climate-consensus evidence")):
            for item in row.get(f"{role}_evidence", []):
                evidence.append(html.Div([
                    html.Strong(label), html.Blockquote(item["quote"], style={"whiteSpace": "pre-wrap"}),
                    html.Details([html.Summary("Source location"),
                                  html.P(f"Characters [{item['start']}, {item['end']}) · article version {row['version_id']}")]),
                ]))
        metadata = [row.get("publisher"), sponsor_display(row.get("sponsor")),
                    row.get("date") or "Publication date unknown"]
        details = [html.P(f"Speaker: {row.get('speaker') or 'Unspecified'} · "
                          f"Treatment: {row.get('treatment') or 'Unspecified'} · "
                          f"Status of action: {row.get('temporal_status') or 'Unspecified'}", className="scope-note")]
        if row.get("consensus_position") and row["consensus_position"] != "not_applicable":
            details.append(html.P("Separate consensus judgment: " + row["consensus_position"], className="scope-note"))
        if row.get("rationale"):
            details.append(html.P(row["rationale"]))
        links = [dcc.Link("Read current article and materials", href=href)]
        url = _safe_url(row.get("url")) if links_enabled else ""
        if url:
            links.append(html.A("Original source ↗", href=url, target="_blank", rel="noopener noreferrer"))
        rendered.append(html.Article([
            html.H3(dcc.Link(row.get("title") or "Untitled record", href=href)),
            html.P(" · ".join(str(item) for item in metadata if item), className="scope-note"),
            *details, *evidence, html.Div(links, className="source-links"),
        ], className="network-record"))
    return rendered


def content_panel():
    # Held-out test wording is never loaded for the public panel.
    options = []
    return html.Section([
        html.H2("Reviewed content assignments"),
        html.P("Customer validation questions are held out for evaluation. Published CLAIMS assignments remain available separately. " + MEANING,
               className="scope-note"),
        html.Label("Content question", htmlFor=f"{PREFIX}-question"),
        dcc.Dropdown(id=f"{PREFIX}-question", options=options, value=None, clearable=False, disabled=True),
        html.P(id=f"{PREFIX}-status", className="scope-note", role="status"),
        dcc.Loading(html.Div(id=f"{PREFIX}-results", **{"aria-live": "polite"}), type="circle"),
        html.Div([
            html.Button("Previous matches", id=f"{PREFIX}-prev", n_clicks=0, disabled=True,
                        className="button button-quiet"),
            html.Button("Next matches", id=f"{PREFIX}-next", n_clicks=0, disabled=True,
                        className="button button-quiet"),
            html.Button("Download this page JSON ↓", id=f"{PREFIX}-export", n_clicks=0,
                        className="button button-quiet"),
        ], className="pager"),
        dcc.Store(id=f"{PREFIX}-page", data={"offset": 0}),
        dcc.Download(id=f"{PREFIX}-download"),
        html.P(id=f"{PREFIX}-export-status", className="scope-note", role="status"),
    ], className="records-panel", style={"display": "none"}, **{"aria-hidden": "true"})


def read_content_page(service, filters, question_id, state=None, direction=None):
    """Read a fresh page; never trust cached records or counts from the browser."""
    if filters.dataset != "native" or question_id not in QUESTION_IDS:
        raise ValueError("Select a native client content question")
    state = state if isinstance(state, dict) else {}
    offset = state.get("offset", 0)
    if type(offset) is not int or not 0 <= offset <= 10000 or offset % PAGE_SIZE:
        raise ValueError("Invalid reviewed content page")
    signature = {"question_id": question_id, "filters": filters.model_dump(mode="json")}
    if direction:
        if direction not in {"previous", "next", "current"}:
            raise ValueError("Invalid reviewed page direction")
        if state.get("selection") != signature or not state.get("publication_version"):
            raise ValueError("The selection changed; read the first page again")
        if direction != "current":
            offset = max(0, offset + (PAGE_SIZE if direction == "next" else -PAGE_SIZE))
    else:
        offset = 0
    result = service.content_matches(filters, question_id=question_id, offset=offset, limit=PAGE_SIZE)
    if direction and result.get("publication_version") != state.get("publication_version"):
        raise ValueError("The content publication changed; read the first page again")
    if direction and result.get("source_scope_sha256") != state.get("source_scope_sha256"):
        raise ValueError("The source selection changed; read the first page again")
    content_summary(result)
    return result, {"selection": signature, "offset": offset,
                    "publication_version": result.get("publication_version"),
                    "source_scope_sha256": result.get("source_scope_sha256")}


def register_content_browser(app, service, links_enabled):
    @app.callback(
        Output(f"{PREFIX}-results", "children"), Output(f"{PREFIX}-status", "children"),
        Output(f"{PREFIX}-page", "data"), Output(f"{PREFIX}-prev", "disabled"),
        Output(f"{PREFIX}-next", "disabled"),
        Input("native-network-data", "data"), Input("page-location", "pathname"),
        Input("native-view", "value"), Input("active-dataset", "value"),
        Input(f"{PREFIX}-question", "value"), Input(f"{PREFIX}-prev", "n_clicks"),
        Input(f"{PREFIX}-next", "n_clicks"), State(f"{PREFIX}-page", "data"),
    )
    def browse(snapshot, pathname, view, dataset, question, previous, following, state):
        if pathname not in {"/data", "/data/"} or view != "overview" or dataset != "native":
            return [], "", {"offset": 0}, True, True
        try:
            filters = Filters.model_validate(snapshot["filters"])
            direction = {f"{PREFIX}-prev": "previous", f"{PREFIX}-next": "next"}.get(ctx.triggered_id)
            result, state = read_content_page(service, filters, question, state, direction)
            summary = content_summary(result)
            if not result.get("available"):
                return [], summary, {"offset": 0}, True, True
            notes = [html.P(result["question"]["question_exact"])]
            definition = result["question"].get("definition")
            if definition:
                notes.append(html.Details([html.Summary("Reviewed question definition"), html.P(definition)]))
            notes += content_record_cards(result, links_enabled)
            if not result["records"]:
                notes.append(html.P("No reviewed matches are currently shown; use the coverage above to distinguish absence from unknown records."))
            return notes, summary, state, state["offset"] == 0, result.get("next_offset") is None
        except Exception:
            return [], "This reviewed selection is unavailable or changed. Choose a question again or refresh the collection.", {"offset": 0}, True, True

    @app.callback(
        Output(f"{PREFIX}-download", "data"), Output(f"{PREFIX}-export-status", "children"),
        Input(f"{PREFIX}-export", "n_clicks"), Input("native-network-data", "data"),
        Input(f"{PREFIX}-question", "value"), State("page-location", "pathname"),
        State("native-view", "value"), State("active-dataset", "value"),
        State(f"{PREFIX}-page", "data"), prevent_initial_call=True,
    )
    def export(clicks, snapshot, question, pathname, view, dataset, state):
        if ctx.triggered_id != f"{PREFIX}-export":
            return no_update, ""
        if pathname not in {"/data", "/data/"} or view != "overview" or dataset != "native":
            return no_update, "Open the native Overview to export reviewed content."
        try:
            filters = Filters.model_validate(snapshot["filters"])
            # Re-read the displayed page and validate the same publication/scope.
            result, _ = read_content_page(service, filters, question, state, "current")
            if not result.get("available"):
                raise ValueError("No published content")
            return dcc.send_string(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                                   f"reviewed-{question}-page.json"), (
                "Downloaded this page of reviewed matches and whole-selection coverage. "
                "This file contains a complete list only when classification_complete and page_complete are both true."
            )
        except Exception:
            return no_update, "This reviewed page changed or is unavailable. Refresh the question before exporting."
