"""Complete-corpus association counts are exact witnessed source projections."""

import hashlib
import json
from copy import deepcopy

import pytest

from observatory.analytics import sponsor_publisher_matrix
from observatory.knowledge_graph import build_graph
from observatory.knowledge_map import (
    SUMMARY_PREDICATE,
    build_collection_map,
    validate_collection_map,
)


def article(record_id, sponsor="exxonmobil", publisher="The Washington Post", *, body="An advertisement.", **updates):
    result = {"record_id": record_id, "version_id": "version-" + record_id, "dataset": "native",
              "title": "Article " + record_id, "date": "2020-04-01", "sponsor": sponsor,
              "publisher": publisher, "body": body, "body_hash": hashlib.sha256(body.encode()).hexdigest(),
              "retrievable": bool(body), "url": "https://example.org/" + record_id,
              "archive_url": "https://archive.org/" + record_id, "annotations": [], "issues": []}
    result.update(updates)
    return result


def fixture():
    return [article("r1"), article("r2", body="", retrievable=False),
            article("r3", publisher="The New York Times"),
            article("r4", sponsor="ExxonMobil"), article("r5", sponsor="api"),
            article("r6", sponsor="cera"), article("r7", sponsor=""),
            article("r8", publisher=""), article("r9", sponsor="", publisher="")]


def edge_source_values(graph, edge):
    nodes = {node["id"]: node for node in graph["nodes"]}
    return tuple(nodes[edge[field]]["properties"]["source_value"] for field in ("source", "target"))


def test_complete_association_counts_match_independent_source_cross_tab():
    rows = fixture()
    graph = build_collection_map(rows)
    counts = {edge_source_values(graph, edge): edge["count"] for edge in graph["summary_edges"]}
    assert counts == {("exxonmobil", "The Washington Post"): 2,
                      ("exxonmobil", "The New York Times"): 1,
                      ("ExxonMobil", "The Washington Post"): 1,
                      ("api", "The Washington Post"): 1,
                      ("cera", "The Washington Post"): 1}
    matrix = sponsor_publisher_matrix(rows)
    sponsor_values = [item["value"] for item in matrix["sponsors"]]
    for (sponsor, publisher), count in counts.items():
        assert matrix["matrix"][sponsor_values.index(sponsor)][matrix["publishers"].index(publisher)] == count
    assert graph["coverage"]["total_records"] == 9
    assert graph["coverage"]["associated_records"] == 6
    assert graph["coverage"]["retrievable_records"] == 8
    assert graph["coverage"]["missing_sponsor_records"] == 2
    assert graph["coverage"]["missing_outlet_records"] == 2
    assert graph["coverage"]["truncated"] is False


def test_entity_record_counts_include_single_dimension_articles_and_empty_bodies():
    graph = build_collection_map(fixture())
    sponsors = {node["properties"]["source_value"]: node for node in graph["nodes"] if node["type"] == "SponsorCandidate"}
    outlets = {node["properties"]["source_value"]: node for node in graph["nodes"] if node["type"] == "Outlet"}
    assert sponsors["exxonmobil"]["record_count"] == 4
    assert sponsors["exxonmobil"]["record_ids"] == ["r1", "r2", "r3", "r8"]
    assert outlets["The Washington Post"]["record_count"] == 6
    assert all(node["record_count"] == 1 for node in graph["nodes"] if node["type"] == "Article")
    assert "(Unknown)" not in {node["label"] for node in graph["nodes"]}


def test_each_count_has_original_article_edges_record_and_version_witnesses():
    graph = build_collection_map(fixture())
    source_edges = {edge["id"]: edge for edge in graph["article_edges"]}
    records = {record["record_id"]: record for record in graph["records"]}
    for edge in graph["summary_edges"]:
        assert edge["predicate"] == SUMMARY_PREDICATE
        assert edge["provenance"]["method"] == "derived_from_article_source_fields"
        assert edge["count"] == len(edge["witnesses"]) == len(edge["record_ids"])
        for witness in edge["witnesses"]:
            assert records[witness["record_id"]]["version_id"] == witness["version_id"]
            assert {source_edges[identifier]["predicate"] for identifier in witness["source_edge_ids"]} == {"source_lists_sponsor", "published_in"}
            assert all(source_edges[identifier]["provenance"]["record_id"] == witness["record_id"]
                       and source_edges[identifier]["provenance"]["version_id"] == witness["version_id"]
                       for identifier in witness["source_edge_ids"])
    assert validate_collection_map(graph) == []


def test_candidate_ids_match_existing_source_graph_and_aliases_do_not_merge():
    rows = fixture()
    source_graph = build_graph(rows)
    graph = build_collection_map(rows)
    expected = {node["id"] for node in source_graph["nodes"] if node["type"] in {"Article", "SponsorCandidate", "Outlet"}}
    assert {node["id"] for node in graph["nodes"]} == expected
    sponsors = [node for node in graph["nodes"] if node["type"] == "SponsorCandidate"]
    assert len([node for node in sponsors if node["label"] == "ExxonMobil"]) == 2
    cera = next(node for node in sponsors if node["label"] == "CERAWeek")
    assert cera["properties"]["type_hint"] == "conference/event"
    assert cera["properties"]["identity_status"] == "source_candidate_not_resolved"


