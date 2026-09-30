"""Exercise large-map lifecycle and provenance through actual Dash callbacks."""

import json
from datetime import date

import pytest
from dash import Dash, dcc, html
from test_app import SECRET, callback, component_tree
from test_collection_graph_visual import sample_graph

from observatory.collection_graph_breakdown import selection_breakdown
from observatory.collection_graph_ui import (
    ALL_BUCKETS,
    collection_graph_panel,
    register_collection_graph,
)
from observatory.knowledge_map import build_collection_map
from observatory.models import Filters


class MapService:
    def __init__(self):
        self.graph = sample_graph()
        self.calls = []
        self.fail = False

    def knowledge_map(self, filters):
        self.calls.append(filters.model_dump(mode="json"))
        if self.fail:
            raise RuntimeError(SECRET)
        return self.graph


@pytest.fixture
def map_app():
    service = MapService()
    app = Dash(__name__)
    app.layout = html.Div([
        dcc.Store(id="native-network-data"), dcc.Location(id="page-location"),
        dcc.RadioItems(id="native-view", value="relationships"),
        dcc.RadioItems(id="active-dataset", value="native"), collection_graph_panel(),
    ])
    register_collection_graph(app, service, True)
    return app, app.server.test_client(), service


def values(**overrides):
    return {"native-network-data.data": {"filters": {"dataset": "native"}},
            "page-location.pathname": "/data", "native-view.value": "relationships", "active-dataset.value": "native",
            "collection-graph-view.value": "entities", "collection-graph-layout.value": "cose",
            "collection-graph-offset.data": 0, "collection-graph-canvas.zoom": 1,
            # Most interactions start with a populated canvas. The callback
            # checks presence only and never trusts these client contents.
            "collection-graph-canvas.elements": [{"data": {"id": "already-rendered"}}],
            **overrides}


def explore(map_app, data=None, changed="native-network-data.data", expected_status=200):
    app, client, _ = map_app
    return callback(app, client, "collection-graph-inspector.children", data or values(), changed,
                    expected_status=expected_status)


def test_panel_has_large_canvas_and_accessible_navigation_controls(map_app):
    app, client, _ = map_app
    components = {item["props"].get("id"): item["props"] for item in component_tree(client.get("/_dash-layout").json)}
    assert components["collection-graph-canvas"]["responsive"] is False
    assert "collection-graph-size" in components
    assert components["collection-graph-canvas"]["style"]["height"] == "100%"
    assert "style" not in components["collection-graph-stage"]
    assert components["collection-graph-stage"]["className"] == "collection-graph-stage"
    assert components["collection-graph-zoom-in"]["aria-label"] == "Zoom in"
    assert components["collection-graph-find"]["clearable"] is True
    assert app.callback_map


@pytest.mark.parametrize("inactive", [{"page-location.pathname": "/query"},
                                      {"native-view.value": "overview"},
                                      {"active-dataset.value": "social"}])
def test_inactive_map_never_queries_service(map_app, inactive):
    explore(map_app, values(**inactive), expected_status=204)
    assert map_app[2].calls == []


def test_complete_map_counts_and_full_filters_remain_visible(map_app):
    filters = Filters(publishers=["The Washington Post"], sponsors=["exxonmobil"],
                      keywords=["CCS"], labels=["historic"], record_ids=["record-1"],
                      platforms=["Website"], date_from=date(2020, 1, 1), include_unknown_dates=False)
    response = explore(map_app, values(**{"native-network-data.data": {"filters": filters.model_dump(mode="json")}}))
    assert map_app[2].calls == [filters.model_dump(mode="json")]
    assert "23 of 23 eligible records" in json.dumps(response["collection-graph-coverage"]["children"])
    assert "4 counted source associations" in json.dumps(response["collection-graph-coverage"]["children"])
    assert len(response["collection-graph-canvas"]["elements"]) == 8
    assert response["collection-graph-canvas"]["layout"]["randomize"] is False


@pytest.mark.parametrize("bad", [None, {}, {"filters": {"dataset": "social"}},
                                 {"filters": {"dataset": "native", "date_from": "not-a-date"}}])
def test_bad_scope_never_loads_full_collection(map_app, bad):
    response = explore(map_app, values(**{"native-network-data.data": bad}))
    assert map_app[2].calls == []
    assert response["collection-graph-canvas"]["elements"] == []
    assert "temporarily unavailable" in response["collection-graph-coverage"]["children"]


