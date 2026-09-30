"""Clicked graph counts keep every source category and exact denominator."""

from copy import deepcopy

import pytest

from observatory.collection_graph_breakdown import (
    MISSING_COLOR,
    breakdown_figure,
    resolve_breakdown_slice,
    selection_breakdown,
)
from observatory.knowledge_map import build_collection_map


def article(identifier, sponsor="exxonmobil", outlet="The Washington Post", date="2020-04-01"):
    return {"record_id": identifier, "version_id": "v-" + identifier, "dataset": "native",
            "title": "Article " + identifier, "sponsor": sponsor, "publisher": outlet,
            "date": date, "retrievable": False}


def fixture():
    return [article("r1"), article("r2", date=None),
            article("r3", outlet="The New York Times", date="2021-01-01"),
            article("r4", sponsor="ExxonMobil"), article("r5", sponsor="api"),
            article("r6", sponsor=""), article("r7", outlet=""),
            article("r8", sponsor="", outlet="")]


def select_node(graph, kind, source_value):
    node = next(node for node in graph["nodes"] if node["type"] == kind
                and node["properties"].get("source_value") == source_value)
    return {"kind": "node", "id": node["id"]}


def select_edge(graph, sponsor="exxonmobil", outlet="The Washington Post"):
    s = select_node(graph, "SponsorCandidate", sponsor)["id"]
    o = select_node(graph, "Outlet", outlet)["id"]
    edge = next(edge for edge in graph["summary_edges"] if (edge["source"], edge["target"]) == (s, o))
    return {"kind": "edge", "id": edge["id"]}


def test_sponsor_donut_partitions_all_records_including_missing_outlet():
    graph = build_collection_map(fixture())
    selected = select_node(graph, "SponsorCandidate", "exxonmobil")
    detail = selection_breakdown(graph, selected)
    assert detail["total_records"] == 4
    assert detail["record_ids"] == ["r1", "r2", "r3", "r7"]
    assert detail["unknown_date_records"] == 1
    assert detail["dimension"] == "News outlets"
    assert [(item["label"], item["count"], item["share"]) for item in detail["categories"]] == [
        ("The Washington Post", 2, .5), ("The New York Times", 1, .25), ("Missing outlet", 1, .25),
    ]
    missing = detail["categories"][-1]
    assert missing["missing"] and missing["color"] == MISSING_COLOR
    assert missing["node_id"] is missing["edge_id"] is missing["selection"] is None
    assert missing["record_ids"] == ["r7"]
    assert sum(item["count"] for item in detail["categories"]) == detail["total_records"]


def test_outlet_donut_preserves_missing_sponsor_and_duplicate_display_aliases():
    graph = build_collection_map(fixture())
    detail = selection_breakdown(graph, select_node(graph, "Outlet", "The Washington Post"))
    assert detail["total_records"] == 5
    assert len(detail["categories"]) == 4
    by_source = {item["source_value"]: item for item in detail["categories"]}
    assert by_source["exxonmobil"]["count"] == 2
    assert by_source["ExxonMobil"]["count"] == 1
    assert by_source["exxonmobil"]["node_id"] != by_source["ExxonMobil"]["node_id"]
    assert by_source["exxonmobil"]["label"] != by_source["ExxonMobil"]["label"]
    assert by_source[""]["label"] == "Missing sponsor"
    assert by_source[""]["share"] == .2
    assert sum(item["share"] for item in detail["categories"]) == pytest.approx(1)


def test_summary_edge_uses_year_pie_and_separate_endpoint_denominators():
    graph = build_collection_map(fixture())
    detail = selection_breakdown(graph, select_edge(graph))
    assert detail["total_records"] == 2
    assert detail["dimension"] == "Publication years"
    assert [(item["label"], item["count"], item["share"]) for item in detail["categories"]] == [
        ("2020", 1, .5), ("Unknown date", 1, .5),
    ]
    endpoint = {item["type"]: item for item in detail["endpoint_shares"]}
    assert endpoint["SponsorCandidate"]["share"] == .5
    assert endpoint["SponsorCandidate"]["total_records"] == 4
    assert endpoint["Outlet"]["share"] == .4
    assert endpoint["Outlet"]["total_records"] == 5
    assert endpoint["Outlet"]["count"] == 2


