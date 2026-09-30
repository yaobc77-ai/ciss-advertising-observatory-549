"""Knowledge explorer isolation, provenance and bounded public export."""

import json

import pytest
from dash import Dash, dcc, html
from test_app import callback, component_tree

from observatory.knowledge_ui import (
    _properties,
    _source_url,
    knowledge_panel,
    register_knowledge_graph,
)
from observatory.knowledge_visual import article_nodes, focused_graph, knowledge_figure


class GraphService:
    def __init__(self):
        self.calls = []
        self.total = 12
        self.fail = False

    def knowledge_graph(self, filters, offset=0, limit=5, **kwargs):
        self.calls.append((filters.model_dump(mode="json"), offset, limit, kwargs))
        if self.fail:
            raise RuntimeError("postgres://secret:password@private-db")
        records, nodes, edges = [], [], []
        for index in range(offset, min(offset + limit, self.total)):
            rid = f"r{index}"
            records.append({"record_id": rid, "title": f"Article {index}", "sponsor": "source name", "publisher": "News", "date": "2025-01-01"})
            for suffix, kind, label, properties in [
                ("a", "Article", f"Article {index}", {"record_id": rid}),
                ("v", "TextVersion", f"Text version {index}", {"version_id": f"v{index}"}),
                ("n", "Annotation", "Historical solution annotation", {"review_status": "unreviewed", "scope_status": "prior_or_unavailable_body"}),
            ]:
                nodes.append({"id": f"{rid}-{suffix}", "type": kind, "label": label, "properties": properties, "record_ids": [rid]})
            edges.append({"id": f"{rid}-edge", "source": f"{rid}-a", "target": f"{rid}-v", "predicate": "has_text_version", "label": "Has text version", "record_ids": [rid],
                          "provenance": {"record_id": rid, "version_id": f"v{index}", "field": "body", "method": "import", "status": "source_record"}})
        return {"schema_version": "1", "nodes": nodes, "edges": edges, "records": records,
                "coverage": {"total_records": self.total, "shown_records": len(records), "offset": offset, "limit": limit},
                "warnings": [{"code": "legacy", "message": "Historical annotations need review."}],
                "predicate_definitions": {"has_text_version": {"description": "Links the article to its immutable stored text version."}}}


@pytest.fixture
def knowledge_app():
    service = GraphService()
    app = Dash(__name__)
    app.layout = html.Div([dcc.Store(id="native-network-data"), dcc.RadioItems(id="native-view", value="relationships"), knowledge_panel()])
    register_knowledge_graph(app, service, True)
    return app, app.server.test_client(), service


def values(**overrides):
    return {"native-view.value": "relationships", "native-network-data.data": {"filters": {"dataset": "native"}},
            "knowledge-offset.data": 0, "knowledge-focus.value": "r0-a",
            "knowledge-layer.value": "sources", "knowledge-annotation.value": None, **overrides}


def explore(bundle, data=None, changed="native-view.value"):
    app, client, _ = bundle
    return callback(app, client, "knowledge-inspector.children", data or values(), changed)


def test_graph_is_lazy_and_missing_service_is_safe(knowledge_app):
    result = explore(knowledge_app, values(**{"native-view.value": "overview"}))
    assert knowledge_app[2].calls == []
    assert result["knowledge-focus"]["options"] == []


def test_native_disclosure_state_opens_detail_graph_only_on_native_data_page():
    service = GraphService()
    app = Dash(__name__)
    app.layout = html.Div([
        dcc.Store(id="native-network-data"), dcc.Store(id="article-provenance-open", data=False),
        dcc.Location(id="page-location"), dcc.RadioItems(id="active-dataset", value="native"),
        dcc.RadioItems(id="native-view", value="relationships"), knowledge_panel(),
    ])
    register_knowledge_graph(app, service, True, require_open=True)
    bundle = app, app.server.test_client(), service
    scope = {"page-location.pathname": "/data", "active-dataset.value": "native"}
    closed = explore(bundle, values(**scope, **{"article-provenance-open.data": False}),
                     "article-provenance-open.data")
    assert service.calls == [] and closed["knowledge-focus"]["options"] == []
    opened = explore(bundle, values(**scope, **{"article-provenance-open.data": True}),
                     "article-provenance-open.data")
    assert len(service.calls) == 1 and len(opened["knowledge-focus"]["options"]) == 5
    explore(bundle, values(**{"article-provenance-open.data": True, "page-location.pathname": "/query",
                             "active-dataset.value": "native"}), "page-location.pathname")
    assert len(service.calls) == 1


