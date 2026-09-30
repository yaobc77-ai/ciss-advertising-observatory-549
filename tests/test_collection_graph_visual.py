"""Check truthful complete-map views and interaction-preserving encodings."""

import hashlib
import math
import xml.etree.ElementTree as ET
from urllib.parse import unquote

import pytest

from observatory.collection_graph_visual import (
    METADATA_ONLY_COLOR,
    TEXT_READY_COLOR,
    entity_record_image,
    map_elements,
    map_layout,
    map_stylesheet,
    selection_item,
    visible_map,
)
from observatory.knowledge_map import build_collection_map


def sample_graph(total=23):
    body = "A sponsor is listed in the source."
    rows = [{"record_id": f"record-{index}", "version_id": f"version-{index}", "dataset": "native",
             "title": f"Article {index}", "date": None if index == 0 else "2021-04-03",
             "sponsor": "exxonmobil" if index < 20 else "api", "publisher": "The Washington Post" if index % 2 == 0 else "The New York Times",
             "body": body, "body_hash": hashlib.sha256(body.encode()).hexdigest(),
             "retrievable": True, "url": f"https://example.org/record/{index}", "archive_url": ""}
            for index in range(total)]
    return build_collection_map(rows)


def test_complete_overview_keeps_every_entity_and_counted_edge():
    graph = sample_graph()
    elements = map_elements(graph)
    nodes = [item for item in elements if "source" not in item["data"]]
    edges = [item for item in elements if "source" in item["data"]]
    assert len(nodes) == graph["counts"]["sponsors"] + graph["counts"]["outlets"]
    assert len(edges) == graph["counts"]["summary_edges"]
    assert sum(item["data"]["record_count"] for item in edges) == 23
    assert all(item["data"]["type"] != "Article" for item in nodes)
    assert all("record_ids" not in item["data"] and "provenance" not in item["data"] for item in elements)


def test_all_articles_are_available_without_five_record_cap():
    graph = sample_graph()
    elements = map_elements(graph, "articles")
    assert sum(item["data"].get("type") == "Article" for item in elements) == 23
    assert sum("source" in item["data"] for item in elements) == 46


def test_expanded_articles_follow_exact_witnessed_edge_records():
    graph = sample_graph()
    edge = graph["summary_edges"][0]
    anchor = {"kind": "edge", "id": edge["id"]}
    nodes, edges = visible_map(graph, "articles", anchor)
    ids = {rid for node in nodes if node["type"] == "Article" for rid in node["record_ids"]}
    assert ids == set(edge["record_ids"])
    assert len(edges) == 2 * edge["count"]


def test_selection_dimming_preserves_all_elements_and_uses_id_only():
    graph = sample_graph()
    sponsor = next(node for node in graph["nodes"] if node["type"] == "SponsorCandidate")
    selected = {"kind": "node", "id": sponsor["id"], "label": "Forged payment", "record_count": 9000}
    canonical, item = selection_item(graph, selected)
    assert canonical == {"kind": "node", "id": sponsor["id"]}
    assert item["label"] != "Forged payment"
    rules = map_stylesheet(graph, selected=canonical)
    assert any(rule["selector"] == "node" and rule["style"].get("opacity") == .4 for rule in rules)
    assert any(rule["style"].get("label") == "data(count_label)" for rule in rules)
    assert len(map_elements(graph)) == graph["counts"]["sponsors"] + graph["counts"]["outlets"] + graph["counts"]["summary_edges"]


@pytest.mark.parametrize("candidate", [None, {"kind": "node", "id": "forged"}, {"kind": "SQL", "id": "forged"}, {"id": []}])
def test_unknown_selection_does_not_create_graph_facts(candidate):
    assert selection_item(sample_graph(), candidate) == (None, None)


def test_deterministic_starting_positions_and_safe_layout_fallback():
    graph = sample_graph()
    assert map_elements(graph) == map_elements(graph)
    assert map_layout()["randomize"] is False
    assert map_layout("unapproved")["name"] == "cose"
    assert map_layout("preset")["animate"] is False
    assert map_elements(sample_graph(0)) == []


def decoded_svg(image):
    assert image.startswith("data:image/svg+xml;utf8,")
    return ET.fromstring(unquote(image.split(",", 1)[1]))


def test_entity_rings_show_exact_record_dots_and_text_availability_partition():
    graph = sample_graph()
    for record in graph["records"][:7]:
        record["retrievable"] = False
    record_lookup = {record["record_id"]: record for record in graph["records"]}
    node_lookup = {node["id"]: node for node in graph["nodes"]}
    for element in map_elements(graph):
        data = element["data"]
        if data.get("kind") != "node":
            continue
        node = node_lookup[data["id"]]
        ready = sum(record_lookup[rid]["retrievable"] for rid in node["record_ids"])
        svg = decoded_svg(data["record_image"])
        dots = svg.findall("{http://www.w3.org/2000/svg}circle")
        assert len(dots) == len(node["record_ids"]) == data["record_count"]
        assert svg.attrib["data-text-ready"] == str(ready) == str(data["text_ready_count"])
        assert int(svg.attrib["data-metadata-only"]) + ready == data["record_count"]
        assert data["metadata_only_count"] == data["record_count"] - ready
        assert all(dot.attrib["class"] == "record-dot" for dot in dots)
        assert "record_ids" not in data and "body" not in data and "provenance" not in data


