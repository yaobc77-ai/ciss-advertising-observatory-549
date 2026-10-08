"""One workspace for native advertisements and collected company social posts."""

import csv
import hashlib
import json
from collections import Counter
from io import StringIO

import dash_ag_grid as dag
import plotly.graph_objects as go
from dash import Input, Output, State, ctx, dcc, html, no_update
from dash.exceptions import PreventUpdate
from flask import session

from .analytics import csv_safe_cell, sponsor_display

COMPANY_PREFIX = "company:"
FILTER_NAMES = ("publishers", "sponsors", "platforms", "keywords", "labels", "accounts")
FILTER_COUNT = len(FILTER_NAMES) + 3
COLLECTION_NAMES = {"native": "Native advertisements", "social": "Company social posts"}
COLORS = {"native": "#456992", "social": "#168477"}
STATUS_NOTE = (
    "Native advertisement records and collected company social posts are shown together. "
    "A social post is not a verified paid advertisement. Counts and historical labels remain separate by collection."
)


def company_options(facets):
    """Combine identical spelling across casing, without resolving different aliases."""
    groups = {}
    for raw in facets.get("sponsors", []):
        if not isinstance(raw, str):
            raise ValueError("Company facets must contain strings")
        groups.setdefault(raw.strip().casefold(), []).append(raw)
    options = []
    for variants in groups.values():
        variants = sorted(set(variants))
        options.append({
            "label": sponsor_display(variants[0]),
            "value": COMPANY_PREFIX + json.dumps(variants, ensure_ascii=False, separators=(",", ":")),
        })
    duplicates = Counter(option["label"] for option in options)
    for option in options:
        if duplicates[option["label"]] > 1:
            variants = decode_company_selection([option["value"]])
            option["label"] += f" (source: {variants[0]})"
    return sorted(options, key=lambda option: (option["label"].casefold(), option["value"]))


def decode_company_selection(values):
    """Expand a displayed company to its exact source spellings, not guessed aliases."""
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("Company selection must be a list")
    result = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Company values must be strings")
        if value.startswith(COMPANY_PREFIX):
            try:
                variants = json.loads(value[len(COMPANY_PREFIX):])
            except (ValueError, TypeError) as error:
                raise ValueError("Invalid company selection") from error
            if (not isinstance(variants, list) or not variants
                    or any(not isinstance(raw, str) for raw in variants)
                    or len({raw.strip().casefold() for raw in variants}) != 1):
                raise ValueError("A company selection must use identical source spelling")
        else:
            variants = [value]
        for raw in variants:
            if raw not in result:
                result.append(raw)
    return result


def combined_filters(facets):
    """Only shared fields are visible; all IDs exist for the common filter contract."""
    names = {"sponsors": "Sponsor / company affiliation", "keywords": "Collection search term"}
    controls = []
    for name in FILTER_NAMES:
        controls.append(html.Div([
            html.Label(names.get(name, name.title()), htmlFor=f"all-{name}"),
            dcc.Dropdown(
                id=f"all-{name}", value=[], multi=True, placeholder="All",
                options=company_options(facets) if name == "sponsors" else
                [{"label": value, "value": value} for value in facets.get(name, [])] if name == "keywords" else [],
                persistence=name in names, persistence_type="session", className="filter-dropdown",
            ),
        ], className="filter-field", style={} if name in names else {"display": "none"}))
    return html.Aside([
        *controls,
        html.Div([
            html.Label("Publication date"),
            dcc.DatePickerRange(id="all-dates", clearable=True, minimum_nights=0,
                                display_format="MMM D, YYYY", start_date_placeholder_text="Start date",
                                end_date_placeholder_text="End date", persistence=True, persistence_type="session"),
            dcc.Checklist(id="all-unknown-dates", options=[{"label": " Include unknown dates", "value": "include"}],
                          value=["include"], persistence=True, persistence_type="session", className="unknown-toggle"),
        ], className="filter-field date-filter"),
        html.P("Company names combine identical spelling across casing. Different aliases remain separate. "
               "Use a collection view for outlet, platform, account or historical-label filters. "
               "Collection search terms describe collection methods, not verified themes.", className="scope-note"),
        html.Button("Clear filters", id="all-clear-filters", n_clicks=0, className="button button-quiet"),
    ], id="all-filter-panel", className="filters-panel")