def test_node_click_uses_server_values_and_does_not_relayout(map_app):
    graph = map_app[2].graph
    sponsor = next(node for node in graph["nodes"] if node["type"] == "SponsorCandidate" and node["label"] == "ExxonMobil")
    clicked = {"id": sponsor["id"], "label": "Forged payment claim", "record_count": 9000, "provenance": SECRET}
    response = explore(map_app, values(**{"collection-graph-canvas.tapNodeData": clicked}), "collection-graph-canvas.tapNodeData")
    assert response["collection-graph-selection"]["data"] == {"kind": "node", "id": sponsor["id"]}
    text = json.dumps(response)
    assert "20 supporting records" in text and "Connected outlets" in text
    assert "Forged payment claim" not in text and SECRET not in text
    assert "elements" not in response["collection-graph-canvas"]
    assert "layout" not in response["collection-graph-canvas"]
    assert response["collection-graph-next"]["disabled"] is False
    assert any(item["props"].get("href", "").startswith("/records/record-")
               for item in component_tree(response["collection-graph-inspector"]) if item.get("type") == "A")


def test_counted_edge_click_and_keyboard_share_canonical_selection(map_app):
    edge = map_app[2].graph["summary_edges"][0]
    click = values(**{"collection-graph-canvas.tapEdgeData": {"id": edge["id"], "count": 99999}})
    response = explore(map_app, click, "collection-graph-canvas.tapEdgeData")
    assert f"{edge['count']} supporting records" in json.dumps(response)
    assert "derived_from_article_source_fields" in json.dumps(response)
    assert "99999" not in json.dumps(response)
    selected = {"kind": "edge", "id": edge["id"]}
    keyboard = explore(map_app, values(**{"collection-graph-find.value": json.dumps(selected)}), "collection-graph-find.value")
    assert keyboard["collection-graph-selection"]["data"] == selected


def test_forged_ids_and_new_scope_clear_selection(map_app):
    response = explore(map_app, values(**{"collection-graph-canvas.tapNodeData": {"id": "unknown"}}), "collection-graph-canvas.tapNodeData")
    assert response["collection-graph-selection"]["data"] is None
    graph = map_app[2].graph
    previous = {"kind": "node", "id": graph["nodes"][0]["id"]}
    reset = explore(map_app, values(**{"collection-graph-selection.data": previous,
                                      "collection-graph-anchor.data": previous, "collection-graph-offset.data": 20}))
    assert reset["collection-graph-selection"]["data"] is None
    assert reset["collection-graph-anchor"]["data"] is None
    assert reset["collection-graph-offset"]["data"] == 0


def test_record_pager_preserves_graph_positions_and_exact_counts(map_app):
    sponsor = next(node for node in map_app[2].graph["nodes"] if node["label"] == "ExxonMobil")
    selected = {"kind": "node", "id": sponsor["id"]}
    response = explore(map_app, values(**{"collection-graph-selection.data": selected}), "collection-graph-next.n_clicks")
    assert response["collection-graph-offset"]["data"] == 10
    assert response["collection-graph-next"]["disabled"] is True
    assert response["collection-graph-prev"]["disabled"] is False
    assert "Showing 11" in json.dumps(response) and "of 20" in json.dumps(response)
    assert "layout" not in response["collection-graph-canvas"]


def test_article_expansion_is_explicit_and_retains_full_coverage(map_app):
    edge = map_app[2].graph["summary_edges"][0]
    selected = {"kind": "edge", "id": edge["id"]}
    response = explore(map_app, values(**{"collection-graph-selection.data": selected,
                                         "collection-graph-view.value": "articles"}), "collection-graph-view.value")
    elements = response["collection-graph-canvas"]["elements"]
    assert sum(item["data"].get("type") == "Article" for item in elements) == edge["count"]
    assert response["collection-graph-anchor"]["data"] == selected
    assert "23 of 23 eligible records" in json.dumps(response["collection-graph-coverage"]["children"])
    assert "explicitly expanded selection" in json.dumps(response["collection-graph-coverage"]["children"])


def test_errors_do_not_expose_connection_and_recovery_loads_complete_map(map_app):
    map_app[2].fail = True
    response = explore(map_app)
    assert SECRET not in json.dumps(response)
    assert response["collection-graph-canvas"]["elements"] == []
    map_app[2].fail = False
    assert "23 of 23" in json.dumps(explore(map_app)["collection-graph-coverage"]["children"])


def test_json_export_rebuilds_full_server_graph_not_visible_expansion(map_app):
    app, client, service = map_app
    data = values(**{"collection-graph-export.n_clicks": 1})
    response = callback(app, client, "collection-graph-download.data", data, "collection-graph-export.n_clicks")
    graph = json.loads(response["collection-graph-download"]["data"]["content"])
    assert graph["coverage"]["total_records"] == 23
    assert len(graph["records"]) == 23 and len(service.calls) == 1
    assert SECRET not in json.dumps(response)


