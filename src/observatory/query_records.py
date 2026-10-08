"""Page all records in a submitted statistics answer, without another model call."""

import re

from dash import Input, Output, State, ctx, dcc, html
from dash.exceptions import PreventUpdate
from flask import current_app
from itsdangerous import BadData, URLSafeSerializer

from .models import Filters
from .network_ui import record_cards
from .structured_queries import validate_share_scope

PAGE_SIZE = 10
NAMES = {"native": "native ad records", "social": "company social posts"}


def _serializer():
    return URLSafeSerializer(current_app.secret_key, salt="query-matching-records-v1")


def _valid_version(value):
    return isinstance(value, str) and bool(value) and value != "unavailable"


def _historical_guard(filters, supplied):
    """Keep a source-state guard inside the signed submitted selection."""
    if supplied is None:
        if filters.dataset == "social" and filters.labels:
            raise ValueError("Historical source-state version is missing")
        return None
    if not isinstance(supplied, dict) or set(supplied) != {"filters", "source_state_version"}:
        raise ValueError("Invalid historical source-state guard")
    scope = Filters.model_validate(supplied["filters"])
    version = supplied["source_state_version"]
    if scope.dataset != "social" or not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
        raise ValueError("Invalid historical source-state scope or version")
    validate_share_scope(filters, scope)
    return {"filters": scope.model_dump(mode="json"), "source_state_version": version}


def _historical_current(service, guard):
    return (guard is None or service.db.social_source_state_version(
        Filters.model_validate(guard["filters"])) == guard["source_state_version"])


def _notice(message):
    return html.P(message, className="scope-note", role="status")


def _controls(token=None, options=None, dataset=None, children=None, total=0, shown=0):
    """Also supplies dynamic callback components to Dash's validation layout."""
    return html.Details([
        html.Summary(f"Inspect matching records · showing {shown:,} of {total:,}", id="query-records-summary"),
        dcc.Store(id="query-records-scope", data=token),
        dcc.Store(id="query-records-offset", data=0),
        html.Div([
            html.Label("Collection", htmlFor="query-records-collection"),
            dcc.Dropdown(id="query-records-collection", options=options or [], value=dataset,
                         clearable=False, searchable=False),
        ], style={} if len(options or []) > 1 else {"display": "none"}),
        html.Div(children or [], id="query-records-content", **{"aria-live": "polite"}),
        html.Div([
            html.Button("Previous records", id="query-records-prev", n_clicks=0, disabled=True,
                        className="button button-quiet"),
            html.Button("Next records", id="query-records-next", n_clicks=0, disabled=total <= PAGE_SIZE,
                        className="button button-quiet"),
            html.Button("First page", id="query-records-reset", n_clicks=0, disabled=True,
                        className="button button-quiet"),
        ], className="pager"),
        _notice("These records use the submitted selection. Submit a new question to change it."),
    ], className="statistics-records")


def validation_panel():
    return _controls()


def statistics_records_panel(data, service, links_enabled):
    """Sign a server-returned scope, including subsets not editable in the UI."""
    try:
        filters = Filters.model_validate(data["filters"])
        if filters.date_from and filters.date_to and filters.date_from > filters.date_to:
            raise ValueError("invalid dates")
        health = service.health()
        if health.get("status") != "ok" or not _valid_version(health.get("data_version")):
            return _notice("Record browsing is temporarily unavailable. Submit the question again when the collection is available.")
        version = data.get("data_version")
        if not _valid_version(version):
            return _notice("The saved record selection cannot be verified. Submit the question again before browsing matching records.")
        if health["data_version"] != version:
            return _notice("The collection changed since this answer. Submit the question again to inspect matching records.")
        guard = _historical_guard(filters, data.get("historical_source_state_guard"))
        if data.get("kind") == "social_historical_labels" and guard is None:
            raise ValueError("Historical distributions require their source-state guard")
        if not _historical_current(service, guard):
            return _notice("Historical source states changed since this answer. Submit the question again to inspect matching records.")
        counts = health.get("countable_record_counts", health.get("record_counts", {}))
        totals = {}
        for item in data["collections"]:
            dataset, total = item["dataset"], item["total"]
            if (dataset not in NAMES or dataset in totals or filters.dataset not in {"all", dataset}
                    or type(total) is not int or total < 0):
                raise ValueError("invalid collection totals")
            # Active but unadmitted records must not acquire a zero-record browser.
            if counts.get(dataset, 0):
                totals[dataset] = total
        if not totals:
            return _notice("No admitted collection is available for record browsing.")
        dataset = next(iter(totals))
        payload = {"schema": 1, "filters": filters.model_dump(mode="json"),
                   "collections": totals, "data_version": version}
        if guard is not None:
            payload["historical_source_state_guard"] = guard
        token = _serializer().dumps(payload)
        options = [{"label": f"{NAMES[name].capitalize()} ({total:,})", "value": name}
                   for name, total in totals.items()]
        rows = [row for row in data.get("records", []) if row.get("dataset") == dataset][
            :min(PAGE_SIZE, totals[dataset])]
        children = [
            _notice(f"{totals[dataset]:,} matching {NAMES[dataset]} · showing {len(rows):,} of {totals[dataset]:,}"),
            *record_cards(rows, links_enabled),
        ]
        if not totals[dataset]:
            children = [_notice("No matching records in the submitted selection.")]
        return _controls(token, options, dataset, children, totals[dataset], len(rows))
    except Exception:
        return _notice("The saved record selection is unavailable. Submit the question again.")


