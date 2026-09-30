"""Read-only CLAIMS2 browsing, separate from historical sentence labels."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import quote

from dash import Input, Output, State, ctx, dcc, html, no_update

from .analytics import sponsor_display
from .models import Filters
from .records import _safe_url

PREFIX = "claims2"
PAGE_SIZE = 10
COVERAGE = "Only published matches are shown. Unmatched records are not classified negatives."
MEANING = "Taxonomy assignments describe the quoted text; they are not independent fact checks or greenwashing verdicts."
REVIEW_LABELS = {
    "automatic_unverified": "Automated assignment · meaning not human-reviewed",
    "human_supported": "Human-reviewed assignment",
}
_CATEGORY = re.compile(r"(NC|SC)_[1-9][0-9]*")


def public_claims(result, *, record=None):
    """Project display fields and reject a stale record/evidence read as a whole.

    The service owns publication eligibility. The detail page additionally binds
    this later read to the exact body that it is about to show to the user.
    """
    if not isinstance(result, dict) or not result.get("available"):
        return {"state": "pending", "records": [], "total_records": 0, "total_matches": 0,
                "note": "CLAIMS2 results are awaiting publication. " + COVERAGE}
    totals = [result.get("total_records"), result.get("total_matches")]
    rows = result.get("records")
    if any(type(value) is not int or value < 0 for value in totals) or not isinstance(rows, list):
        raise ValueError("Invalid CLAIMS2 result")
    scope = (result.get("coverage_summary") or {}).get("scope_records")
    if scope is not None and (type(scope) is not int or scope < totals[0]):
        raise ValueError("Invalid CLAIMS2 coverage denominator")
    projected, seen = [], set()
    for row in rows:
        identifier = row.get("record_id") if isinstance(row, dict) else None
        claims = row.get("claims") if isinstance(row, dict) else None
        if not isinstance(identifier, str) or not identifier or identifier in seen or not isinstance(claims, list) or not claims:
            raise ValueError("Invalid CLAIMS2 record")
        seen.add(identifier)
        if record is not None and identifier != record["record_id"]:
            raise ValueError("CLAIMS2 result belongs to another record")
        displayed = []
        for item in claims:
            if not isinstance(item, dict) or item.get("record_id") != identifier:
                raise ValueError("Invalid CLAIMS2 evidence ownership")
            nc, sc = item.get("nc_id"), item.get("sc_id")
            if (not isinstance(nc, str) or not re.fullmatch(r"NC_[1-9][0-9]*", nc)
                    or (sc is not None and (not isinstance(sc, str) or not re.fullmatch(r"SC_[1-9][0-9]*", sc)))):
                raise ValueError("Invalid CLAIMS2 category")
            review = item.get("review_state")
            start, end, text = item.get("start"), item.get("end"), item.get("quote")
            if (review not in REVIEW_LABELS or type(start) is not int or type(end) is not int
                    or not 0 <= start < end or not isinstance(text, str) or not text):
                raise ValueError("Invalid CLAIMS2 evidence")
            if record is not None and (
                item.get("version_id") != record.get("version_id")
                or item.get("body_hash") != record.get("body_hash")
                or record.get("body", "")[start:end] != text
                or item.get("dataset") != record.get("dataset")
            ):
                raise ValueError("CLAIMS2 evidence changed during the record read")
            fields = ("version_id", "body_hash", "run_id", "taxonomy_version", "review_version")
            if any(not isinstance(item.get(key), str) or not item[key] for key in fields):
                raise ValueError("Incomplete CLAIMS2 provenance")
            if any(item.get(key) is not None and not isinstance(item[key], str)
                   for key in ("nc_definition", "sc_definition")):
                raise ValueError("Invalid CLAIMS2 definition")
            displayed.append({
                **{key: item[key] for key in fields},
                "nc_id": nc, "sc_id": sc,
                "nc_definition": str(item.get("nc_definition") or "Definition unavailable in this published bundle."),
                "sc_definition": str(item.get("sc_definition") or "") if sc else "",
                "review_state": review, "review_label": REVIEW_LABELS[review],
                "quote": text, "start": start, "end": end,
            })
        metadata = claims[0]
        projected.append({
            "record_id": identifier, "record_url": "/records/" + quote(identifier, safe=""),
            "title": str(row.get("title") or metadata.get("title") or "Untitled record"),
            "publisher": str(row.get("publisher") or metadata.get("publisher") or "Unknown outlet"),
            "sponsor": sponsor_display(row.get("sponsor") or metadata.get("sponsor")),
            "date": str(row.get("date") or metadata.get("date") or "Publication date unknown"),
            "url": _safe_url(row.get("url") or metadata.get("url")),
            "claims": displayed,
        })
    if len(projected) > totals[0] or sum(len(row["claims"]) for row in projected) > totals[1]:
        raise ValueError("CLAIMS2 totals do not cover the returned page")
    categories = []
    for item in result.get("category_counts", []):
        if (not isinstance(item, dict) or not isinstance(item.get("nc_id"), str)
                or not re.fullmatch(r"NC_[1-9][0-9]*", item["nc_id"])
                or (item.get("sc_id") is not None and (not isinstance(item["sc_id"], str)
                    or not re.fullmatch(r"SC_[1-9][0-9]*", item["sc_id"])))):
            raise ValueError("Invalid CLAIMS2 category counts")
        counts = [item.get("record_count"), item.get("assignment_count")]
        if any(type(value) is not int or not 0 <= value <= bound for value, bound in zip(counts, totals, strict=True)):
            raise ValueError("Invalid CLAIMS2 category counts")
        strings = ("taxonomy_version", "nc_definition", "sc_definition")
        if any(item.get(key) is not None and not isinstance(item[key], str) for key in strings):
            raise ValueError("Invalid CLAIMS2 category definitions")
        categories.append({key: item.get(key) for key in ("nc_id", "sc_id", *strings, "record_count", "assignment_count")})
    note = MEANING + " " + COVERAGE
    if scope is not None:
        note += f" {scope:,} eligible records are in scope; classification completion is unknown."
    return {
        "state": "ready" if totals[0] else "empty", "records": projected,
        "total_records": totals[0], "total_matches": totals[1],
        "claims_version": result.get("claims_version"),
        "note": note, "category_counts": categories,
        "coverage_summary": {"scope_records": scope, "published_match_records": totals[0],
                             "classification_completion_known": False},
    }


def claim_card(item):
    definitions = [html.Dt(item["nc_id"]), html.Dd(item["nc_definition"])]
    if item["sc_id"]:
        definitions += [html.Dt(item["sc_id"]), html.Dd(item["sc_definition"] or "Definition unavailable in this published bundle.")]
    else:
        definitions += [html.Dt("Superclaim mapping"), html.Dd("No superclaim is mapped in this published taxonomy.")]
    return html.Article([
        html.P(item["review_label"], className="scope-note"),
        html.Dl(definitions),
        html.Blockquote(item["quote"], style={"whiteSpace": "pre-wrap"}),
        html.Details([
            html.Summary("Assignment provenance"),
            html.Div([
                html.Span(f"Run {item['run_id']}"), html.Span(f"Taxonomy {item['taxonomy_version']}"),
                html.Span(f"Review {item['review_version']}"), html.Span(f"Article version {item['version_id']}"),
                html.Span(f"Body SHA-256 {item['body_hash']}"),
                html.Span(f"Original character range [{item['start']}, {item['end']})"),
            ], className="record-reference"),
        ]),
    ], className="evidence-card")


def claims_record_cards(rows, links_enabled):
    """Shared display for the browser and model-selected read-only tool route."""
    rendered = []
    for row in rows:
        links = [html.A("Read article and original materials", href=row["record_url"])]
        if links_enabled and row["url"]:
            links.append(html.A("Original source ↗", href=row["url"], target="_blank", rel="noopener noreferrer"))
        rendered.append(html.Article([
            html.H3(html.A(row["title"], href=row["record_url"])),
            html.P(f"{row['publisher']} · {row['sponsor']} · {row['date']}", className="scope-note"),
            *[claim_card(item) for item in row["claims"]],
            html.Div(links, className="source-links"),
        ], className="network-record"))
    return rendered


def claims_panel():
    return html.Section([
        html.H2("Read published CLAIMS2 evidence"),
        html.P(id=f"{PREFIX}-status", className="scope-note", role="status"),
        html.Div([
            html.Div([html.Label("Category ID (optional)", htmlFor=f"{PREFIX}-category"),
                      dcc.Input(id=f"{PREFIX}-category", type="text", value="", maxLength=32,
                                placeholder="For example, NC_1 or SC_1")]),
            html.Div([html.Label("Review state", htmlFor=f"{PREFIX}-review"),
                      dcc.Dropdown(id=f"{PREFIX}-review", value="all", clearable=False, searchable=False,
                                   options=[{"label": "All published assignments", "value": "all"},
                                            {"label": "Human-reviewed assignments", "value": "human_supported"},
                                            {"label": "Automated assignments", "value": "automatic_unverified"}])]),
            html.Button("Find assignments", id=f"{PREFIX}-apply", n_clicks=0,
                        className="button button-quiet"),
            html.Button("Clear", id=f"{PREFIX}-reset", n_clicks=0, className="button button-quiet"),
            html.Button("Download matching page JSON ↓", id=f"{PREFIX}-export", n_clicks=0,
                        className="button button-quiet"),
        ], id=f"{PREFIX}-controls", className="record-page-controls", style={"display": "none"}),
        html.Div(id=f"{PREFIX}-results", **{"aria-live": "polite"}),
        html.Div([
            html.Button("Previous matching records", id=f"{PREFIX}-prev", n_clicks=0, disabled=True,
                        className="button button-quiet"),
            html.Button("Next matching records", id=f"{PREFIX}-next", n_clicks=0, disabled=True,
                        className="button button-quiet"),
        ], id=f"{PREFIX}-pager", className="pager", style={"display": "none"}),
        dcc.Store(id=f"{PREFIX}-selection"), dcc.Store(id=f"{PREFIX}-offset", data=0),
        dcc.Download(id=f"{PREFIX}-download"),
        html.P(id=f"{PREFIX}-export-status", className="scope-note", role="status"),
    ], className="records-panel claims2-panel")


def _query(category, review):
    if not isinstance(category, str):
        raise ValueError("Use a published category ID such as NC_1 or SC_1.")
    category = category.strip()
    if category and not _CATEGORY.fullmatch(category):
        raise ValueError("Use a published category ID such as NC_1 or SC_1.")
    if review not in {"all", *REVIEW_LABELS}:
        raise ValueError("Select a supported review state.")
    return {"category": category, "review": review}, {
        "nc_ids": [category] if category.startswith("NC_") else None,
        "sc_ids": [category] if category.startswith("SC_") else None,
        "review_state": None if review == "all" else review,
    }


def register_claims_browser(app, service, links_enabled):
    def response(note="", *, controls=False, rows=None, selection=None, offset=0, previous=True, following=True):
        return (rows or [], note, {} if controls else {"display": "none"}, offset,
                previous, following, selection, (selection or {}).get("category", ""),
                (selection or {}).get("review", "all"), {} if controls and rows else {"display": "none"})

    @app.callback(
        Output(f"{PREFIX}-results", "children"), Output(f"{PREFIX}-status", "children"),
        Output(f"{PREFIX}-controls", "style"), Output(f"{PREFIX}-offset", "data"),
        Output(f"{PREFIX}-prev", "disabled"), Output(f"{PREFIX}-next", "disabled"),
        Output(f"{PREFIX}-selection", "data"), Output(f"{PREFIX}-category", "value"),
        Output(f"{PREFIX}-review", "value"), Output(f"{PREFIX}-pager", "style"),
        Input("native-network-data", "data"), Input("page-location", "pathname"),
        Input("native-view", "value"), Input("active-dataset", "value"),
        Input(f"{PREFIX}-apply", "n_clicks"), Input(f"{PREFIX}-reset", "n_clicks"),
        Input(f"{PREFIX}-prev", "n_clicks"), Input(f"{PREFIX}-next", "n_clicks"),
        State(f"{PREFIX}-category", "value"), State(f"{PREFIX}-review", "value"),
        State(f"{PREFIX}-selection", "data"), State(f"{PREFIX}-offset", "data"),
    )
    def browse(snapshot, pathname, view, dataset, apply, reset, previous, following, category, review, selection, offset):
        if pathname not in {"/data", "/data/"} or view != "overview" or dataset != "native":
            return response()
        if not hasattr(service, "claims_matches"):
            return response("CLAIMS2 results are awaiting publication. " + COVERAGE)
        try:
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("filters"), dict):
                raise ValueError("Missing collection selection")
            filters = Filters.model_validate(snapshot["filters"])
            if filters.dataset != "native":
                raise ValueError("Wrong collection")
            trigger = ctx.triggered_id
            if trigger == f"{PREFIX}-apply":
                selection, options = _query(category, review)
                offset = 0
            elif trigger in {f"{PREFIX}-prev", f"{PREFIX}-next"}:
                if not isinstance(selection, dict) or set(selection) != {"category", "review"}:
                    raise ValueError("Missing assignment selection")
                selection, options = _query(selection["category"], selection["review"])
                offset = max(0, offset) // PAGE_SIZE * PAGE_SIZE if type(offset) is int else 0
                offset = max(0, offset - PAGE_SIZE) if trigger == f"{PREFIX}-prev" else offset + PAGE_SIZE
            else:
                selection, options = _query("", "all")
                offset = 0
            result = public_claims(service.claims_matches(filters, offset=offset, limit=PAGE_SIZE, **options))
            if result["state"] == "pending":
                return response(result["note"])
            total = result["total_records"]
            # Clamp stale/forged pagination by rereading the last real page.
            if total and offset >= total:
                offset = (total - 1) // PAGE_SIZE * PAGE_SIZE
                result = public_claims(service.claims_matches(filters, offset=offset, limit=PAGE_SIZE, **options))
                if result["state"] != "ready" or result["total_records"] != total:
                    raise ValueError("Assignment selection changed during the read")
            if not total:
                filtered = bool(selection["category"] or selection["review"] != "all")
                return response("No published CLAIMS2 assignments match the current selection. " + result["note"],
                                controls=filtered, selection=selection)
            rendered = claims_record_cards(result["records"], links_enabled)
            note = (f"{total:,} matching records · {result['total_matches']:,} assignments · showing "
                    f"{offset + 1:,}–{offset + len(result['records']):,}. " + result["note"])
            return response(note, controls=True, rows=rendered, selection=selection, offset=offset,
                            previous=offset == 0, following=offset + PAGE_SIZE >= total)
        except (TypeError, ValueError):
            return response("This CLAIMS2 selection is unavailable. Use a category ID such as NC_1 or SC_1, or refresh the collection filters.",
                            controls=True)
        except Exception:
            return response("CLAIMS2 assignments are temporarily unavailable. The source records remain available.")

    @app.callback(
        Output(f"{PREFIX}-download", "data"), Output(f"{PREFIX}-export-status", "children"),
        Input(f"{PREFIX}-export", "n_clicks"), Input("native-network-data", "data"),
        State("page-location", "pathname"), State("native-view", "value"), State("active-dataset", "value"),
        State(f"{PREFIX}-selection", "data"), State(f"{PREFIX}-offset", "data"),
        prevent_initial_call=True,
    )
    def export(clicks, snapshot, pathname, view, dataset, selection, offset):
        if ctx.triggered_id != f"{PREFIX}-export":
            return no_update, ""
        if pathname not in {"/data", "/data/"} or view != "overview" or dataset != "native":
            return no_update, "Open the native advertising Overview to export a matching page."
        try:
            filters = Filters.model_validate(snapshot["filters"])
            if filters.dataset != "native" or not isinstance(selection, dict) or set(selection) != {"category", "review"}:
                raise ValueError("Invalid selection")
            payload = claims_page_export(service, filters, selection, offset, links_enabled=links_enabled)
            return dcc.send_string(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                                   "claims2-matching-page.json"), (
                f"Downloaded {payload['page']['returned_records']:,} matching records from this page. "
                "Filtered totals describe the whole selection; the file contains this page of evidence only."
            )
        except Exception:
            return no_update, "This matching page is unavailable for export. Refresh the collection filters and try again."


def claims_page_export(service, filters, selection, offset, *, links_enabled):
    """Re-read a bounded public page; never export a client-supplied result cache."""
    selection, options = _query(selection["category"], selection["review"])
    offset = max(0, offset) // PAGE_SIZE * PAGE_SIZE if type(offset) is int else 0
    result = public_claims(service.claims_matches(filters, offset=offset, limit=PAGE_SIZE, **options))
    if result["state"] == "pending":
        raise ValueError("No published results available")
    if result["total_records"] and offset >= result["total_records"]:
        offset = (result["total_records"] - 1) // PAGE_SIZE * PAGE_SIZE
        result = public_claims(service.claims_matches(filters, offset=offset, limit=PAGE_SIZE, **options))
        if result["state"] != "ready":
            raise ValueError("Publication changed during the export")
    for row in result["records"]:
        if not links_enabled:
            row["url"] = ""
    return {
        "schema_version": 1, "kind": "claims2_matching_page",
        "exported_at": datetime.now(UTC).isoformat(), "claims_version": result.get("claims_version"),
        "filters": filters.model_dump(mode="json"), "assignment_filters": selection,
        "page": {"offset": offset, "limit": PAGE_SIZE, "returned_records": len(result["records"]),
                 "returned_assignments": sum(len(row["claims"]) for row in result["records"])},
        "filtered_totals": {"records": result["total_records"], "assignments": result["total_matches"]},
        "coverage_summary": result["coverage_summary"], "category_counts": result["category_counts"],
        "note": "This file contains one page of published evidence, plus totals for the whole filtered selection. " + result["note"],
        "records": result["records"],
    }