def combined_panel(links_enabled, empty_figure):
    columns = [
        {"field": "title", "headerName": "Source record", "cellRenderer": "RecordTitle", "tooltipField": "title",
         "minWidth": 260, "flex": 3, "wrapText": True, "autoHeight": True},
        {"field": "record_type", "headerName": "Record type", "minWidth": 175, "wrapText": True},
        {"field": "sponsor", "headerName": "Sponsor / company affiliation", "minWidth": 180,
         "wrapText": True, "headerTooltip": "Native sponsor or social source company affiliation; a post is not verified paid advertising."},
        {"field": "channel", "headerName": "News outlet / platform", "minWidth": 170, "wrapText": True},
        {"field": "account", "headerName": "Social account", "minWidth": 145},
        {"field": "date", "headerName": "Published", "width": 125},
        {"field": "url", "headerName": "Original", "cellRenderer": "SourceLink",
         "cellRendererParams": {"enabled": links_enabled}, "width": 115},
        {"field": "archive_status", "headerName": "Archive", "cellRenderer": "ArchiveLink",
         "cellRendererParams": {"enabled": links_enabled}, "minWidth": 165},
    ]
    def chart(key, title, note):
        return html.Section([
            html.H3(title), html.P(note, className="chart-note"),
            dcc.Graph(id=f"all-{key}-chart", figure=empty_figure("Reading both collections"),
                      config={"displayModeBar": False, "responsive": True}),
        ], className="chart-card chart-wide")

    overview = html.Div([
        chart("companies", "Source-listed sponsors and company affiliations",
              "Separate counts of native ads and company social posts. Click a company to browse both record types."),
        chart("timeline", "Publication dates across the two collections",
              "Annual counts by record type. Unknown publication dates remain a separate category."),
        html.P("Counts describe this collected corpus, not all advertising by a company. News outlets and social "
               "platforms have different roles; neither shared names nor record counts prove financial ties.", className="scope-note"),
    ], id="all-overview", className="data-view")
    records = html.Section([
        html.Div([html.H2("Browse both collections"),
                  html.Button("Download selected records ↓", id="all-export", n_clicks=0,
                              className="button button-quiet")], className="section-heading"),
        html.P(id="all-record-count", className="scope-note"),
        html.P("Open a title for the stored text and original materials. Social posts use unique platform and post "
               "URLs; source observations remain available in the record details.", className="scope-note"),
        html.Div([
            html.Div([html.Label("Rows per page", htmlFor="all-page-size"),
                      dcc.Dropdown(id="all-page-size", options=[20, 50, 100], value=20,
                                   clearable=False, searchable=False)]),
            html.Div([html.Label("Sort records", htmlFor="all-sort"),
                      dcc.Dropdown(id="all-sort", options=[
                          {"label": "Newest first", "value": "date:desc"},
                          {"label": "Oldest first", "value": "date:asc"},
                          {"label": "Title A–Z", "value": "title:asc"},
                          {"label": "Sponsor / company A–Z", "value": "sponsor:asc"},
                      ], value="date:desc", clearable=False, searchable=False)]),
        ], className="record-page-controls"),
        dag.AgGrid(id="all-grid", columnDefs=columns, rowData=[],
                   defaultColDef={"sortable": False, "resizable": True, "filter": False},
                   dashGridOptions={"rowHeight": 68, "animateRows": False},
                   className="ag-theme-quartz observatory-grid", style={"height": "560px"}),
        html.Div([
            html.Button("Previous", id="all-page-prev", n_clicks=0, disabled=True, className="button button-quiet"),
            html.Span(id="all-page-label", role="status"),
            html.Button("Next", id="all-page-next", n_clicks=0, disabled=True, className="button button-quiet"),
        ], className="pager"),
        dcc.Download(id="all-download"), html.Div(id="all-export-status", role="status"),
    ], id="all-records", className="data-view records-view", hidden=True)
    return html.Section([
        dcc.Store(id="all-page-offset", data=0), dcc.Store(id="all-snapshot"),
        html.Div(id="all-status", className="collection-status", role="status"),
        dcc.Loading(html.Div([
            html.Div(id="all-summary", className="stats-grid"),
            dcc.Tabs(id="all-view", value="overview", mobile_breakpoint=0, className="data-tabs",
                     parent_className="data-tabs-container", children=[
                         dcc.Tab(label=label, value=value, className="data-tab", selected_className="data-tab-selected")
                         for label, value in (("Overview", "overview"), ("Records", "records"))
                     ]),
            overview, records,
        ]), type="circle"),
    ], id="all-panel", style={"display": "none"})


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Invalid collection count")
    return value


