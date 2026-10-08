"""Unified collection UI through Dash callbacks; no model or database calls."""

import copy
import csv
import json
from collections import Counter
from io import StringIO

import pytest
from dash import Dash, dcc, html
from test_app import SECRET, callback, component_tree

from observatory.analytics import UNKNOWN
from observatory.combined_ui import (
    COLLECTION_NAMES,
    FILTER_NAMES,
    _charts,
    combined_filters,
    combined_panel,
    company_options,
    decode_company_selection,
    register_combined,
)


class CombinedService:
    def __init__(self):
        self.rows = [
            {"record_id": "native-a", "version_id": "native-v1", "dataset": "native",
             "title": "A native article", "sponsor": "shell", "publisher": "Outlet A",
             "date": "2024-01-01", "keyword": "energy", "url": "https://example.test/article",
             "archive_url": "https://archive.example.test/article", "retrievable": True, "internal": SECRET},
            {"record_id": "native-b", "version_id": "native-v2", "dataset": "native",
             "title": " =FORMULA()", "sponsor": UNKNOWN, "publisher": "Outlet B", "date": None,
             "keyword": "gas", "url": "javascript:alert(1)", "archive_url": "file:///private", "retrievable": False},
        ] + [
            {"record_id": f"social-{index}", "version_id": f"social-v{index}", "dataset": "social",
             "title": f"Post {index}", "sponsor": "Shell" if index < 24 else UNKNOWN,
             "publisher": "", "platform": "Twitter", "account": "Shell account",
             "date": "2023-02-01" if index < 20 else "2024-02-01", "keyword": "energy",
             "url": f"https://example.test/post/{index}", "retrievable": True,
             "count_unit": "unique_platform_post_url", "raw": {"secret": SECRET}}
            for index in range(25)
        ]
        self.version = "data-1"
        self.state_version = "state-1"
        self.dashboard_calls = []
        self.page_calls = []
        self.browse_calls = []
        self.source_calls = []
        self.fail = False
        self.drift = None

    def health(self):
        return {"status": "ok", "data_version": self.version, "secret": SECRET}

    def social_source_state_version(self, filters):
        assert filters.dataset == "social"
        self.source_calls.append(filters)
        return self.state_version

    def facets(self):
        return {"sponsors": ["shell", "Shell", UNKNOWN], "keywords": ["energy", "gas"]}

    def selected(self, filters):
        assert filters.dataset == "all"
        return [row for row in self.rows
                if (not filters.sponsors or row["sponsor"] in filters.sponsors)
                and (not filters.keywords or row["keyword"] in filters.keywords)
                and (filters.include_unknown_dates or row["date"])
                and (not filters.date_from or not row["date"] or row["date"] >= str(filters.date_from))
                and (not filters.date_to or not row["date"] or row["date"] <= str(filters.date_to))]

    def page(self, filters, offset=0, limit=20, sort_by="date", descending=True):
        self.page_calls.append(filters)
        rows = self.selected(filters)
        offset = min(offset, max(0, (len(rows) - 1) // limit * limit))
        if self.drift == "page":
            self.state_version += "-new"
        return {"rows": rows[offset:offset + limit], "total": len(rows), "offset": offset}

    def dashboard(self, filters, offset=0, limit=20, sort_by="date", descending=True):
        self.dashboard_calls.append(filters)
        if self.fail:
            raise RuntimeError(SECRET)
        rows = self.selected(filters)
        page = self.page(filters, offset, limit, sort_by, descending)
        collections = [{
            "dataset": dataset, "total": sum(row["dataset"] == dataset for row in rows),
            "retrievable": sum(row["dataset"] == dataset and row["retrievable"] for row in rows),
            "unknown_dates": sum(row["dataset"] == dataset and not row["date"] for row in rows),
        } for dataset in COLLECTION_NAMES]
        sponsors = sorted({row["sponsor"] for row in rows})
        companies = [{"sponsor": sponsor, **{
            dataset: sum(row["dataset"] == dataset and row["sponsor"] == sponsor for row in rows)
            for dataset in COLLECTION_NAMES
        }} for sponsor in sponsors]
        years = Counter((row["date"][:4] if row["date"] else "Unknown", row["dataset"]) for row in rows)
        result = {"stats": {"total": len(rows)}, "page": page, "combined": {
            "collections": collections, "companies": companies,
            "timeline": [{"year": year, "dataset": dataset, "count": count} for (year, dataset), count in years.items()],
        }}
        if self.drift == "dashboard":
            self.state_version += "-new"
        elif self.drift == "once":
            self.state_version += "-new"
            self.drift = None
        elif self.drift == "bad_aggregate":
            result["combined"]["companies"] = result["combined"]["companies"][:-1]
        return result

    def browse(self, filters):
        self.browse_calls.append(filters)
        if self.fail:
            raise RuntimeError(SECRET)
        if self.drift == "export":
            self.state_version += "-new"
        rows = self.selected(filters)
        if self.drift == "duplicate_export":
            return rows + rows[:1]
        if self.drift == "truncated_export":
            return rows[:-1]
        return rows


@pytest.fixture
def unified_app():
    service = CombinedService()
    app = Dash(__name__)
    app.server.secret_key = "signed-combined-test"
    app.layout = html.Div([
        combined_filters(service.facets()), combined_panel(True, lambda message: {}),
        dcc.Dropdown(id="active-dataset", value="all"), dcc.Location(id="page-location"),
    ])
    register_combined(app, service, True)
    return app, app.server.test_client(), service


def values(**extra):
    return {**{f"all-{field}.value": [] for field in FILTER_NAMES},
            "all-dates.start_date": None, "all-dates.end_date": None,
            "all-unknown-dates.value": ["include"], "all-page-size.value": 20,
            "all-sort.value": "date:desc", "active-dataset.value": "all",
            "page-location.pathname": "/data", "all-page-offset.data": 0, **extra}


def refresh(application, selection=None, changed="active-dataset.value", status=200):
    app, client, _service = application
    return callback(app, client, "all-grid.rowData", selection or values(), changed, expected_status=status)


def export(application, selection):
    app, client, _service = application
    return callback(app, client, "all-download.data", selection | {"all-export.n_clicks": 1}, "all-export.n_clicks")


def test_layout_shared_fields_record_types_and_navigation(unified_app):
    app, client, _service = unified_app
    layout = client.get("/_dash-layout").json
    props = {node["props"].get("id"): node["props"] for node in component_tree(layout)}
    assert set(f"all-{field}" for field in FILTER_NAMES) <= set(props)
    assert props["all-view"]["value"] == "overview"
    assert props["all-unknown-dates"]["value"] == ["include"]
    assert props["all-records"]["hidden"] is True
    columns = {item["field"]: item for item in props["all-grid"]["columnDefs"]}
    assert columns["title"]["cellRenderer"] == "RecordTitle"
    assert {"record_type", "channel", "account", "date", "url", "archive_status"} <= set(columns)
    assert "not verified paid advertising" in columns["sponsor"]["headerTooltip"]
    tabs = callback(app, client, "all-overview.hidden", {"all-view.value": "records"}, "all-view.value")
    assert tabs == {"all-overview": {"hidden": True}, "all-records": {"hidden": False}}


def test_company_values_merge_case_but_not_different_names():
    options = company_options({"sponsors": ["Shell", "shell", "Williams", "Williams Companies"]})
    shell = next(option for option in options if option["label"] == "Shell")
    assert decode_company_selection([shell["value"], "Shell"]) == ["Shell", "shell"]
    assert len(options) == 3
    assert decode_company_selection(["Williams"]) == ["Williams"]


@pytest.mark.parametrize("selection", ["Shell", [1], ['company:{"raw":"Shell"}'], ["company:[]"],
                                       ['company:["Shell","BP"]'], ["company:broken"]])
def test_invalid_company_selection_is_rejected(selection):
    with pytest.raises(ValueError):
        decode_company_selection(selection)


def test_aggregates_and_counts_cover_full_collections_not_first_page(unified_app):
    result = refresh(unified_app)
    _app, _client, service = unified_app
    assert len(result["all-grid"]["rowData"]) == 20
    assert result["all-record-count"]["children"] == "2 native ads · 25 company social posts"
    assert "verified paid advertisement" in result["all-status"]["children"]
    traces = result["all-companies-chart"]["figure"]["data"]
    assert [(trace["name"], sum(trace["x"])) for trace in traces] == [("Native advertisements", 2), ("Company social posts", 25)]
    shell_index = traces[0]["y"].index("Shell")
    assert (traces[0]["x"][shell_index], traces[1]["x"][shell_index]) == (1, 24)
    timelines = result["all-timeline-chart"]["figure"]["data"]
    assert sum(timelines[0]["y"]) == 2
    assert sum(timelines[1]["y"]) == 25
    assert timelines[0]["y"][timelines[0]["x"].index("Unknown")] == 1
    assert all(call.dataset == "social" for call in service.source_calls)
    assert SECRET not in json.dumps(result)
    rows = result["all-grid"]["rowData"]
    assert rows[0]["record_type"] == "Native advertisement"
    assert rows[2]["record_type"] == "Company social post"
    assert rows[2]["channel"] == "Twitter"
    assert rows[2]["paid_ad_status"] == "not_verified"
    assert rows[1]["url"] == rows[1]["archive_url"] == ""


def test_paging_reuses_authenticated_aggregate_and_keeps_scope(unified_app):
    first = refresh(unified_app)
    _app, _client, service = unified_app
    count = len(service.dashboard_calls)
    second = refresh(unified_app, values(**{"all-snapshot.data": first["all-snapshot"]["data"],
                                            "all-page-next.n_clicks": 1}), "all-page-next.n_clicks")
    assert len(service.dashboard_calls) == count
    assert second["all-page-offset"]["data"] == 20
    assert len(second["all-grid"]["rowData"]) == 7
    assert "all-summary" not in second and "all-companies-chart" not in second
    assert {row["record_id"] for row in first["all-grid"]["rowData"] + second["all-grid"]["rowData"]} == {
        row["record_id"] for row in service.rows
    }


@pytest.mark.parametrize("tamper", ["count", "scope", "token"])
def test_browser_snapshot_cannot_change_counts_or_scope(unified_app, tamper):
    first = refresh(unified_app)
    snapshot = copy.deepcopy(first["all-snapshot"]["data"])
    if tamper == "count":
        snapshot["combined"]["collections"][0]["total"] = 999999
    elif tamper == "scope":
        snapshot["filters"]["sponsors"] = ["Different company"]
    else:
        snapshot["token"]["data_version"] = "fake"
    result = refresh(unified_app, values(**{"all-snapshot.data": snapshot}), "all-page-next.n_clicks")
    assert result["all-page-offset"]["data"] == 0
    assert result["all-record-count"]["children"] == "2 native ads · 25 company social posts"
    assert "selection changed" in result["all-status"]["children"]


def test_stable_retry_returns_current_first_page(unified_app):
    _app, _client, service = unified_app
    service.drift = "once"
    result = refresh(unified_app)
    assert len(service.dashboard_calls) == 2
    assert result["all-page-offset"]["data"] == 0
    assert "selection changed" in result["all-status"]["children"]


@pytest.mark.parametrize("drift", ["dashboard", "bad_aggregate"])
def test_changing_source_or_incomplete_aggregates_hide_all_old_outputs(unified_app, drift):
    first = refresh(unified_app)
    _app, _client, service = unified_app
    service.drift = drift
    result = refresh(unified_app, values(**{"all-snapshot.data": first["all-snapshot"]["data"]}))
    assert result["all-grid"]["rowData"] == []
    assert result["all-summary"]["children"] == []
    assert result["all-snapshot"]["data"] is None
    assert result["all-record-count"]["children"] == "Unavailable"
    assert SECRET not in json.dumps(result)


def test_changed_source_invalidates_paging(unified_app):
    first = refresh(unified_app)
    _app, _client, service = unified_app
    service.state_version = "state-2"
    result = refresh(unified_app, values(**{"all-snapshot.data": first["all-snapshot"]["data"]}), "all-page-next.n_clicks")
    assert result["all-page-offset"]["data"] == 0
    assert len(service.dashboard_calls) == 2


def test_export_uses_complete_scope_and_separate_units(unified_app):
    first = refresh(unified_app)
    result = export(unified_app, values(**{"all-snapshot.data": first["all-snapshot"]["data"]}))
    rows = list(csv.DictReader(StringIO(result["all-download"]["data"]["content"])))
    assert Counter(row["dataset"] for row in rows) == {"native": 2, "social": 25}
    assert rows[1]["title"] == "' =FORMULA()"
    assert rows[0]["count_unit"] == "native_ad_record"
    assert rows[2]["count_unit"] == "unique_platform_post_url"
    assert rows[2]["paid_ad_status"] == "not_verified"
    assert rows[0]["sponsor_key"] == "shell"
    assert rows[2]["sponsor_key"] == "Shell"
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("drift", ["export", "duplicate_export", "truncated_export"])
def test_changed_or_inconsistent_export_is_withheld(unified_app, drift):
    first = refresh(unified_app)
    _app, _client, service = unified_app
    service.drift = drift
    result = export(unified_app, values(**{"all-snapshot.data": first["all-snapshot"]["data"]}))
    assert result["all-download"]["data"] is None
    assert "Refresh the selection" in result["all-export-status"]["children"]


def test_tampered_snapshot_cannot_authorize_export(unified_app):
    first = refresh(unified_app)
    snapshot = copy.deepcopy(first["all-snapshot"]["data"])
    snapshot["combined"]["collections"][0]["total"] = 42
    result = export(unified_app, values(**{"all-snapshot.data": snapshot}))
    assert result["all-download"]["data"] is None
    assert unified_app[2].browse_calls == []


def test_shared_company_selection_and_dates_cover_both_collections(unified_app):
    service = unified_app[2]
    option = next(option for option in company_options(service.facets()) if option["label"] == "Shell")
    selection = values(**{"all-sponsors.value": [option["value"]], "all-dates.start_date": "2024-01-01",
                           "all-dates.end_date": "2024-12-31", "all-unknown-dates.value": []})
    first = refresh(unified_app, selection)
    assert first["all-record-count"]["children"] == "1 native ads · 4 company social posts"
    assert service.dashboard_calls[-1].sponsors == ["Shell", "shell"]
    result = export(unified_app, selection | {"all-snapshot.data": first["all-snapshot"]["data"]})
    assert len(list(csv.DictReader(StringIO(result["all-download"]["data"]["content"])))) == 5
    assert all(call.sponsors == ["Shell", "shell"] and not call.include_unknown_dates for call in service.source_calls)


@pytest.mark.parametrize("field", ["publishers", "platforms", "labels", "accounts"])
def test_hidden_collection_specific_filters_never_silently_narrow_combined_scope(unified_app, field):
    result = refresh(unified_app, values(**{f"all-{field}.value": ["forged"]}))
    assert result["all-grid"]["rowData"] == []
    assert unified_app[2].dashboard_calls == []


@pytest.mark.parametrize("active,path", [("native", "/data"), ("social", "/data"), ("all", "/query")])
def test_hidden_combined_page_never_reads_data(unified_app, active, path):
    refresh(unified_app, values(**{"active-dataset.value": active, "page-location.pathname": path}), status=204)
    assert not unified_app[2].dashboard_calls
    assert not unified_app[2].source_calls


def test_disabled_links_do_not_export_source_urls():
    service = CombinedService()
    app = Dash(__name__)
    app.server.secret_key = "disabled-links-test"
    app.layout = html.Div([combined_filters(service.facets()), combined_panel(False, lambda message: {}),
                           dcc.Dropdown(id="active-dataset"), dcc.Location(id="page-location")])
    register_combined(app, service, False)
    fixture = app, app.server.test_client(), service
    first = refresh(fixture)
    assert all(row["url"] == row["archive_url"] == "" for row in first["all-grid"]["rowData"])
    result = export(fixture, values(**{"all-snapshot.data": first["all-snapshot"]["data"]}))
    rows = list(csv.DictReader(StringIO(result["all-download"]["data"]["content"])))
    assert "url" not in rows[0] and "archive_url" not in rows[0]


def test_zero_scope_preserves_explicit_separate_zero_counts(unified_app):
    result = refresh(unified_app, values(**{"all-keywords.value": ["not-collected"]}))
    assert result["all-record-count"]["children"] == "0 native ads · 0 company social posts"
    assert result["all-grid"]["rowData"] == []
    assert result["all-page-prev"]["disabled"] is True
    assert result["all-page-next"]["disabled"] is True


def test_company_figure_does_not_merge_different_williams_aliases():
    company, _timeline = _charts({"companies": [
        {"sponsor": "Williams", "native": 1, "social": 0},
        {"sponsor": "Williams Companies", "native": 0, "social": 2},
    ], "timeline": []})
    assert set(company.data[0].y) == {"Williams", "Williams Companies"}


def company_click(application, first, selection=None, clicked=None, status=200):
    app, client, service = application
    if clicked is None:
        trace = first["all-companies-chart"]["figure"]["data"][0]
        clicked = trace["customdata"][trace["y"].index("Shell")]
    current = (selection or values()) | {
        "all-snapshot.data": first["all-snapshot"]["data"],
        "all-sponsors.options": company_options(service.facets()),
        "all-companies-chart.clickData": {"points": [{"customdata": clicked}]},
    }
    return callback(app, client, "all-view.value", current, "all-companies-chart.clickData", expected_status=status)


def test_company_bar_opens_matching_records_preserving_dates_and_terms(unified_app):
    selection = values(**{"all-dates.start_date": "2024-01-01", "all-dates.end_date": "2024-12-31",
                           "all-keywords.value": ["energy"], "all-unknown-dates.value": []})
    first = refresh(unified_app, selection)
    result = company_click(unified_app, first, selection)
    assert result["all-view"]["value"] == "records"
    company_values = result["all-sponsors"]["value"]
    assert decode_company_selection(company_values) == ["Shell", "shell"]
    matched = refresh(unified_app, selection | {"all-sponsors.value": company_values}, "all-sponsors.value")
    assert matched["all-record-count"]["children"] == "1 native ads · 4 company social posts"
    assert all(row["keyword"] == "energy" and row["date"].startswith("2024") for row in matched["all-grid"]["rowData"])


def test_trailing_slash_data_route_can_load_select_company_and_export(unified_app):
    selection = values(**{"page-location.pathname": "/data/"})
    first = refresh(unified_app, selection)
    assert first["all-record-count"]["children"] == "2 native ads · 25 company social posts"
    clicked = company_click(unified_app, first, selection)
    assert clicked["all-view"]["value"] == "records"
    selected = selection | {"all-sponsors.value": clicked["all-sponsors"]["value"]}
    current = refresh(unified_app, selected, "all-sponsors.value")
    downloaded = export(unified_app, selected | {"all-snapshot.data": current["all-snapshot"]["data"]})
    rows = list(csv.DictReader(StringIO(downloaded["all-download"]["data"]["content"])))
    assert {row["dataset"] for row in rows} == {"native", "social"}
    assert len(rows) == 25


def test_company_bar_can_only_narrow_existing_company_scope(unified_app):
    selection = values(**{"all-sponsors.value": ["Shell"]})
    first = refresh(unified_app, selection)
    result = company_click(unified_app, first, selection)
    assert decode_company_selection(result["all-sponsors"]["value"]) == ["Shell"]
    assert {"label": "Shell", "value": 'company:["Shell"]'} in result["all-sponsors"]["options"]
    company_click(unified_app, first, selection, 'company:["Shell","shell"]', status=204)


def test_date_filtered_company_click_keeps_full_same_spelling_identity(unified_app):
    selection = values(**{"all-dates.start_date": "2023-01-01", "all-dates.end_date": "2023-12-31",
                           "all-unknown-dates.value": []})
    first = refresh(unified_app, selection)
    assert first["all-record-count"]["children"] == "0 native ads · 20 company social posts"
    result = company_click(unified_app, first, selection)
    assert decode_company_selection(result["all-sponsors"]["value"]) == ["Shell", "shell"]
    cleared_dates = refresh(unified_app, values(**{"all-sponsors.value": result["all-sponsors"]["value"]}))
    assert cleared_dates["all-record-count"]["children"] == "1 native ads · 24 company social posts"


@pytest.mark.parametrize("clicked", ["Shell", "company:broken", 'company:["BP"]', None, {}, ["Shell"]])
def test_malformed_or_fabricated_company_click_is_ignored(unified_app, clicked):
    first = refresh(unified_app)
    if clicked is None:
        clicked = ""
    company_click(unified_app, first, clicked=clicked, status=204)


def test_stale_or_tampered_company_click_is_ignored(unified_app):
    first = refresh(unified_app)
    unified_app[2].state_version = "source-changed"
    company_click(unified_app, first, status=204)
    fresh = refresh(unified_app)
    fresh["all-snapshot"]["data"]["combined"]["companies"][0]["native"] = 999
    company_click(unified_app, fresh, status=204)


def test_identical_display_aliases_keep_distinct_source_categories():
    options = company_options({"sponsors": ["cera", "CERAWeek"]})
    assert len({option["label"] for option in options}) == 2
    company, _timeline = _charts({"companies": [
        {"sponsor": "cera", "native": 1, "social": 0},
        {"sponsor": "CERAWeek", "native": 0, "social": 2},
    ], "timeline": []})
    assert len(set(company.data[0].y)) == 2