@pytest.mark.parametrize("kind", ["SponsorCandidate", "Outlet"])
@pytest.mark.parametrize("count,ready", [(0, 0), (1, 0), (1, 1), (263, 226)])
def test_ring_image_handles_empty_full_and_partial_partitions(kind, count, ready):
    image = entity_record_image(kind, count, ready)
    svg = decoded_svg(image)
    paths = svg.findall("{http://www.w3.org/2000/svg}path")
    assert len(svg.findall("{http://www.w3.org/2000/svg}circle")) == count
    assert paths[0].attrib["stroke"] == METADATA_ONLY_COLOR
    assert len(paths) == (2 if ready else 1)
    if ready:
        assert paths[1].attrib["stroke"] == TEXT_READY_COLOR
        portion, perimeter = map(float, paths[1].attrib["stroke-dasharray"].split())
        assert portion / perimeter == pytest.approx(ready / count)
    assert image == entity_record_image(kind, count, ready)
    assert "base64" not in image and "viewBox" not in unquote(image)


@pytest.mark.parametrize("kind,count,ready", [
    ("<script>", 2, 1), ("Article", 1, 1), ("Outlet", -1, 0),
    ("Outlet", 1, 2), ("Outlet", 3, -1), ("Outlet", True, 1), ("Outlet", 1, "1"),
])
def test_ring_image_rejects_untrusted_types_and_invalid_count_partitions(kind, count, ready):
    with pytest.raises(ValueError):
        entity_record_image(kind, count, ready)


def test_long_entity_names_remain_complete_and_cannot_enter_svg_xml():
    graph = sample_graph()
    sponsor = next(node for node in graph["nodes"] if node["type"] == "SponsorCandidate")
    sponsor["label"] = "Long source category " * 8 + '<img src="https://example.org/private">'
    data = next(element["data"] for element in map_elements(graph)
                if element["data"]["id"] == sponsor["id"])
    assert sponsor["label"] in data["display_label"]
    assert "https://example.org/private" not in unquote(data["record_image"])
    assert decoded_svg(data["record_image"]).find("{http://www.w3.org/2000/svg}img") is None


def test_article_bucket_intersects_canonical_anchor_and_preserves_exact_source_paths():
    graph = sample_graph()
    edge = graph["summary_edges"][0]
    expected = set(edge["record_ids"][:2])
    requested = [*expected, "forged-record", "record-22"]
    anchor = {"kind": "edge", "id": edge["id"]}
    nodes, edges = visible_map(graph, "articles", anchor, record_ids=requested)
    actual = {rid for node in nodes if node["type"] == "Article" for rid in node["record_ids"]}
    assert actual == expected
    assert len(edges) == 2 * len(expected)
    assert len(map_elements(graph, record_ids=[])) == len(map_elements(graph))


def test_article_subset_rings_count_expanded_records_without_changing_canonical_nodes():
    graph = sample_graph()
    selected_records = ["record-0", "record-2"]
    original_counts = {node["id"]: node["record_count"] for node in graph["nodes"]}
    elements = map_elements(graph, "articles", record_ids=selected_records)
    for element in elements:
        data = element["data"]
        if data.get("kind") != "node" or data["type"] == "Article":
            continue
        assert data["record_count"] == 2
        assert len(decoded_svg(data["record_image"]).findall("{http://www.w3.org/2000/svg}circle")) == 2
        assert data["scope_record_count"] == original_counts[data["id"]]
    assert original_counts == {node["id"]: node["record_count"] for node in graph["nodes"]}
    assert map_elements(graph, "articles", record_ids=[]) == []


def test_types_and_source_relations_have_distinct_truthful_encodings():
    graph = sample_graph()
    rules = map_stylesheet(graph)
    outlet = next(rule["style"] for rule in rules if rule["selector"] == ".Outlet")
    assert outlet["shape"] == "round-rectangle"
    assert not any(rule["selector"] in {"edge", ".association"}
                   and rule["style"].get("target-arrow-shape") for rule in rules)
    overview = map_elements(graph)
    assert all(element["data"]["count_label"].endswith("records")
               or element["data"]["count_label"] == "1 record"
               for element in overview if "source" in element["data"])
    article_edges = [element for element in map_elements(graph, "articles") if "source" in element["data"]]
    assert {edge["data"]["count_label"] for edge in article_edges} == {"Source lists sponsor", "Published in"}
    assert all("source-path" in edge["classes"] for edge in article_edges)
    assert any(rule["style"].get("background-image") == "data(record_image)" for rule in rules)


def test_full_corpus_article_markers_keep_space_without_record_truncation():
    graph = sample_graph(263)
    articles = [element for element in map_elements(graph, "articles")
                if element["data"].get("type") == "Article"]
    assert len(articles) == 263
    for first, second in zip(articles, [*articles[1:], articles[0]], strict=True):
        distance = math.dist(tuple(first["position"].values()), tuple(second["position"].values()))
        assert distance > first["data"]["size"]
