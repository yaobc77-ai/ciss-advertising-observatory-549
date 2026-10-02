"""Exercise matrix selection and tab state through Dash's callback protocol."""

import json
from datetime import date

import pytest
from dash import Dash, dcc, html
from test_app import SECRET, FakeService, callback, component_tree

from observatory.data_ui import register_data_views


@pytest.fixture
def matrix_app():
    service = FakeService()
    app = Dash(__name__)
    app.layout = html.Div([
        *[
            html.Div([
                dcc.RadioItems(id=f"{dataset}-view", value="overview"),
                html.Div(id=f"{dataset}-overview"),
                html.Div(id=f"{dataset}-relationships"),
                html.Div(id=f"{dataset}-records"),
            ])
            for dataset in ("native", "social")
        ],
        dcc.Store(id="native-network-data"),
        dcc.Graph(id="native-relationships-chart"),
        html.Div(id="matrix-records"),
        dcc.Store(id="matrix-selection"),
        dcc.Store(id="matrix-offset", data=0),
        html.Button(id="matrix-prev"),
        html.Button(id="matrix-next"),
        html.Button(id="matrix-reset"),
    ])
    register_data_views(app, service, links_enabled=True)
    return app, app.server.test_client(), service


def matrix_values(sponsor="Sponsor A", publisher="Outlet A", filters=None):
    return {
        "native-network-data.data": {
            "relationships": [
                {"sponsor": "Sponsor A", "publisher": "Outlet A", "count": 1},
                {"sponsor": "Sponsor B", "publisher": "Outlet B", "count": 1},
            ],
            "filters": {"dataset": "native", **(filters or {})},
        },
        "native-relationships-chart.clickData": {
            "points": [{"customdata": {"kind": "edge", "sponsor": sponsor, "publisher": publisher}}],
        },
        "matrix-offset.data": 0,
    }


def select(app, client, values, changed="native-relationships-chart.clickData"):
    return callback(app, client, "matrix-records.children", values, changed)


@pytest.mark.parametrize("dataset,view,visible", [
    ("native", "overview", "overview"),
    ("native", "relationships", "relationships"),
    ("native", "records", "records"),
    ("social", "overview", "overview"),
    ("social", "records", "records"),
    ("social", "relationships", "overview"),
    ("native", "invalid", "overview"),
])
def test_tabs_show_one_requested_view(matrix_app, dataset, view, visible):
    app, client, _service = matrix_app
    response = callback(app, client, f"{dataset}-overview.hidden", {f"{dataset}-view.value": view}, f"{dataset}-view.value")
    assert {name: response[f"{dataset}-{name}"]["hidden"] for name in ("overview", "relationships", "records")} == {
        name: name != visible for name in ("overview", "relationships", "records")
    }


def test_matrix_links_titles_and_preserves_every_filter(matrix_app):
    app, client, service = matrix_app
    filters = {
        "publishers": ["Outlet A", "Outlet B"],
        "sponsors": ["Sponsor A", "Sponsor B"],
        "platforms": ["Website"],
        "keywords": ["CCS"],
        "labels": ["historical_solution"],
        "record_ids": ["native-a"],
        "date_from": "2024-01-01",
        "date_to": "2024-12-31",
        "include_unknown_dates": False,
    }
    response = select(app, client, matrix_values(filters=filters))
    applied = service.browse_calls[-1]
    assert applied.model_dump() == {
        **filters, "dataset": "native", "publishers": ["Outlet A"], "sponsors": ["Sponsor A"],
        "date_from": date(2024, 1, 1), "date_to": date(2024, 12, 31), "date_presence": "any",
        "include_inferred_dates": False,
    }
    anchors = [node["props"] for node in component_tree(response["matrix-records"]) if node.get("type") == "A"]
    assert any(a["children"] == "A capture proposal" and a["href"] == "/records/native-a" for a in anchors)
    assert SECRET not in json.dumps(response)
    assert response["matrix-prev"]["disabled"] is True
    assert response["matrix-next"]["disabled"] is True


def test_matrix_zero_cell_is_explicit_and_retains_selection(matrix_app):
    app, client, service = matrix_app
    response = select(app, client, matrix_values(publisher="Outlet B"))
    assert "No matching records for this cell" in json.dumps(response)
    assert response["matrix-selection"]["data"] == {"kind": "edge", "sponsor": "Sponsor A", "publisher": "Outlet B"}
    assert len(service.browse_calls) == 1
    assert response["matrix-prev"]["disabled"] is True
    assert response["matrix-next"]["disabled"] is True


def test_matrix_accepts_dash_heatmap_coordinates_without_customdata(matrix_app):
    app, client, service = matrix_app
    service.rows[0]["sponsor"] = "exxonmobil"
    values = matrix_values()
    values["native-network-data.data"]["relationships"][0]["sponsor"] = "exxonmobil"
    # Dash removes the array pointNumber and object customdata from heatmaps.
    values["native-relationships-chart.clickData"] = {
        "points": [{"curveNumber": 0, "x": "Outlet A", "y": "exxonmobil", "z": 1}],
        "timestamp": 1789999999999,
    }
    response = select(app, client, values)
    assert service.browse_calls[-1].sponsors == ["exxonmobil"]
    assert "ExxonMobil" in json.dumps(response["matrix-records"])
    assert "A capture proposal" in json.dumps(response["matrix-records"])