def test_source_article_edge_and_article_nodes_can_be_inspected():
    graph = build_collection_map(fixture())
    article_node = next(node for node in graph["nodes"] if node["type"] == "Article" and node["record_ids"] == ["r1"])
    detail = selection_breakdown(graph, {"kind": "node", "id": article_node["id"]})
    assert detail["total_records"] == 1
    assert detail["categories"][0]["id"] == "date:2020"
    source_edge = next(edge for edge in graph["article_edges"] if edge["source"] == article_node["id"])
    edge_detail = selection_breakdown(graph, {"kind": "edge", "id": source_edge["id"]})
    assert edge_detail["record_ids"] == ["r1"]
    assert sorted(item["total_records"] for item in edge_detail["endpoint_shares"]) == [4, 5]


def test_filtered_snapshot_is_only_denominator_and_colors_are_local_and_reproducible():
    rows = fixture()
    original = build_collection_map(rows)
    filtered = build_collection_map([row for row in rows if row["record_id"] in {"r1", "r3"}])
    selected = select_node(filtered, "SponsorCandidate", "exxonmobil")
    detail = selection_breakdown(filtered, selected)
    assert detail["total_records"] == 2
    assert all(item["share"] == .5 for item in detail["categories"])
    original_detail = selection_breakdown(original, selected)
    assert detail["color_scope"] == "selected_breakdown_categories"
    assert detail == selection_breakdown(build_collection_map(reversed([row for row in rows if row["record_id"] in {"r1", "r3"}])), selected)
    assert original_detail["total_records"] == 4


def test_every_category_is_retained_instead_of_top_n_or_other_aggregation():
    graph = build_collection_map([article(f"r{index}", outlet=f"Outlet {index}") for index in range(50)])
    detail = selection_breakdown(graph, select_node(graph, "SponsorCandidate", "exxonmobil"))
    assert len(detail["categories"]) == 50
    assert all(item["count"] == 1 and item["share"] == .02 for item in detail["categories"])
    figure = breakdown_figure(detail)
    assert len(figure.data[0].values) == 50
    assert set(figure.data[0].text) == {""}
    assert len(set(figure.data[0].marker.colors)) == 50


def test_four_outlet_slices_have_four_distinct_colors():
    rows = [article(f"r{index}", outlet=f"Outlet {index}") for index in range(4)]
    graph = build_collection_map(rows)
    selected = select_node(graph, "SponsorCandidate", "exxonmobil")
    detail = selection_breakdown(graph, selected)
    assert len({category["color"] for category in detail["categories"]}) == 4
    assert detail == selection_breakdown(build_collection_map(reversed(rows)), selected)


def test_nineteen_sponsors_and_missing_have_twenty_distinct_swatches():
    rows = [article(f"r{index}", sponsor=f"Sponsor {index}") for index in range(19)] + [article("missing", sponsor="")]
    graph = build_collection_map(rows)
    selected = select_node(graph, "Outlet", "The Washington Post")
    detail = selection_breakdown(graph, selected)
    assert len(detail["categories"]) == 20
    assert len({category["color"] for category in detail["categories"]}) == 20
    assert detail["categories"][-1]["color"] == MISSING_COLOR
    assert detail == selection_breakdown(build_collection_map(reversed(rows)), selected)


def test_same_category_set_keeps_colors_when_counts_and_ranks_change():
    rows = [article("a", outlet="Outlet A"), article("b", outlet="Outlet B"), article("c", outlet="Outlet C")]
    before = build_collection_map(rows)
    after = build_collection_map([*rows, article("b2", outlet="Outlet B"), article("c2", outlet="Outlet C")])
    selected = select_node(before, "SponsorCandidate", "exxonmobil")
    before_colors = {category["id"]: category["color"] for category in selection_breakdown(before, selected)["categories"]}
    after_colors = {category["id"]: category["color"] for category in selection_breakdown(after, selected)["categories"]}
    assert before_colors == after_colors


@pytest.mark.parametrize("selection", [None, {}, {"kind": "node", "id": "missing"},
                                       {"kind": "edge", "id": "missing"}, {"kind": "private", "id": "missing"}])
def test_invalid_and_stale_client_selection_has_no_breakdown(selection):
    assert selection_breakdown(build_collection_map(fixture()), selection) is None
    assert selection_breakdown(build_collection_map([]), selection) is None


def test_client_selected_counts_and_properties_are_ignored():
    graph = build_collection_map(fixture())
    selected = select_node(graph, "SponsorCandidate", "exxonmobil")
    selected.update(record_count=999, record_ids=["secret"], label="Forged")
    detail = selection_breakdown(graph, selected)
    assert detail["total_records"] == 4 and detail["title"] == "ExxonMobil"
    assert detail["selection"] == {"kind": "node", "id": selected["id"]}