def test_viewport_buttons_and_png_are_independent_of_source_claims(map_app):
    app, client, service = map_app
    zoom = explore(map_app, values(**{"collection-graph-canvas.zoom": 2}), "collection-graph-zoom-in.n_clicks")
    assert zoom["collection-graph-canvas"]["zoom"] == 2.5
    fit = explore(map_app, values(**{"collection-graph-fit.n_clicks": 1}), "collection-graph-fit.n_clicks")
    assert fit["collection-graph-canvas"]["layout"]["name"] == "preset"
    assert "elements" not in fit["collection-graph-canvas"]
    expanded = callback(app, client, "collection-graph-panel.className", {"collection-graph-expand.n_clicks": 1}, "collection-graph-expand.n_clicks")
    assert "collection-graph-expanded" in expanded["collection-graph-panel"]["className"]
    assert "collection-graph-canvas" not in expanded
    calls = len(service.calls)
    image = callback(app, client, "collection-graph-canvas.generateImage", {"collection-graph-png.n_clicks": 1}, "collection-graph-png.n_clicks")
    assert image["collection-graph-canvas"]["generateImage"]["action"] == "download"
    assert len(service.calls) == calls


def test_positive_resize_refits_existing_positions_without_resetting_selection(map_app):
    sponsor = next(node for node in map_app[2].graph["nodes"] if node["label"] == "ExxonMobil")
    selected = {"kind": "node", "id": sponsor["id"]}
    layouts = []
    for revision in (1, 2):
        response = explore(map_app, values(**{
            "collection-graph-selection.data": selected,
            "collection-graph-size.data": {"width": 1132, "height": 720, "revision": revision},
        }), "collection-graph-size.data")
        assert response["collection-graph-selection"]["data"] == selected
        canvas = response["collection-graph-canvas"]
        assert "elements" not in canvas and "pan" not in canvas and "zoom" not in canvas
        assert canvas["layout"]["name"] == "preset" and canvas["layout"]["fit"] is True
        layouts.append(canvas["layout"])
    assert layouts[0] != layouts[1]


@pytest.mark.parametrize("empty", [None, []])
@pytest.mark.parametrize("trigger", ["collection-graph-size.data", "collection-graph-find.value"])
def test_resize_or_selection_recovers_elements_when_initial_response_was_superseded(map_app, empty, trigger):
    graph = map_app[2].graph
    sponsor = next(node for node in graph["nodes"] if node["label"] == "ExxonMobil")
    selected = {"kind": "node", "id": sponsor["id"]}
    response = explore(map_app, values(**{
        "collection-graph-canvas.elements": empty,
        "collection-graph-selection.data": selected,
        "collection-graph-find.value": json.dumps(selected),
        "collection-graph-size.data": {"width": 1132, "height": 720, "revision": 1},
    }), trigger)
    canvas = response["collection-graph-canvas"]
    nodes = [item["data"] for item in canvas["elements"] if item["data"]["kind"] == "node"]
    expected_ids = {item["id"] for item in graph["nodes"] if item["type"] != "Article"}
    assert {item["id"] for item in nodes} == expected_ids
    assert len(canvas["elements"]) == len(expected_ids) + len(graph["summary_edges"])
    assert canvas["layout"]["fit"] is True
    assert "20 supporting records" in json.dumps(response["collection-graph-selection-heading"])
    assert response["collection-graph-selection"]["data"] == selected


@pytest.mark.parametrize("size", [None, {}, {"width": 0, "height": 720, "revision": 1},
                                  {"width": True, "height": 720, "revision": 1},
                                  {"width": 1132, "height": 720, "revision": -1}])
def test_hidden_or_invalid_resize_does_not_query_collection(map_app, size):
    explore(map_app, values(**{"collection-graph-size.data": size}), "collection-graph-size.data", expected_status=204)
    assert map_app[2].calls == []


def test_donut_click_narrows_records_keeps_parent_and_preserves_entity_viewport(map_app):
    graph = map_app[2].graph
    sponsor = next(node for node in graph["nodes"] if node["label"] == "ExxonMobil")
    selected = {"kind": "node", "id": sponsor["id"]}
    detail = selection_breakdown(graph, selected)
    category = next(row for row in detail["categories"] if row["label"] == "The Washington Post")
    response = explore(map_app, values(**{"collection-graph-selection.data": selected,
                                         "collection-graph-breakdown.clickData": {"points": [{"customdata": category["id"], "value": 99999}]}}),
                       "collection-graph-breakdown.clickData")
    assert response["collection-graph-selection"]["data"] == selected
    assert response["collection-graph-bucket"]["value"] == category["id"]
    assert "10 matching records" in json.dumps(response["collection-graph-inspector"])
    assert "99999" not in json.dumps(response)
    chart = response["collection-graph-breakdown"]["figure"]["data"][0]
    assert sum(chart["values"]) == 20
    assert "elements" not in response["collection-graph-canvas"] and "layout" not in response["collection-graph-canvas"]
    assert any(rule["selector"] == f'edge[id = "{category["edge_id"]}"]'
               for rule in response["collection-graph-canvas"]["stylesheet"])