def test_page_focus_has_typed_nodes_provenance_and_record_link(knowledge_app):
    result = explore(knowledge_app)
    assert len(result["knowledge-focus"]["options"]) == 5
    assert result["knowledge-focus"]["value"] == "r0-a"
    assert "1–5 of 12" in result["knowledge-coverage"]["children"]
    assert result["knowledge-next"]["disabled"] is False
    anchors = [node["props"] for node in component_tree(result["knowledge-inspector"]) if node.get("type") == "A"]
    assert any(item["href"] == "/records/r0" for item in anchors)
    custom = [item for trace in result["knowledge-graph"]["figure"]["data"] for item in trace.get("customdata", [])]
    assert {item["id"] for item in custom} == {"r0-a", "r0-v", "r0-edge"}
    assert result["knowledge-graph"]["style"]["height"] == f"{result['knowledge-graph']['figure']['layout']['height']}px"
    assert result["knowledge-graph"]["figure"]["layout"]["height"] >= 520
    assert result["knowledge-graph"]["style"]["minWidth"] == "100%"
    assert result["knowledge-annotation-wrap"]["style"] == {"display": "none"}
    assert "source view shows 2 nodes and 1 typed relations" in result["knowledge-coverage"]["children"]
    assert "full graph (3 nodes, 1 relations)" in result["knowledge-coverage"]["children"]


def test_relation_inspector_explains_source_and_definition(knowledge_app):
    data = values(**{"knowledge-graph.clickData": {"points": [{"customdata": {"kind": "edge", "id": "r0-edge", "provenance": "forged"}}]}})
    result = explore(knowledge_app, data, "knowledge-graph.clickData")
    text = json.dumps(result["knowledge-inspector"])
    assert "immutable stored text version" in text
    assert "Relation provenance" in text and "Source record" in text
    assert "forged" not in text
    assert result["knowledge-selection"]["data"] == {"kind": "edge", "id": "r0-edge"}


@pytest.mark.parametrize("selection", [
    {"kind": "node", "id": "forged", "label": "DO NOT SHOW"},
    {"kind": "edge", "id": "r1-edge"},
    {"kind": "node", "id": ["r0-a"]},
    {"kind": "fake", "id": "r0-a"}, None,
])
def test_forged_or_other_article_selection_is_rejected(knowledge_app, selection):
    data = values(**{"knowledge-graph.clickData": {"points": [{"customdata": selection}]}})
    result = explore(knowledge_app, data, "knowledge-graph.clickData")
    assert result["knowledge-selection"]["data"] is None
    assert "DO NOT SHOW" not in json.dumps(result)


def test_historical_annotation_status_is_visible(knowledge_app):
    data = values(**{"knowledge-layer.value": "annotations", "knowledge-annotation.value": "r0-n",
                     "knowledge-graph.clickData": {"points": [{"customdata": {"kind": "node", "id": "r0-n"}}]}})
    result = explore(knowledge_app, data, "knowledge-graph.clickData")
    assert "current version not linked" in json.dumps(result["knowledge-inspector"])
    assert "Unreviewed" in json.dumps(result["knowledge-inspector"])


def test_page_navigation_resets_selection_and_is_bounded(knowledge_app):
    data = values(**{"knowledge-selection.data": {"kind": "node", "id": "r0-a"}})
    result = explore(knowledge_app, data, "knowledge-next.n_clicks")
    assert result["knowledge-offset"]["data"] == 5
    assert result["knowledge-focus"]["value"] == "r5-a"
    assert result["knowledge-selection"]["data"] is None
    result = explore(knowledge_app, values(**{"knowledge-offset.data": 1000}), "knowledge-next.n_clicks")
    assert result["knowledge-offset"]["data"] == 10
    assert result["knowledge-next"]["disabled"] is True