def test_matrix_rejects_fabricated_dash_coordinates(matrix_app):
    app, client, service = matrix_app
    values = matrix_values()
    values["native-relationships-chart.clickData"] = {
        "points": [{"curveNumber": 0, "x": "Fabricated", "y": "Sponsor A", "z": 42}],
    }
    response = select(app, client, values)
    assert response["matrix-selection"]["data"] is None
    assert service.browse_calls == []


def test_matrix_does_not_guess_raw_sponsors_from_display_aliases(matrix_app):
    app, client, service = matrix_app
    values = matrix_values()
    values["native-network-data.data"]["relationships"][0]["sponsor"] = "exxonmobil"
    values["native-relationships-chart.clickData"] = {
        "points": [{"curveNumber": 0, "x": "Outlet A", "y": "ExxonMobil", "z": 1}],
    }
    response = select(app, client, values)
    assert response["matrix-selection"]["data"] is None
    assert service.browse_calls == []


@pytest.mark.parametrize("selection", [
    {"kind": "edge", "sponsor": "Fabricated", "publisher": "Outlet A"},
    {"kind": "edge", "sponsor": "Sponsor A", "publisher": "Fabricated"},
    {"kind": "sponsor", "sponsor": "Sponsor A"},
    {"kind": "edge", "sponsor": ["Sponsor A"], "publisher": "Outlet A"},
    None,
])
def test_matrix_rejects_invalid_cells_without_querying(matrix_app, selection):
    app, client, service = matrix_app
    values = matrix_values()
    values["native-relationships-chart.clickData"] = {"points": [{"customdata": selection}]}
    response = select(app, client, values)
    assert response["matrix-selection"]["data"] is None
    assert service.browse_calls == []


@pytest.mark.parametrize("filters", [
    {"sponsors": ["Sponsor B"]}, {"publishers": ["Outlet B"]},
    {"date_from": "not-a-date"}, {"dataset": "social"},
])
def test_matrix_never_widens_or_drops_incompatible_filters(matrix_app, filters):
    app, client, service = matrix_app
    response = select(app, client, matrix_values(filters=filters))
    assert response["matrix-selection"]["data"] is None
    assert service.browse_calls == []


def test_matrix_pagination_and_reset_preserve_current_scope(matrix_app):
    app, client, service = matrix_app
    service.rows = [{**service.rows[0], "record_id": f"native-{index}", "title": f"Record {index}"} for index in range(23)]
    values = matrix_values(filters={"keywords": ["CCS"], "include_unknown_dates": False})
    first = select(app, client, values)
    assert "23 matching records" in json.dumps(first)
    assert first["matrix-prev"]["disabled"] is True
    assert first["matrix-next"]["disabled"] is False
    values["matrix-selection.data"] = first["matrix-selection"]["data"]
    second = select(app, client, values, "matrix-next.n_clicks")
    assert second["matrix-offset"]["data"] == 10
    assert second["matrix-prev"]["disabled"] is False
    values["matrix-offset.data"] = 10
    last = select(app, client, values, "matrix-next.n_clicks")
    assert last["matrix-offset"]["data"] == 20
    assert last["matrix-next"]["disabled"] is True
    links = [node["props"]["href"] for node in component_tree(last) if node.get("type") == "A"]
    assert {href for href in links if href.startswith("/records/")} == {"/records/native-20", "/records/native-21", "/records/native-22"}
    values["matrix-offset.data"] = 20
    back = select(app, client, values, "matrix-prev.n_clicks")
    assert back["matrix-offset"]["data"] == 10
    assert all(f.keywords == ["CCS"] and not f.include_unknown_dates for f in service.browse_calls)
    calls = len(service.browse_calls)
    reset = select(app, client, values, "matrix-reset.n_clicks")
    assert reset["matrix-selection"]["data"] is None
    assert reset["matrix-offset"]["data"] == 0
    assert len(service.browse_calls) == calls


def test_new_snapshot_clears_old_selection_and_click(matrix_app):
    app, client, service = matrix_app
    values = matrix_values()
    values["matrix-selection.data"] = {"kind": "edge", "sponsor": "Sponsor A", "publisher": "Outlet A"}
    values["matrix-offset.data"] = 20
    response = select(app, client, values, "native-network-data.data")
    assert response["matrix-selection"]["data"] is None
    assert response["matrix-offset"]["data"] == 0
    assert "Select a matrix cell" in json.dumps(response)
    assert service.browse_calls == []


def test_matrix_service_errors_are_sanitized_and_can_recover(matrix_app):
    app, client, service = matrix_app
    service.fail_browse = True
    response = select(app, client, matrix_values())
    assert "temporarily unavailable" in json.dumps(response)
    assert SECRET not in json.dumps(response)
    assert response["matrix-selection"]["data"]["sponsor"] == "Sponsor A"
    service.fail_browse = False
    recovered = select(app, client, matrix_values())
    assert "A capture proposal" in json.dumps(recovered)