def _validated_combined(dashboard):
    """Validate full aggregates; never infer a chart or total from page rows."""
    combined = dashboard["combined"]
    if not isinstance(combined, dict):
        raise ValueError("Combined aggregates are missing")
    collections = combined["collections"]
    if (not isinstance(collections, list) or len(collections) != 2
            or {item["dataset"] for item in collections} != set(COLLECTION_NAMES)):
        raise ValueError("Both collection aggregates are required")
    totals = {}
    for item in collections:
        total = _count(item["total"])
        if any(_count(item[key]) > total for key in ("retrievable", "unknown_dates")):
            raise ValueError("Invalid collection totals")
        totals[item["dataset"]] = total
    total = sum(totals.values())
    if _count(dashboard["stats"]["total"]) != total or _count(dashboard["page"]["total"]) != total:
        raise ValueError("Collection and page counts differ")
    for dataset in COLLECTION_NAMES:
        if sum(_count(row[dataset]) for row in combined["companies"]) != totals[dataset]:
            raise ValueError("Company chart must cover the complete selected collection")
        timeline = [row for row in combined["timeline"] if row["dataset"] == dataset]
        if sum(_count(row["count"]) for row in timeline) != totals[dataset]:
            raise ValueError("Year chart must cover known and unknown publication dates")
        unknown = sum(row["count"] for row in timeline if row["year"] == "Unknown")
        expected = next(item["unknown_dates"] for item in collections if item["dataset"] == dataset)
        if unknown != expected:
            raise ValueError("Unknown-date totals differ")
    for row in combined["timeline"]:
        if row["dataset"] not in COLLECTION_NAMES or not isinstance(row["year"], str):
            raise ValueError("Invalid yearly category")
    return combined


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _summary(combined):
    return [html.Div([
        html.Span(COLLECTION_NAMES[item["dataset"]], className="stat-label"),
        html.Strong(f"{item['total']:,}", className="stat-value"),
        html.Span(f"{item['retrievable']:,} with searchable text · {item['unknown_dates']:,} unknown dates", className="scope-note"),
    ], className="stat-card") for item in combined["collections"]]


def _company_groups(combined):
    grouped = {}
    for row in combined["companies"]:
        key = row["sponsor"].strip().casefold()
        target = grouped.setdefault(key, {"sponsor": row["sponsor"], "native": 0, "social": 0, "sources": []})
        for dataset in COLLECTION_NAMES:
            target[dataset] += row[dataset]
        target["sources"].append(row["sponsor"])
    companies = sorted(grouped.values(), key=lambda row: (-row["native"] - row["social"], row["sponsor"]))
    duplicates = Counter(sponsor_display(row["sponsor"]) for row in companies)
    for row in companies:
        row["sources"] = sorted(set(row["sources"]))
        row["label"] = sponsor_display(row["sponsor"])
        if duplicates[row["label"]] > 1:
            row["label"] += f" (source: {row['sponsor']})"
        row["selection"] = COMPANY_PREFIX + json.dumps(row["sources"], ensure_ascii=False, separators=(",", ":"))
    return companies


