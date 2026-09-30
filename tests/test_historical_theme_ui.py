"""Historical label drilldown keeps overlapping current-filter membership."""

import json

import pytest
from dash import Dash, dcc, html
from test_app import SECRET, callback, component_tree

from observatory.app import _label_chart
from observatory.historical_theme_ui import (
    historical_theme_panel,
    register_historical_themes,
)


class ThemeService:
    def __init__(self):
        self.browse_calls = []
        self.page_calls = []
        self.facet_calls = []
        self.fail = None
        self.extra_page_row = False
        self.rows = [
            {"record_id": f"native-{i}", "version_id": f"v-{i}", "dataset": "native",
             "title": f"Labeled article {i}", "publisher": "Outlet A", "sponsor": "exxonmobil",
             "keyword": "CCS", "date": "2024-06-01", "platform": "Website",
             "labels": labels, "retrievable": i % 2 == 0, "url": "https://example.test/article",
             "raw": {"secret": SECRET}, "internal_path": SECRET}
            for i, labels in enumerate([["green.a", "green.b"], ["green.a"], ["green.b", "fossil.c"], [], None, "green.a"])
        ]
        self.rows[2]["publisher"] = "Outlet B"
        self.rows[3]["date"] = None

    def facets(self, dataset):
        self.facet_calls.append(dataset)
        if self.fail == "facets":
            raise RuntimeError(SECRET)
        return {"labels": ["green.a", "green.b", "fossil.c", "outside.current.scope"]}

    def selected(self, filters):
        result = []
        for row in self.rows:
            if filters.dataset != row["dataset"]:
                continue
            if any(getattr(filters, plural) and row[field] not in getattr(filters, plural)
                   for field, plural in (("publisher", "publishers"), ("sponsor", "sponsors"),
                                         ("keyword", "keywords"), ("platform", "platforms"))):
                continue
            row_labels = row["labels"] if isinstance(row["labels"], list) else []
            if filters.labels and not set(filters.labels).intersection(row_labels):
                continue
            if filters.record_ids and row["record_id"] not in filters.record_ids:
                continue
            if not row["date"] and not filters.include_unknown_dates:
                continue
            if row["date"] and ((filters.date_from and row["date"] < filters.date_from.isoformat())
                                or (filters.date_to and row["date"] > filters.date_to.isoformat())):
                continue
            result.append(row)
        return result

    def browse(self, filters):
        self.browse_calls.append(filters.model_copy(deep=True))
        if self.fail == "browse":
            raise RuntimeError(SECRET)
        return self.selected(filters)

    def page(self, filters, offset=0, limit=20):
        self.page_calls.append((filters.model_copy(deep=True), offset, limit))
        if self.fail == "page":
            raise RuntimeError(SECRET)
        rows = self.selected(filters)
        page = rows[offset:offset + limit]
        if self.extra_page_row:
            page = [*page, {**self.rows[0], "record_id": "forged", "title": SECRET}]
        return {"rows": page, "total": len(rows), "offset": offset}


@pytest.fixture
def theme_app():
    service = ThemeService()
    app = Dash(__name__)
    app.layout = html.Div([
        dcc.Store(id="native-network-data"), dcc.Location(id="page-location"),
        dcc.RadioItems(id="native-view", value="overview"),
        dcc.Store(id="active-dataset", data="native"),
        dcc.Graph(id="native-labels-chart"), historical_theme_panel(),
    ])
    register_historical_themes(app, service, links_enabled=True)
    return app, app.server.test_client(), service


def values(**filters):
    return {
        "native-network-data.data": {"filters": {"dataset": "native", **filters}},
        "page-location.pathname": "/data", "native-view.value": "overview", "active-dataset.value": "native",
        "native-labels-chart.clickData": {"points": [{"customdata": "green.b", "x": 999,
                                                        "record_ids": ["forged"]}]},
        "historical-theme-label.value": "green.b", "historical-theme-offset.data": 0,
        "historical-theme-selection.data": "green.b",
    }


def select(theme_app, inputs=None, changed="native-labels-chart.clickData"):
    app, client, _service = theme_app
    return callback(app, client, "historical-theme-records.children", inputs or values(), changed)