def test_pie_slice_is_resolved_against_its_parent_not_client_payload_or_other_category():
    graph = build_collection_map(fixture())
    selected = select_node(graph, "SponsorCandidate", "exxonmobil")
    detail = selection_breakdown(graph, selected)
    bucket = detail["categories"][0]
    assert resolve_breakdown_slice(graph, selected, bucket["id"]) == bucket
    assert resolve_breakdown_slice(graph, selected, "missing:Outlet")["record_ids"] == ["r7"]
    assert resolve_breakdown_slice(graph, selected, {"id": bucket["id"], "count": 999}) is None
    other = select_node(graph, "SponsorCandidate", "api")["id"]
    assert resolve_breakdown_slice(graph, selected, other) is None
    filtered = build_collection_map([article("r3", outlet="The New York Times")])
    assert resolve_breakdown_slice(filtered, selected, bucket["id"]) is None


def test_donut_uses_record_counts_ids_and_observed_shares_without_reordering():
    graph = build_collection_map(fixture())
    detail = selection_breakdown(graph, select_node(graph, "SponsorCandidate", "exxonmobil"))
    figure = breakdown_figure(detail, detail["categories"][0]["id"])
    trace = figure.data[0]
    assert trace.type == "pie" and trace.hole == .7 and trace.sort is False
    assert list(trace.values) == [2, 1, 1]
    assert list(trace.customdata) == [item["id"] for item in detail["categories"]]
    assert list(trace.text) == ["50%", "25%", "25%"]
    assert list(trace.pull) == [.06, 0, 0]
    assert figure.layout.clickmode == "event"
    assert figure.layout.transition.duration == 0


def test_no_records_renders_explicit_empty_state_without_fake_pie():
    for detail in (None, {"total_records": 0, "categories": [], "selection": {"kind": "node", "id": "empty"}}):
        figure = breakdown_figure(detail)
        assert not figure.data
        assert figure.layout.annotations[0].text == "No records in this selection"


def test_valid_zero_membership_entity_has_empty_partition_without_division_by_zero():
    graph = build_collection_map([])
    graph["nodes"] = [{"id": "sponsorcandidate:empty", "type": "SponsorCandidate", "label": "Empty sponsor",
                       "properties": {"source_value": "Empty sponsor"}, "record_ids": [], "record_count": 0}]
    detail = selection_breakdown(graph, {"kind": "node", "id": "sponsorcandidate:empty"})
    assert detail["total_records"] == 0
    assert detail["categories"] == detail["record_ids"] == detail["endpoint_shares"] == []
    assert not breakdown_figure(detail).data


def test_labels_are_escaped_before_plotly_rich_text_rendering():
    graph = build_collection_map([article("r1", outlet="<b>Outlet</b>")])
    detail = selection_breakdown(graph, select_node(graph, "SponsorCandidate", "exxonmobil"))
    assert breakdown_figure(detail).data[0].labels == ("&lt;b&gt;Outlet&lt;/b&gt;",)


@pytest.mark.parametrize("mutation", [
    lambda graph: graph["summary_edges"][0].update(count=999),
    lambda graph: graph["article_edges"][0]["provenance"].update(version_id="wrong"),
    lambda graph: graph["article_edges"][0].update(record_ids=["wrong"]),
])
def test_inconsistent_server_snapshot_cannot_generate_plausible_counts(mutation):
    graph = deepcopy(build_collection_map(fixture()))
    selected = select_node(graph, "SponsorCandidate", "exxonmobil")
    mutation(graph)
    with pytest.raises(ValueError):
        selection_breakdown(graph, selected)


def test_entity_membership_must_equal_its_source_path_membership():
    graph = deepcopy(build_collection_map(fixture()))
    selected = select_node(graph, "SponsorCandidate", "exxonmobil")
    sponsor = next(node for node in graph["nodes"] if node["id"] == selected["id"])
    sponsor["record_ids"].append("r8")
    sponsor["record_count"] += 1
    with pytest.raises(ValueError, match="Entity count membership"):
        selection_breakdown(graph, selected)


def test_missing_bucket_is_gray_without_adding_a_missing_entity_to_graph():
    graph = build_collection_map([article("r1", outlet="")])
    detail = selection_breakdown(graph, select_node(graph, "SponsorCandidate", "exxonmobil"))
    assert detail["categories"][0]["share"] == 1
    assert len(graph["nodes"]) == 2
    assert detail["categories"][0]["color"] == MISSING_COLOR