def _charts(combined):
    from .app import _figure

    companies = _company_groups(combined)
    company = _figure(None if companies else "No matching records")
    years = sorted({row["year"] for row in combined["timeline"]}, key=lambda year: (year == "Unknown", year))
    timeline = _figure(None if years else "No matching records")
    for dataset, name in COLLECTION_NAMES.items():
        company.add_trace(go.Bar(
            name=name, x=[row[dataset] for row in companies], y=[row["label"] for row in companies],
            customdata=[row["selection"] for row in companies],
            hovertext=["; ".join(row["sources"]) for row in companies], orientation="h",
            marker_color=COLORS[dataset], text=[row[dataset] for row in companies], textposition="auto",
            hovertemplate="%{y}<br>" + name + ": %{x:,}<br>Source names: %{hovertext}<extra></extra>",
        ))
        counts = Counter()
        for row in combined["timeline"]:
            if row["dataset"] == dataset:
                counts[row["year"]] += row["count"]
        timeline.add_trace(go.Bar(name=name, x=years, y=[counts[year] for year in years], marker_color=COLORS[dataset],
                                  text=[counts[year] for year in years], textposition="auto",
                                  hovertemplate="%{x}<br>" + name + ": %{y:,}<extra></extra>"))
    company.update_layout(barmode="group", height=max(360, len(companies) * 58 + 90),
                          legend={"orientation": "h", "y": 1.12}, yaxis={"autorange": "reversed", "automargin": True},
                          xaxis_title="Source records", margin={"l": 20, "r": 20, "t": 45, "b": 45})
    timeline.update_layout(barmode="group", legend={"orientation": "h", "y": 1.12},
                           xaxis={"type": "category"}, yaxis_title="Source records")
    return company, timeline


