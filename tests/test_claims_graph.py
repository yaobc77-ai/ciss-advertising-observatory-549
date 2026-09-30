"""Synthetic public assignments keep category identity and original evidence."""

import copy
import json

import pytest
from test_knowledge_service import PRIVATE, graph_row

from observatory.knowledge_graph import build_graph, relationship_counts, validate_graph
from observatory.knowledge_visual import graph_layer, knowledge_figure


def assignment(row=None):
    row = row or graph_row()
    return {"candidate_key": "a" * 64, "review_version": "b" * 64,
            "taxonomy_version": "c" * 64, "run_id": "synthetic-run",
            "record_id": row["record_id"], "version_id": row["version_id"],
            "dataset": row["dataset"], "body_hash": row["body_hash"],
            "nc_id": "NC_1", "sc_id": "SC_1", "nc_definition": "A project is described.",
            "sc_definition": "Projects and technology.", "review_state": "automatic_unverified",
            "start": 0, "end": len(row["body"]), "quote": row["body"],
            "raw": PRIVATE, "reviewer": PRIVATE}


def matches(*claims):
    return {"available": True, "claims_version": "d" * 64,
            "records": [{"record_id": "a", "claims": list(claims)}] if claims else []}


def test_assignment_has_named_typed_path_to_taxonomy_and_original_text():
    row = graph_row()
    before = copy.deepcopy(row)
    baseline = build_graph([row])
    graph = build_graph([row], claims2=matches(assignment(row)))
    kinds = {node["type"] for node in graph["nodes"]}
    assert {"ClaimAssignment", "Subclaim", "Superclaim", "EvidenceSpan"} <= kinds
    predicates = {edge["predicate"] for edge in graph["edges"]}
    assert {"has_claim_assignment", "assigns_subclaim", "subclaim_of", "cites_claim_evidence", "located_in"} <= predicates
    assert PRIVATE not in json.dumps(graph)
    assert relationship_counts(graph) == relationship_counts(baseline)
    assert row == before and validate_graph(graph, rows=[row]) == []
    assert graph["claims2"]["shown_assignments"] == 1
    assert not graph["claims2"]["classification_completion_known"]


@pytest.mark.parametrize("changes", [
    {"record_id": "other"}, {"dataset": "social"}, {"version_id": "stale"},
    {"body_hash": "0" * 64}, {"quote": "Invented quote"}, {"start": True},
    {"nc_id": "SC_1"}, {"sc_id": "NC_1"}, {"nc_definition": None},
    {"sc_definition": ""}, {"review_state": "fact_checked"}, {"taxonomy_version": "unknown"},
])
def test_assignment_scope_id_and_exact_quote_fail_closed(changes):
    claim = assignment()
    claim.update(changes)
    with pytest.raises(ValueError):
        build_graph([graph_row()], claims2=matches(claim))


def test_assignment_outside_current_page_fails_closed():
    page = matches(assignment())
    page["records"][0]["record_id"] = "other"
    with pytest.raises(ValueError, match="outside"):
        build_graph([graph_row()], claims2=page)


def test_unmapped_subclaim_does_not_invent_a_parent_or_twelve_label():
    claim = assignment()
    claim.update(sc_id=None, sc_definition=None)
    graph = build_graph([graph_row()], claims2=matches(claim))
    assert not any(node["type"] == "Superclaim" for node in graph["nodes"])
    assert not any(edge["predicate"] == "subclaim_of" for edge in graph["edges"])
    old_labels = [node for node in build_graph([graph_row()])["nodes"] if node["type"] == "Label"]
    assert [node for node in graph["nodes"] if node["type"] == "Label"] == old_labels


def test_same_ids_in_different_taxonomies_remain_separate_and_quote_is_shared():
    first, second = assignment(), assignment()
    second.update(candidate_key="e" * 64, taxonomy_version="f" * 64,
                  review_state="human_supported", nc_definition="Another version of this definition.")
    graph = build_graph([graph_row()], claims2=matches(first, second))
    assert len([node for node in graph["nodes"] if node["type"] == "Subclaim"]) == 2
    assert len([node for node in graph["nodes"] if node["type"] == "Superclaim"]) == 2
    assert len([node for node in graph["nodes"] if node["type"] == "EvidenceSpan"]) == 1
    assert len([edge for edge in graph["edges"] if edge["predicate"] == "located_in"]) == 1
    assert validate_graph(graph, rows=[graph_row()]) == []


def test_no_published_results_leaves_source_graph_and_history_unchanged():
    baseline = build_graph([graph_row()])
    graph = build_graph([graph_row()], claims2=matches())
    assert graph["nodes"] == baseline["nodes"] and graph["edges"] == baseline["edges"]
    assert graph["claims2"]["shown_assignments"] == 0
    assert "not negative" in graph["claims2"]["coverage"]


def test_claim_view_shows_one_assignment_and_actual_subclaim_parent_and_quote():
    first, second = assignment(), assignment()
    second.update(candidate_key="e" * 64, nc_id="NC_2", nc_definition="Another category.")
    graph = build_graph([graph_row()], claims2=matches(first, second))
    chosen = next(node for node in graph["nodes"] if node["type"] == "ClaimAssignment"
                  and node["properties"]["candidate_key"] == first["candidate_key"])
    visible = graph_layer(graph, "claims2", chosen["id"])
    assert len([node for node in visible["nodes"] if node["type"] == "ClaimAssignment"]) == 1
    assert {node["type"] for node in visible["nodes"]} == {
        "Article", "ClaimAssignment", "Subclaim", "Superclaim", "EvidenceSpan", "TextVersion"}
    assert not any(node["type"] in {"Annotation", "Label"} for node in visible["nodes"])
    figure = knowledge_figure(visible)
    assert figure.layout.meta["canvas_width"] >= 930
    assert any("A project is described" in str(trace.hovertext) for trace in figure.data)


def test_selected_assignment_does_not_show_other_assignment_mapping_provenance():
    first, second = assignment(), assignment()
    second.update(candidate_key="e" * 64, run_id="A second run", review_state="human_supported")
    graph = build_graph([graph_row()], claims2=matches(first, second))
    chosen = next(node for node in graph["nodes"] if node["type"] == "ClaimAssignment"
                  and node["properties"]["candidate_key"] == first["candidate_key"])
    visible = graph_layer(graph, "claims2", chosen["id"])
    assert len([edge for edge in visible["edges"] if edge["predicate"] == "subclaim_of"]) == 1
    for edge in visible["edges"]:
        provenance = edge["provenance"]
        if edge["predicate"] == "located_in":
            assert "candidate_key" not in provenance and provenance["status"] == "location_verified"
        else:
            assert provenance["candidate_key"] == first["candidate_key"]