def test_all_nodes_and_all_relationships_are_returned_without_top_n_pruning():
    rows = [article(f"r{index:03}", sponsor=f"sponsor-{index}", publisher=f"outlet-{index}") for index in range(75)]
    graph = build_collection_map(rows)
    assert graph["counts"] == {"articles": 75, "sponsors": 75, "outlets": 75, "source_edges": 150, "summary_edges": 75}
    assert graph["coverage"]["shown_records"] == 75
    assert len(graph["nodes"]) == 225
    assert len(graph["records"]) == 75
    assert graph["coverage"]["truncated"] is False


def test_order_independent_deterministic_ids_witnesses_and_memberships():
    rows = fixture()
    assert build_collection_map(rows) == build_collection_map(reversed(rows))


@pytest.mark.parametrize("change", [{}, {"version_id": "different"}, {"body": "Different body"}, {"sponsor": "bp"}])
def test_any_duplicate_record_fails_instead_of_silently_coalescing(change):
    with pytest.raises(ValueError, match="Duplicate source record"):
        build_collection_map([article("r1"), article("r1", **change)])


@pytest.mark.parametrize("change", [
    {"record_id": "../private"}, {"version_id": "../private"},
    {"dataset": "social"}, {"countable": False}, {"active": False},
])
def test_invalid_or_ineligible_source_inputs_fail_closed(change):
    row = article("r1")
    row.update(change)
    with pytest.raises(ValueError):
        build_collection_map([row])


def test_filtered_selection_only_contains_supplied_records_and_counts():
    graph = build_collection_map([row for row in fixture() if row["publisher"] == "The New York Times"])
    assert graph["coverage"]["total_records"] == 1
    assert graph["summary_edges"][0]["record_ids"] == ["r3"]
    assert graph["records"][0]["record_id"] == "r3"
    assert graph["coverage"]["scope"] == "complete_supplied_selection"


def test_metadata_only_snapshot_does_not_report_omitted_body_as_a_hash_defect():
    rows = fixture()
    expected_hash = rows[0]["body_hash"]
    for row in rows:
        row.pop("body")
    graph = build_collection_map(rows)
    assert graph["coverage"]["total_records"] == 9
    assert graph["records"][0]["body_hash"] == expected_hash
    assert graph["records"][0]["body_hash_status"] == "stored_reference_not_checked_in_overview"
    assert not any(warning["code"] == "body_hash_unverified" for warning in graph["warnings"])
    assert "body bytes are not loaded" in " ".join(graph["limitations"])


def test_private_payloads_source_paths_and_annotations_are_not_serialized():
    row = article("r1", raw={"secret": "PRIVATE_SECRET"}, provenance=[{"path": "C:/PRIVATE_PATH"}],
                  annotations=[{"explanation": "PRIVATE_ANNOTATION", "labels": ["false_solutions"]}],
                  issues=[{"code": "body_partial_recovery", "detail": "PRIVATE_DETAIL"}])
    graph = build_collection_map([row])
    serialized = json.dumps(graph)
    assert "PRIVATE_" not in serialized
    assert "false_solutions" not in serialized
    assert all(node["type"] not in {"Annotation", "EvidenceSpan", "Label"} for node in graph["nodes"])
    assert any("not classify" in limitation for limitation in graph["limitations"])


def test_invalid_metadata_types_cannot_leak_private_nested_objects():
    row = article("r1", publisher={"secret": "PRIVATE_VALUE"}, sponsor={"path": "C:/PRIVATE_PATH"})
    graph = build_collection_map([row])
    assert graph["records"][0]["publisher"] == graph["records"][0]["sponsor"] == ""
    assert "PRIVATE_" not in json.dumps(graph)


def test_disabled_links_and_unsafe_references_are_omitted():
    row = article("r1")
    graph = build_collection_map([row], links_enabled=False)
    assert graph["records"][0]["url"] == graph["records"][0]["archive_url"] == ""
    assert "https://example.org" not in json.dumps(graph)
    row.update(url="https://user:password@example.org/private", archive_url="file:///C:/private.pdf")
    graph = build_collection_map([row])
    assert graph["records"][0]["url"] == graph["records"][0]["archive_url"] == ""
    assert "password" not in json.dumps(graph)


@pytest.mark.parametrize("mutation,match", [
    (lambda graph: graph["summary_edges"][0].update(count=99), "count lacks"),
    (lambda graph: graph["summary_edges"][0]["witnesses"][0].update(version_id="wrong"), "conflicting record/version"),
    (lambda graph: graph["summary_edges"][0]["witnesses"][0].update(source_edge_ids=["missing", "missing"]), "source edge witnesses"),
    (lambda graph: graph["summary_edges"][0].update(target="absent"), "endpoints"),
    (lambda graph: graph["nodes"][0].update(record_count=100), "article membership"),
])
def test_validator_rejects_tampered_summary_counts_or_provenance(mutation, match):
    graph = deepcopy(build_collection_map(fixture()))
    mutation(graph)
    with pytest.raises(ValueError, match=match):
        validate_collection_map(graph)


def test_empty_selection_is_a_complete_empty_map_with_schema_and_scope():
    graph = build_collection_map([])
    assert graph["nodes"] == graph["summary_edges"] == graph["article_edges"] == graph["records"] == []
    assert graph["coverage"]["total_records"] == 0
    assert graph["coverage"]["truncated"] is False
    assert graph["predicate_definitions"][SUMMARY_PREDICATE]["domain"] == ["SponsorCandidate"]
    assert "business relationship" in graph["predicate_definitions"][SUMMARY_PREDICATE]["description"]