def register_combined(app, service, links_enabled, record_details=None):
    """Register unified views with the existing service and a signed page guard."""
    from .app import _figure, _filter_inputs, _filters, _page, _public_rows

    guard_key = "combined_page_guard"

    def filters_from(values):
        filters = _filters("all", *values)
        if filters.publishers or filters.platforms or filters.accounts or filters.labels:
            raise ValueError("Choose a collection view for collection-specific filters.")
        return filters

    def token(filters):
        health = service.health()
        if health.get("status") != "ok" or not health.get("data_version"):
            raise RuntimeError("Current collection state unavailable")
        reader = getattr(service, "social_source_state_version", None)
        # Existing social fingerprint is evaluated on only the social projection
        # of the shared company/date/collection-term scope.
        state = reader(filters.model_copy(update={"dataset": "social"})) if callable(reader) else None
        return {"data_version": health["data_version"], "social_source_state_version": state}

    def public_rows(source_rows):
        rows = _public_rows(source_rows, links_enabled)
        seen = set()
        for row in rows:
            if (row["dataset"] not in COLLECTION_NAMES or not row["record_id"] or not row["version_id"]
                    or row["record_id"] in seen):
                raise ValueError("Invalid page identity")
            seen.add(row["record_id"])
            social = row["dataset"] == "social"
            row["record_type"] = "Company social post" if social else "Native advertisement"
            row["channel"] = row["platform"] if social else row["publisher"]
            row["account"] = row["account"] if social else "Not applicable"
            row["count_unit"] = row.get("count_unit", "unique_platform_post_url") if social else "native_ad_record"
            row["archive_status"] = "Online archive" if row["archive_url"] else "No verified archived copy"
        if record_details is not None:
            summaries = record_details.summaries(source_rows)
            for row in rows:
                row.update(summaries.get(row["record_id"], {}))
        return rows

    def dashboard_read(filters, offset, size, sort_by, descending):
        for attempt in range(2):
            before = token(filters)
            result = service.dashboard(filters, offset=0 if attempt else offset, limit=size,
                                       sort_by=sort_by, descending=descending)
            combined = _validated_combined(result)
            rows = public_rows(result["page"]["rows"])
            if token(filters) == before:
                return result, combined, rows, before, bool(attempt)
        raise RuntimeError("Collections changed during the read")

    @app.callback(Output("all-overview", "hidden"), Output("all-records", "hidden"), Input("all-view", "value"))
    def show_view(view):
        return view == "records", view != "records"

    @app.callback(*[Output(f"all-{name}", "value") for name in FILTER_NAMES],
                  Output("all-dates", "start_date"), Output("all-dates", "end_date"),
                  Output("all-unknown-dates", "value"), Input("all-clear-filters", "n_clicks"), prevent_initial_call=True)
    def clear_filters(clicks):
        if not clicks:
            raise PreventUpdate
        return [*([] for _ in FILTER_NAMES), None, None, ["include"]]

    @app.callback(
        Output("all-sponsors", "value", allow_duplicate=True), Output("all-view", "value"),
        Output("all-sponsors", "options"), Input("all-companies-chart", "clickData"),
        State("all-snapshot", "data"), *_filter_inputs("all", State),
        State("all-sponsors", "options"), State("active-dataset", "value"), State("page-location", "pathname"),
        prevent_initial_call=True,
    )
    def select_company(click, previous, *values):
        options, active, pathname = values[FILTER_COUNT:]
        if active != "all" or _page(pathname) != "data":
            raise PreventUpdate
        try:
            filters = filters_from(values[:FILTER_COUNT])
            scope = filters.model_dump(mode="json")
            guard = session.get(guard_key)
            if (not isinstance(previous, dict) or not isinstance(guard, dict)
                    or guard.get("scope") != scope or _hash(previous) != guard.get("snapshot_sha256")
                    or guard.get("token") != token(filters)):
                raise PreventUpdate
            points = click.get("points") if isinstance(click, dict) else None
            point = points[0] if isinstance(points, list) and points and isinstance(points[0], dict) else {}
            selected = point.get("customdata")
            if not isinstance(selected, str) or not selected.startswith(COMPANY_PREFIX):
                raise PreventUpdate
            groups = _company_groups(previous["combined"])
            match = next((group for group in groups if group["selection"] == selected), None)
            if match is None or (filters.sponsors and not set(match["sources"]) <= set(filters.sponsors)):
                raise PreventUpdate
            options = [dict(option) for option in options] if isinstance(options, list) else []
            # A date filter can hide one casing variant without changing the
            # selected company identity. Restore all same-spelling source values
            # from the global dropdown, intersecting any existing company filter.
            variants = set(match["sources"])
            group_key = match["sponsor"].strip().casefold()
            for option in options:
                raw_values = decode_company_selection([option.get("value")])
                if raw_values and all(raw.strip().casefold() == group_key for raw in raw_values):
                    variants.update(raw_values)
            if filters.sponsors:
                variants.intersection_update(filters.sponsors)
            selected = COMPANY_PREFIX + json.dumps(sorted(variants), ensure_ascii=False, separators=(",", ":"))
            # An explicitly selected exact-source subset also needs a readable
            # option if it differs from the global company's option.
            if not any(option.get("value") == selected for option in options):
                options.append({"label": match["label"], "value": selected})
            return [selected], "records", options
        except PreventUpdate:
            raise
        except Exception as error:
            raise PreventUpdate from error

    @app.callback(
        Output("all-grid", "rowData"), Output("all-summary", "children"),
        Output("all-companies-chart", "figure"), Output("all-timeline-chart", "figure"),
        Output("all-status", "children"), Output("all-record-count", "children"),
        Output("all-page-prev", "disabled"), Output("all-page-next", "disabled"),
        Output("all-page-label", "children"), Output("all-page-offset", "data"), Output("all-snapshot", "data"),
        *_filter_inputs("all"), Input("all-page-prev", "n_clicks"), Input("all-page-next", "n_clicks"),
        Input("all-page-size", "value"), Input("all-sort", "value"),
        Input("active-dataset", "value"), Input("page-location", "pathname"),
        State("all-page-offset", "data"), State("all-snapshot", "data"),
    )
    def refresh(*values):
        previous_click, next_click, size, sort, active, pathname, offset, previous = values[FILTER_COUNT:]
        if active != "all" or _page(pathname) != "data":
            raise PreventUpdate
        try:
            filters = filters_from(values[:FILTER_COUNT])
            scope = filters.model_dump(mode="json")
            size = size if size in (20, 50, 100) else 20
            offset = max(0, int(offset or 0))
            if ctx.triggered_id == "all-page-next":
                offset += size
            elif ctx.triggered_id == "all-page-prev":
                offset = max(0, offset - size)
            else:
                offset = 0
            sort = sort if sort in ("date:desc", "date:asc", "title:asc", "sponsor:asc") else "date:desc"
            sort_by, direction = sort.split(":")
            page_trigger = ctx.triggered_id in {"all-page-next", "all-page-prev", "all-page-size", "all-sort"}
            guard = session.get(guard_key)
            refreshed = False
            if page_trigger:
                before = token(filters)
                if (isinstance(guard, dict) and isinstance(previous, dict)
                        and guard.get("scope") == scope and guard.get("token") == before
                        and _hash(previous) == guard.get("snapshot_sha256")):
                    page = service.page(filters, offset=offset, limit=size, sort_by=sort_by, descending=direction == "desc")
                    rows = public_rows(page["rows"])
                    if _count(page["total"]) == guard["total"] and token(filters) == before:
                        return (rows, no_update, no_update, no_update, no_update, no_update,
                                page["offset"] == 0, page["offset"] + size >= page["total"],
                                f"{page['offset'] + 1 if rows else 0:,}–{page['offset'] + len(rows):,} of {page['total']:,} records",
                                page["offset"], no_update)
                offset, refreshed = 0, True
            session.pop(guard_key, None)
            result, combined, rows, current, retried = dashboard_read(filters, offset, size, sort_by, direction == "desc")
            page = result["page"]
            total = page["total"]
            snapshot = {"filters": scope, "combined": combined, "token": current}
            session[guard_key] = {"scope": scope, "token": current, "total": total, "snapshot_sha256": _hash(snapshot)}
            companies, timeline = _charts(combined)
            counts = {item["dataset"]: item["total"] for item in combined["collections"]}
            notice = "The selection changed; showing the current first page. " if refreshed or retried else ""
            return (rows, _summary(combined), companies, timeline, notice + STATUS_NOTE,
                    f"{counts['native']:,} native ads · {counts['social']:,} company social posts",
                    page["offset"] == 0, page["offset"] + size >= total,
                    f"{page['offset'] + 1 if rows else 0:,}–{page['offset'] + len(rows):,} of {total:,} records",
                    page["offset"], snapshot)
        except Exception:
            session.pop(guard_key, None)
            return ([], [], _figure("Unavailable"), _figure("Unavailable"),
                    "Both collections are temporarily unavailable. Refresh the selection and try again.",
                    "Unavailable", True, True, "Unavailable", 0, None)

    @app.callback(Output("all-download", "data"), Output("all-export-status", "children"),
                  Input("all-export", "n_clicks"), *_filter_inputs("all", State),
                  State("all-snapshot", "data"), State("active-dataset", "value"), State("page-location", "pathname"),
                  prevent_initial_call=True)
    def export(clicks, *values):
        previous, active, pathname = values[FILTER_COUNT:]
        if not clicks or active != "all" or _page(pathname) != "data":
            raise PreventUpdate
        try:
            filters = filters_from(values[:FILTER_COUNT])
            guard = session.get(guard_key)
            before = token(filters)
            if (not isinstance(guard, dict) or not isinstance(previous, dict)
                    or guard.get("scope") != filters.model_dump(mode="json") or guard.get("token") != before
                    or _hash(previous) != guard.get("snapshot_sha256")):
                raise ValueError("Refresh selection before export")
            rows = public_rows(service.browse(filters))
            actual = Counter(row["dataset"] for row in rows)
            expected = {item["dataset"]: item["total"] for item in previous["combined"]["collections"]}
            if any(actual[dataset] != expected[dataset] for dataset in COLLECTION_NAMES) or token(filters) != before:
                raise ValueError("Export membership changed")
            fields = ["record_id", "version_id", "dataset", "record_type", "title", "date", "sponsor",
                      "sponsor_key", "channel", "publisher", "platform", "account", "keyword", "retrievable", "count_unit"]
            if links_enabled:
                fields.extend(["url", "archive_url"])
            fields.extend(["paid_ad_status", "social_historical_states"])
            output = StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                row = dict(row, social_historical_states="; ".join(row.get("social_historical_states", [])))
                writer.writerow({key: csv_safe_cell(value) for key, value in row.items()})
            return {"content": output.getvalue(), "filename": "native-ads-and-company-social-posts.csv", "type": "text/csv"}, (
                f"Downloaded {actual['native']:,} native ads and {actual['social']:,} company social posts."
            )
        except Exception:
            return None, "Download is unavailable. Refresh the selection and retry; your filters are preserved."