def test_filters_preserved_and_new_scope_clears_selection(knowledge_app):
    filters = {"dataset": "native", "sponsors": ["A"], "publishers": ["B"], "date_from": "2024-01-01", "include_unknown_dates": False, "labels": ["L"], "record_ids": ["r0"], "keywords": ["K"], "platforms": []}
    data = values(**{"native-network-data.data": {"filters": filters}, "knowledge-offset.data": 10,
                     "knowledge-selection.data": {"kind": "node", "id": "r0-a"}})
    result = explore(knowledge_app, data, "native-network-data.data")
    assert result["knowledge-offset"]["data"] == 0
    assert result["knowledge-selection"]["data"] is None
    assert knowledge_app[2].calls[0][0] == {**filters, "date_to": None}


@pytest.mark.parametrize("filters", [{"dataset": "social"}, {"date_from": "invalid"}])
def test_bad_scope_does_not_fetch(knowledge_app, filters):
    explore(knowledge_app, values(**{"native-network-data.data": {"filters": filters}}))
    assert knowledge_app[2].calls == []


def test_service_failure_does_not_disclose_connection(knowledge_app):
    knowledge_app[2].fail = True
    result = explore(knowledge_app)
    assert "temporarily unavailable" in json.dumps(result)
    assert "password" not in json.dumps(result)


def test_export_rebuilds_server_graph_and_names_page_scope(knowledge_app):
    app, client, service = knowledge_app
    data = values(**{"knowledge-export.n_clicks": 1, "knowledge-offset.data": 5,
                     "native-network-data.data": {"filters": {"dataset": "native", "sponsors": ["A"]}, "nodes": [{"id": "forged"}]}})
    result = callback(app, client, "knowledge-download.data", data, "knowledge-export.n_clicks")
    graph = json.loads(result["knowledge-download"]["data"]["content"])
    assert graph["export_scope"] == "current article page under current filters"
    assert graph["coverage"]["offset"] == 5
    assert len(graph["records"]) == 5
    assert "forged" not in json.dumps(graph)
    assert service.calls[-1][0]["sponsors"] == ["A"]


def test_empty_filter_scope_and_forged_focus_are_safe(knowledge_app):
    result = explore(knowledge_app, values(**{"knowledge-focus.value": "forged"}), "knowledge-focus.value")
    assert result["knowledge-focus"]["value"] == "r0-a"
    knowledge_app[2].total = 0
    result = explore(knowledge_app)
    assert result["knowledge-focus"]["value"] is None
    assert result["knowledge-next"]["disabled"] is True


def test_graph_labels_escape_markup_and_unknown_focus_has_no_nodes():
    graph = {"nodes": [{"id": "a", "type": "Article", "label": "<script>x</script>", "record_ids": ["a"]}], "edges": []}
    assert focused_graph(graph, "forged") == {"nodes": [], "edges": []}
    figure = knowledge_figure(graph).to_plotly_json()
    assert figure["data"][0]["text"] == ["&lt;script&gt;x&lt;/script&gt;"]


@pytest.mark.parametrize("click", ["bad", {"points": {}}, {"points": ["bad"]}, {"points": None}, {"points": []}])
def test_malformed_clicks_fail_closed(knowledge_app, click):
    result = explore(knowledge_app, values(**{"knowledge-graph.clickData": click}), "knowledge-graph.clickData")
    assert result["knowledge-selection"]["data"] is None