def test_article_mode_expands_exact_pie_bucket_and_all_option_restores_parent(map_app):
    graph = map_app[2].graph
    sponsor = next(node for node in graph["nodes"] if node["label"] == "ExxonMobil")
    selected = {"kind": "node", "id": sponsor["id"]}
    category = selection_breakdown(graph, selected)["categories"][0]
    data = values(**{"collection-graph-selection.data": selected, "collection-graph-view.value": "articles",
                     "collection-graph-bucket-selection.data": category["id"]})
    response = explore(map_app, data, "collection-graph-view.value")
    assert sum(item["data"].get("type") == "Article" for item in response["collection-graph-canvas"]["elements"]) == category["count"]
    restore = explore(map_app, {**data, "collection-graph-anchor.data": selected,
                               "collection-graph-bucket.value": ALL_BUCKETS}, "collection-graph-bucket.value")
    assert sum(item["data"].get("type") == "Article" for item in restore["collection-graph-canvas"]["elements"]) == 20


@pytest.mark.parametrize("forged", ["unavailable-bucket", ["bad"], 5, {"count": 9999}])
def test_forged_donut_bucket_never_creates_counts_or_widens_parent(map_app, forged):
    sponsor = next(node for node in map_app[2].graph["nodes"] if node["label"] == "ExxonMobil")
    selected = {"kind": "node", "id": sponsor["id"]}
    response = explore(map_app, values(**{"collection-graph-selection.data": selected,
                                         "collection-graph-bucket.value": forged}), "collection-graph-bucket.value")
    assert response["collection-graph-selection"]["data"] == selected
    assert response["collection-graph-bucket"]["value"] == ALL_BUCKETS
    assert "of 20" in json.dumps(response["collection-graph-inspector"])


def test_missing_sponsor_slice_includes_record_without_creating_entity(map_app):
    service = map_app[2]
    rows = [{"record_id": "known", "version_id": "v-known", "dataset": "native", "sponsor": "api", "publisher": "Outlet A"},
            {"record_id": "missing", "version_id": "v-missing", "dataset": "native", "sponsor": "", "publisher": "Outlet A"}]
    service.graph = build_collection_map(rows)
    outlet = next(node for node in service.graph["nodes"] if node["type"] == "Outlet")
    selected = {"kind": "node", "id": outlet["id"]}
    category = next(row for row in selection_breakdown(service.graph, selected)["categories"] if row["missing"])
    data = values(**{"collection-graph-selection.data": selected, "collection-graph-view.value": "articles",
                     "collection-graph-bucket-selection.data": category["id"]})
    response = explore(map_app, data, "collection-graph-view.value")
    assert sum(response["collection-graph-breakdown"]["figure"]["data"][0]["values"]) == 2
    elements = response["collection-graph-canvas"]["elements"]
    assert {item["data"]["type"] for item in elements if item["data"].get("kind") == "node"} == {"Article", "Outlet"}
    assert "1 matching records" in json.dumps(response["collection-graph-inspector"])


def test_client_question_buttons_use_existing_filtered_entities(map_app):
    response = explore(map_app, values(), "collection-graph-example-company.n_clicks")
    assert "20 supporting records" in json.dumps(response["collection-graph-selection-heading"])
    assert response["collection-graph-breakdown"]["style"] == {"height": "290px"}
    map_app[2].graph = build_collection_map([{"dataset": "native", "record_id": "one", "version_id": "v-one",
                                            "publisher": "The Washington Post", "sponsor": "exxonmobil"}])
    missing = explore(map_app, values(), "collection-graph-example-count.n_clicks")
    assert missing["collection-graph-selection"]["data"] is None
    assert "No supporting records in the current filters" in json.dumps(missing["collection-graph-selection-heading"])


def test_selection_count_csv_contains_every_category_and_server_denominator(map_app):
    app, client, service = map_app
    sponsor = next(node for node in service.graph["nodes"] if node["label"] == "ExxonMobil")
    response = callback(app, client, "collection-graph-counts-download.data",
                        values(**{"collection-graph-counts-export.n_clicks": 1,
                                  "collection-graph-selection.data": {"kind": "node", "id": sponsor["id"], "record_count": 9999}}),
                        "collection-graph-counts-export.n_clicks")
    assert "Prepared 2 categories for 20 supporting records" in response["collection-graph-counts-status"]["children"]
    content = response["collection-graph-counts-download"]["data"]["content"]
    assert "The Washington Post" in content and "The New York Times" in content and "9999" not in content