def record_links(response):
    return {node["props"]["href"] for node in component_tree(response["historical-theme-records"])
            if node.get("type") == "A" and node["props"].get("href", "").startswith("/records/")}


def test_chart_contains_exact_label_identity_and_does_not_collapse_overlap():
    figure = _label_chart(rows=[{"labels": ["green.a", "green.b"]}, {"labels": ["green.a"]}])
    trace = figure.to_plotly_json()["data"][0]
    assert set(trace["customdata"]) == {"green.a", "green.b"}
    assert sum(trace["x"]) == 3


@pytest.mark.parametrize("changed", ["native-labels-chart.clickData", "historical-theme-label.value"])
def test_bar_and_keyboard_label_open_exact_records_without_client_counts(theme_app, changed):
    response = select(theme_app, changed=changed)
    assert record_links(response) == {"/records/native-0", "/records/native-2"}
    assert "2 matching records" in json.dumps(response)
    assert response["historical-theme-selection"]["data"] == "green.b"
    assert response["historical-theme-label"]["value"] == "green.b"
    assert theme_app[2].page_calls[-1][0].record_ids == ["native-0", "native-2"]
    assert SECRET not in json.dumps(response)
    assert "999" not in json.dumps(response)


def test_current_or_label_filter_is_intersected_not_replaced(theme_app):
    response = select(theme_app, values(labels=["green.a"]))
    applied = theme_app[2].page_calls[-1][0]
    assert applied.labels == ["green.a"]
    assert applied.record_ids == ["native-0"]
    assert record_links(response) == {"/records/native-0"}
    assert "1 matching records" in json.dumps(response)


def test_every_filter_and_record_id_constraint_are_preserved(theme_app):
    constraints = {"publishers": ["Outlet A"], "sponsors": ["exxonmobil"], "platforms": ["Website"],
                   "keywords": ["CCS"], "labels": ["green.a", "green.b"], "record_ids": ["native-0", "native-1"],
                   "date_from": "2024-01-01", "date_to": "2024-12-31", "include_unknown_dates": False}
    response = select(theme_app, values(**constraints))
    original = theme_app[2].browse_calls[-1]
    applied = theme_app[2].page_calls[-1][0]
    assert applied.model_dump() == {**original.model_dump(), "record_ids": ["native-0"]}
    assert record_links(response) == {"/records/native-0"}


def test_options_cover_only_recorded_labels_in_the_current_scope(theme_app):
    response = select(theme_app, values(), "native-network-data.data")
    assert {option["value"] for option in response["historical-theme-label"]["options"]} == {
        "green.a", "green.b", "fossil.c"
    }
    assert response["historical-theme-selection"]["data"] is None
    assert theme_app[2].page_calls == []
    assert SECRET not in json.dumps(response)


def test_missing_or_malformed_labels_are_not_positive_annotations(theme_app):
    response = select(theme_app, values(record_ids=["native-3", "native-4", "native-5"]), "native-network-data.data")
    assert response["historical-theme-label"]["options"] == []
    assert "does not establish that themes are absent" in json.dumps(response)
    assert theme_app[2].page_calls == []


@pytest.mark.parametrize("clicked", [None, {"points": []}, {"points": [None]},
                                     {"points": [{"customdata": {"label": "green.b", "record_ids": ["forged"]}}]},
                                     {"points": [{"customdata": ["green.b"]}]},
                                     {"points": [{"customdata": "outside.current.scope"}]},
                                     {"points": [{"customdata": "invented"}]},
                                     {"points": [{"y": "B", "x": 999}]}])
def test_forged_or_malformed_click_cannot_select_records(theme_app, clicked):
    inputs = values()
    inputs["native-labels-chart.clickData"] = clicked
    response = select(theme_app, inputs)
    assert response["historical-theme-selection"]["data"] is None
    assert theme_app[2].page_calls == []


def test_forged_label_dropdown_or_selection_store_is_not_trusted(theme_app):
    inputs = values()
    inputs["historical-theme-label.value"] = "invented"
    response = select(theme_app, inputs, "historical-theme-label.value")
    assert response["historical-theme-selection"]["data"] is None
    inputs["historical-theme-selection.data"] = {"label": "green.b", "record_ids": ["forged"]}
    response = select(theme_app, inputs, "historical-theme-next.n_clicks")
    assert response["historical-theme-selection"]["data"] is None
    assert theme_app[2].page_calls == []