def test_keyboard_selection_is_scoped_and_validated(knowledge_app):
    selection = json.dumps({"kind": "edge", "id": "r0-edge"}, sort_keys=True)
    result = explore(knowledge_app, values(**{"knowledge-item.value": selection}), "knowledge-item.value")
    assert result["knowledge-item"]["value"] == selection
    assert "Relation provenance" in json.dumps(result["knowledge-inspector"])
    assert len(result["knowledge-item"]["options"]) == 3
    forged = json.dumps({"kind": "node", "id": "r1-a"})
    result = explore(knowledge_app, values(**{"knowledge-item.value": forged}), "knowledge-item.value")
    assert result["knowledge-selection"]["data"] is None


def test_internal_attachment_links_and_source_link_policy():
    assert _source_url("/records/r0/attachments/pdf1", True) == "/records/r0/attachments/pdf1"
    assert _source_url("/records/r0/attachments/pdf1", False) == ""
    assert _source_url("/records/r0/attachments/%2E%2E", True) == ""
    assert _source_url("/records/r0/attachments/pdf%5Cname", True) == ""
    assert _source_url("/other/path", True) == ""
    assert _source_url("javascript:alert(1)", True) == ""


def test_long_identifiers_fold_and_candidate_status_reads_plainly():
    props = _properties({"body_hash": "a" * 64, "version_id": "v1", "identity_status": "source_candidate_not_resolved"}, True)
    direct = props.children[0].to_plotly_json()
    assert "a" * 64 not in str(direct)
    assert "identity not reviewed" in str(direct)
    assert props.children[1].children[0].children == "Technical details"


def test_article_focus_options_follow_source_page_order():
    graph = {"nodes": [{"id": "z", "type": "Article", "record_ids": ["a"]}, {"id": "a", "type": "Article", "record_ids": ["b"]}],
             "records": [{"record_id": "b"}, {"record_id": "a"}]}
    assert [node["id"] for node in article_nodes(graph)] == ["a", "z"]


def test_sources_hide_annotation_selections_and_keyboard_options(knowledge_app):
    result = explore(knowledge_app, values(**{
        "knowledge-item.value": json.dumps({"kind": "node", "id": "r0-n"}),
    }), "knowledge-item.value")
    assert result["knowledge-selection"]["data"] is None
    assert all("r0-n" not in option["value"] for option in result["knowledge-item"]["options"])


@pytest.mark.parametrize("trigger", ["knowledge-layer.value", "knowledge-annotation.value"])
def test_changing_layer_or_annotation_clears_selection(knowledge_app, trigger):
    result = explore(knowledge_app, values(**{
        "knowledge-layer.value": "annotations", "knowledge-annotation.value": "r0-n",
        "knowledge-selection.data": {"kind": "node", "id": "r0-a"},
    }), trigger)
    assert result["knowledge-selection"]["data"] is None
    assert result["knowledge-annotation-wrap"]["style"] == {"display": "block"}


def _add_second_annotation(service, monkeypatch):
    original = service.knowledge_graph

    def graph_with_annotations(*args, **kwargs):
        graph = original(*args, **kwargs)
        for suffix, kind, label in [("n2", "Annotation", "Second annotation"),
                                    ("l", "Label", "First category"), ("l2", "Label", "Second category")]:
            graph["nodes"].append({"id": f"r0-{suffix}", "type": kind, "label": label, "record_ids": ["r0"], "properties": {}})
        for suffix in ("", "2"):
            graph["edges"].extend([
                {"id": f"r0-has{suffix}", "source": "r0-a", "target": f"r0-n{suffix}",
                 "predicate": "has_annotation_record", "record_ids": ["r0"]},
                {"id": f"r0-label{suffix}", "source": f"r0-n{suffix}", "target": f"r0-l{suffix}",
                 "predicate": "assigns_label", "record_ids": ["r0"]},
            ])
        return graph

    monkeypatch.setattr(service, "knowledge_graph", graph_with_annotations)


