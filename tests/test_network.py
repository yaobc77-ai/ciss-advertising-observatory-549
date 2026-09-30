"""Relationship provenance, scope and bounded table interactions."""

import json
from types import SimpleNamespace

from test_app import FakeService, callback, defaults

from observatory.app import create_app
from observatory.network import network_figure, select_relationship

RELATIONS = [
    {"sponsor": "exxonmobil", "publisher": "Outlet A", "count": 3},
    {"sponsor": "exxonmobil", "publisher": "Outlet B", "count": 2},
    {"sponsor": "cera", "publisher": "Outlet B", "count": 1},
]


def test_network_bounded_counts_and_type_separation():
    figure, summary = network_figure(RELATIONS, limit=2)
    assert summary == {"shown_relationships": 2, "total_relationships": 3, "shown_records": 5, "total_records": 6}
    points = figure.data[1]
    assert list(points.text) == ["3", "2"]
    assert points.customdata[0] == {"kind": "edge", "sponsor": "exxonmobil", "publisher": "Outlet A"}
    assert select_relationship(RELATIONS, {"kind": "edge", "sponsor": "cera", "publisher": "Outlet A"}) is None
    assert select_relationship(RELATIONS, {"kind": "publisher", "publisher": "exxonmobil"}) is None
    _, focused = network_figure(RELATIONS, {"kind": "publisher", "publisher": "Outlet B"})
    assert focused["total_records"] == 3


def application(legacy_network=False):
    service = FakeService()
    app = create_app(service, SimpleNamespace(show_source_links=True, cookie_secret="test", monthly_budget_usd=10))
    if legacy_network:
        # The aggregate network remains a comparison component, while the
        # production Relationships view now uses the article knowledge graph.
        from dash import html

        from observatory.data_layout import network_panel
        from observatory.network_ui import register_network

        current_layout = app.layout
        app.layout = lambda: html.Div([current_layout(), network_panel()])
        register_network(app, service, True)
    return app, app.server.test_client(), service


def test_network_click_drills_into_both_source_fields_and_resets():
    app, client, service = application(legacy_network=True)
    values = {
        "native-network-data.data": {"relationships": [{"sponsor": "Sponsor A", "publisher": "Outlet A", "count": 1}],
                                     "filters": {"dataset": "native", "keywords": ["CCS"]}},
        "network-graph.clickData": {"points": [{"customdata": {"kind": "edge", "sponsor": "Sponsor A", "publisher": "Outlet A"}}]},
        "network-limit.value": 24,
    }
    response = callback(app, client, "network-graph.figure", values, "network-graph.clickData")
    assert service.browse_calls[-1].sponsors == ["Sponsor A"]
    assert service.browse_calls[-1].publishers == ["Outlet A"]
    assert service.browse_calls[-1].keywords == ["CCS"]
    rendered = json.dumps(response["network-records"])
    assert "/records/native-a" in rendered and "version-a" in rendered
    assert "/records/native-b" not in rendered
    values["network-selection.data"] = response["network-selection"]["data"]
    reset = callback(app, client, "network-graph.figure", values, "network-reset.n_clicks")
    assert reset["network-selection"]["data"] is None
    assert reset["network-focus"]["value"] is None


def test_changed_filters_discard_stale_node_selection():
    app, client, service = application(legacy_network=True)
    response = callback(app, client, "network-graph.figure", {
        "native-network-data.data": {"relationships": [], "filters": {"dataset": "native"}},
        "network-selection.data": {"kind": "sponsor", "sponsor": "Sponsor A"},
        "network-graph.clickData": {"points": [{"customdata": {"kind": "sponsor", "sponsor": "Sponsor A"}}]},
    }, "native-network-data.data")
    assert response["network-selection"]["data"] is None
    assert service.browse_calls == []


def test_record_paging_preserves_whole_selection_statistics_and_filter_reset():
    app, client, service = application()
    template = service.rows[0]
    service.rows = [dict(template, record_id=f"r{i}", version_id=f"v{i}") for i in range(45)]
    values = defaults("native") | {"native-page-size.value": 20}
    first = callback(app, client, "native-grid.rowData", values, "native-page-size.value")
    assert len(first["native-grid"]["rowData"]) == 20
    assert first["native-record-count"]["children"].startswith("45 eligible")
    assert first["native-matrix"]["rowData"][-1]["total"] == 45
    assert first["native-page-prev"]["disabled"]
    second = callback(app, client, "native-grid.rowData", values | {
        "native-page-offset.data": 0, "native-network-data.data": first["native-network-data"]["data"],
    }, "native-page-next.n_clicks")
    assert second["native-page-offset"]["data"] == 20
    assert second["native-grid"]["rowData"][0]["record_id"] == "r20"
    assert "native-network-data" not in second  # paging must not reset a focused network
    last = callback(app, client, "native-grid.rowData", values | {"native-page-offset.data": 20}, "native-page-next.n_clicks")
    assert len(last["native-grid"]["rowData"]) == 5 and last["native-page-next"]["disabled"]
    reset = callback(app, client, "native-grid.rowData", values | {"native-page-offset.data": 40}, "native-sponsors.value")
    assert reset["native-page-offset"]["data"] == 0