def _unavailable(message, offset=0, retry=False):
    return [_notice(message)], "Matching records · unavailable", offset, True, True, not retry


def register_query_records(app, service, links_enabled):
    @app.callback(
        Output("query-records-content", "children"), Output("query-records-summary", "children"),
        Output("query-records-offset", "data"), Output("query-records-prev", "disabled"),
        Output("query-records-next", "disabled"), Output("query-records-reset", "disabled"),
        Input("query-records-scope", "data"), Input("query-records-collection", "value"),
        Input("query-records-prev", "n_clicks"), Input("query-records-next", "n_clicks"),
        Input("query-records-reset", "n_clicks"), State("query-records-offset", "data"),
        State("page-location", "pathname"),
    )
    def page_records(token, dataset, previous, following, reset, offset, pathname):
        if pathname not in (None, "/", "/query", "/query/"):
            raise PreventUpdate
        trigger = ctx.triggered_id
        clicks = {"query-records-prev": previous, "query-records-next": following,
                  "query-records-reset": reset}
        if trigger in clicks and (type(clicks[trigger]) is not int or clicks[trigger] <= 0):
            raise PreventUpdate
        try:
            if not isinstance(token, str) or not token:
                raise ValueError("missing saved scope")
            payload = _serializer().loads(token)
            if not isinstance(payload, dict) or payload.get("schema") != 1:
                raise ValueError("invalid saved scope")
            filters = Filters.model_validate(payload["filters"])
            totals = payload["collections"]
            version = payload["data_version"]
            guard = _historical_guard(filters, payload.get("historical_source_state_guard"))
            if (not isinstance(totals, dict) or dataset not in NAMES or dataset not in totals
                    or filters.dataset not in {"all", dataset} or not _valid_version(version)
                    or type(totals[dataset]) is not int or totals[dataset] < 0
                    or (filters.date_from and filters.date_to and filters.date_from > filters.date_to)):
                raise ValueError("invalid saved scope")
        except (BadData, KeyError, TypeError, ValueError):
            return _unavailable("The saved record selection is unavailable. Submit the question again.")
        offset = offset if type(offset) is int and offset >= 0 else 0
        if trigger in (None, "query-records-scope", "query-records-collection", "query-records-reset"):
            offset = 0
        elif trigger == "query-records-next":
            offset += PAGE_SIZE
        elif trigger == "query-records-prev":
            offset = max(0, offset - PAGE_SIZE)
        try:
            before = service.health()
            if before.get("status") != "ok":
                return _unavailable("Records are temporarily unavailable. Use First page to try again.", offset, retry=True)
            if before.get("data_version") != version:
                return _unavailable("The collection changed since this answer. Submit the question again to inspect matching records.")
            counts = before.get("countable_record_counts", before.get("record_counts", {}))
            if not counts.get(dataset, 0):
                return _unavailable("This collection has no admitted records. Submit the question again after records are admitted.")
            if not _historical_current(service, guard):
                return _unavailable("Historical source states changed since this answer. Submit the question again to inspect matching records.")
            # Full saved numerator scope, with native/social counting units kept separate.
            page = service.page(filters.model_copy(update={"dataset": dataset}), offset=offset, limit=PAGE_SIZE)
            after = service.health()
            if after.get("status") != "ok":
                return _unavailable("Records are temporarily unavailable. Use First page to try again.", offset, retry=True)
            if after.get("data_version") != version or page.get("total") != totals[dataset]:
                return _unavailable("The collection changed since this answer. Submit the question again to inspect matching records.")
            counts = after.get("countable_record_counts", after.get("record_counts", {}))
            if not counts.get(dataset, 0):
                return _unavailable("This collection has no admitted records. Submit the question again after records are admitted.")
            if not _historical_current(service, guard):
                return _unavailable("Historical source states changed during the read. Submit the question again to inspect matching records.")
            rows, total, offset = page["rows"], page["total"], page["offset"]
            if (not isinstance(rows, list) or type(total) is not int or total < 0
                    or type(offset) is not int or offset < 0 or offset % PAGE_SIZE
                    or offset > max(0, (total - 1) // PAGE_SIZE * PAGE_SIZE)
                    or len(rows) != min(PAGE_SIZE, total - offset)
                    or any(not isinstance(row, dict) or row.get("dataset") != dataset
                           or not isinstance(row.get("record_id"), str) or not row["record_id"] for row in rows)
                    or len({row["record_id"] for row in rows}) != len(rows)):
                raise ValueError("invalid record page")
            start, end = offset + 1 if rows else 0, offset + len(rows)
            summary = f"Inspect matching records · showing {start:,}–{end:,} of {total:,}"
            content = [_notice(f"{total:,} matching {NAMES[dataset]} · showing {start:,}–{end:,} of {total:,}"),
                       *record_cards(rows, links_enabled)]
            if not rows:
                content.append(_notice("No matching records in the submitted selection."))
            return content, summary, offset, offset == 0, offset + PAGE_SIZE >= total, offset == 0
        except Exception:
            return _unavailable("Records are temporarily unavailable. Use First page to try again.", offset, retry=True)