def test_historical_view_shows_one_version_and_validates_selection(knowledge_app, monkeypatch):
    _add_second_annotation(knowledge_app[2], monkeypatch)
    result = explore(knowledge_app, values(**{
        "knowledge-layer.value": "annotations", "knowledge-annotation.value": "r0-n2",
        "knowledge-item.value": json.dumps({"kind": "node", "id": "r0-l"}),
    }), "knowledge-item.value")
    assert result["knowledge-annotation"]["value"] == "r0-n2"
    assert len(result["knowledge-annotation"]["options"]) == 2
    ids = {json.loads(option["value"])["id"] for option in result["knowledge-item"]["options"]}
    assert ids == {"r0-a", "r0-n2", "r0-l2", "r0-has2", "r0-label2"}
    assert result["knowledge-selection"]["data"] is None
    assert "historical label view shows 3 nodes and 2 typed relations" in result["knowledge-coverage"]["children"]
    assert "full graph (6 nodes, 5 relations)" in result["knowledge-coverage"]["children"]


def test_invalid_annotation_version_uses_current_article_option(knowledge_app, monkeypatch):
    _add_second_annotation(knowledge_app[2], monkeypatch)
    result = explore(knowledge_app, values(**{
        "knowledge-layer.value": "annotations", "knowledge-annotation.value": "r1-n",
        "knowledge-selection.data": {"kind": "node", "id": "r0-a"},
    }), "knowledge-annotation.value")
    assert result["knowledge-annotation"]["value"] == "r0-n"
    assert result["knowledge-selection"]["data"] is None


def test_missing_annotations_are_explicit_without_inferred_nodes(knowledge_app, monkeypatch):
    original = knowledge_app[2].knowledge_graph

    def without_annotations(*args, **kwargs):
        graph = original(*args, **kwargs)
        graph["nodes"] = [node for node in graph["nodes"] if node["type"] != "Annotation"]
        return graph

    monkeypatch.setattr(knowledge_app[2], "knowledge_graph", without_annotations)
    result = explore(knowledge_app, values(**{"knowledge-layer.value": "annotations"}))
    assert result["knowledge-annotation"]["value"] is None
    assert result["knowledge-annotation"]["options"] == []
    assert "No historical annotations are available" in json.dumps(result["knowledge-warnings"])
    assert all(json.loads(option["value"])["id"] == "r0-a" for option in result["knowledge-item"]["options"])


def test_annotation_layer_does_not_narrow_page_export(knowledge_app, monkeypatch):
    _add_second_annotation(knowledge_app[2], monkeypatch)
    app, client, _ = knowledge_app
    result = callback(app, client, "knowledge-download.data", values(**{
        "knowledge-export.n_clicks": 1, "knowledge-layer.value": "annotations",
        "knowledge-annotation.value": "r0-n2",
    }), "knowledge-export.n_clicks")
    graph = json.loads(result["knowledge-download"]["data"]["content"])
    ids = {node["id"] for node in graph["nodes"]}
    assert {"r0-n", "r0-n2", "r0-l", "r0-l2", "r0-v", "r1-a"} <= ids


def test_friendly_category_captions_preserve_distinct_schemes_and_full_hover():
    nodes = [
        {"id": name, "type": "Label", "label": "ff labels.infrastructure and production", "record_ids": ["r0"],
         "properties": {"label_key": "ff_labels.infrastructure_and_production", "scheme": f"unknown-codebook:{name}"}}
        for name in ("claims-original", "claims-v2")
    ]
    figure = knowledge_figure({"nodes": nodes, "edges": []}).to_plotly_json()
    traces = figure["data"]
    assert [text for trace in traces for text in trace["text"]] == [
        "Infrastructure and<br>production<br>claims-original", "Infrastructure and<br>production<br>claims-v2"]
    assert [item for trace in traces for item in trace["customdata"]] == [
        {"kind": "node", "id": "claims-original"}, {"kind": "node", "id": "claims-v2"}]
    assert "ff_labels.infrastructure_and_production" in traces[0]["hovertext"][0]
    assert "unknown-codebook:claims-original" in traces[0]["hovertext"][0]
    assert nodes[0]["properties"]["label_key"] == "ff_labels.infrastructure_and_production"
    assert all(trace["textfont"]["size"] >= 12 for trace in traces)
    assert figure["layout"]["legend"]["font"]["size"] >= 12