@pytest.mark.parametrize("changed,value", [("page-location.pathname", "/query"),
                                           ("native-view.value", "relationships"),
                                           ("active-dataset.value", "social")])
def test_inactive_pages_do_not_call_services(theme_app, changed, value):
    inputs = values()
    inputs[changed] = value
    response = select(theme_app, inputs, changed)
    assert response["historical-theme-selection"]["data"] is None
    assert theme_app[2].browse_calls == theme_app[2].page_calls == theme_app[2].facet_calls == []


@pytest.mark.parametrize("snapshot", [None, {}, {"filters": {"dataset": "social"}},
                                       {"filters": {"dataset": "native", "date_from": "invalid"}}])
def test_invalid_snapshot_is_rejected_before_reading_sources(theme_app, snapshot):
    inputs = values()
    inputs["native-network-data.data"] = snapshot
    response = select(theme_app, inputs)
    assert "unavailable" in json.dumps(response)
    assert theme_app[2].browse_calls == theme_app[2].page_calls == theme_app[2].facet_calls == []


def test_new_scope_clears_old_selection_pagination_and_click(theme_app):
    inputs = values(publishers=["Outlet A"])
    inputs["historical-theme-offset.data"] = 20
    response = select(theme_app, inputs, "native-network-data.data")
    assert response["historical-theme-selection"]["data"] is None
    assert response["historical-theme-offset"]["data"] == 0
    assert response["historical-theme-label"]["value"] is None
    assert theme_app[2].page_calls == []


def test_record_pages_cover_all_members_without_changing_label_filters(theme_app):
    service = theme_app[2]
    service.rows = [{**service.rows[0], "record_id": f"native-{i:02d}", "title": f"Article {i}"} for i in range(23)]
    inputs = values(labels=["green.a"])
    first = select(theme_app, inputs)
    assert "23 matching records" in json.dumps(first)
    assert first["historical-theme-next"]["disabled"] is False
    following = select(theme_app, inputs, "historical-theme-next.n_clicks")
    assert following["historical-theme-offset"]["data"] == 10
    inputs["historical-theme-offset.data"] = 10
    last = select(theme_app, inputs, "historical-theme-next.n_clicks")
    assert last["historical-theme-offset"]["data"] == 20
    assert last["historical-theme-next"]["disabled"] is True
    assert record_links(last) == {"/records/native-20", "/records/native-21", "/records/native-22"}
    inputs["historical-theme-offset.data"] = 20
    back = select(theme_app, inputs, "historical-theme-prev.n_clicks")
    assert back["historical-theme-offset"]["data"] == 10
    assert all(filters.labels == ["green.a"] for filters, _offset, _limit in service.page_calls)


def test_clear_does_not_read_sources_again(theme_app):
    response = select(theme_app, values(), "historical-theme-reset.n_clicks")
    assert response["historical-theme-selection"]["data"] is None
    assert response["historical-theme-offset"]["data"] == 0
    assert theme_app[2].browse_calls == theme_app[2].page_calls == theme_app[2].facet_calls == []


@pytest.mark.parametrize("failed", ["facets", "browse", "page"])
def test_source_errors_are_sanitized_and_can_recover(theme_app, failed):
    theme_app[2].fail = failed
    response = select(theme_app)
    assert "temporarily unavailable" in json.dumps(response)
    assert SECRET not in json.dumps(response)
    theme_app[2].fail = None
    response = select(theme_app)
    assert record_links(response) == {"/records/native-0", "/records/native-2"}


def test_stale_or_fabricated_page_members_are_not_published(theme_app):
    theme_app[2].extra_page_row = True
    response = select(theme_app)
    assert "selection is unavailable" in json.dumps(response)
    assert record_links(response) == set()
    assert SECRET not in json.dumps(response)


def test_panel_explains_unverified_overlap_and_has_keyboard_equivalent():
    tree = historical_theme_panel().to_plotly_json()
    text = json.dumps(tree, default=lambda value: value.to_plotly_json())
    assert "unverified and can overlap" in text
    assert "not confirmed themes or greenwashing findings" in text
    assert "historical-theme-label" in text
