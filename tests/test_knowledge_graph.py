"""Semantic graph contracts: source attribution, scope, identity and privacy."""

import copy
import hashlib
import json

import pytest

from observatory.knowledge_graph import (
    PREDICATES,
    build_graph,
    relationship_counts,
    validate_graph,
)


def row(**updates):
    body = "The company claims reduced emissions. A researcher disputes this claim."
    result = {
        "record_id": "r1", "version_id": "v1", "dataset": "native",
        "title": "An energy advertisement", "date": "2020-04-12",
        "sponsor": "exxonmobil", "publisher": "The Times", "body": body,
        "body_hash": hashlib.sha256(body.encode()).hexdigest(),
        "url": "https://example.org/story", "archive_url": "", "annotations": [],
    }
    result.update(updates)
    return result


def annotation(record, **updates):
    payload = {
        "version": "claims-original", "labels": ["emissions_reduction"],
        "body_sha256": record["body_hash"], "basis": "url_and_exact_body",
        "status": "historical_automatic_unverified",
    }
    payload.update(updates)
    return {"ordinal": 0, "payload": payload}


def nodes(graph, kind):
    return [node for node in graph["nodes"] if node["type"] == kind]


def edges(graph, predicate):
    return [edge for edge in graph["edges"] if edge["predicate"] == predicate]


def test_typed_article_centric_graph_has_provenance_and_dictionary():
    graph = build_graph([row()])
    assert {node["type"] for node in graph["nodes"]} == {
        "Article", "TextVersion", "SponsorCandidate", "Outlet", "SourceArtifact",
    }
    assert nodes(graph, "Article")[0]["properties"]["detail_url"] == "/records/r1"
    assert nodes(graph, "SponsorCandidate")[0]["label"] == "ExxonMobil"
    for edge in graph["edges"]:
        assert edge["provenance"]["record_id"] == "r1"
        assert edge["provenance"]["version_id"] == "v1"
        assert edge["predicate"] in PREDICATES
        assert edge["record_ids"] == ["r1"]
    assert graph["predicate_definitions"] == PREDICATES
    assert validate_graph(graph) == []
    json.dumps(graph)


def test_shared_source_values_share_candidate_not_article_or_provenance():
    graph = build_graph([row(), row(record_id="r2", version_id="v2")])
    assert len(nodes(graph, "Article")) == 2
    assert len(nodes(graph, "SponsorCandidate")) == 1
    assert nodes(graph, "SponsorCandidate")[0]["record_ids"] == ["r1", "r2"]
    assert len(edges(graph, "source_lists_sponsor")) == 2
    assert {edge["provenance"]["record_id"] for edge in edges(graph, "source_lists_sponsor")} == {"r1", "r2"}


def test_display_aliases_case_variants_and_datasets_never_merge():
    graph = build_graph([
        row(sponsor="ExxonMobil"), row(record_id="r2", version_id="v2"),
        row(record_id="r3", version_id="v3", dataset="social"),
        row(record_id="r4", version_id="v4", sponsor="Williams"),
        row(record_id="r5", version_id="v5", sponsor="Williams Companies"),
    ])
    assert len(nodes(graph, "SponsorCandidate")) == 5
    assert all(node["properties"]["identity_status"] == "source_candidate_not_resolved"
               for node in nodes(graph, "SponsorCandidate"))


@pytest.mark.parametrize("value", [None, "", " ", "(Unknown)", "UNKNOWN", "N/A"])
def test_unknown_values_are_missing_properties_not_shared_real_entities(value):
    graph = build_graph([row(sponsor=value, publisher=value)])
    assert not nodes(graph, "SponsorCandidate")
    assert not nodes(graph, "Outlet")
    article = nodes(graph, "Article")[0]
    assert article["properties"]["missing_sponsor"]
    assert article["properties"]["missing_outlet"]


def test_event_and_association_types_are_provisional_hints_only():
    graph = build_graph([row(sponsor="cera"), row(record_id="r2", version_id="v2", sponsor="api")])
    candidates = {node["label"]: node for node in nodes(graph, "SponsorCandidate")}
    assert candidates["CERAWeek"]["properties"]["type_hint"] == "conference/event"
    assert candidates["API"]["properties"]["type_hint"] == "industry association"
    assert all(node["properties"]["type_hint_status"] == "provisional_display_hint"
               for node in candidates.values())


def test_labels_are_annotation_assignments_not_direct_facts_or_invented_spans():
    source = row()
    source["annotations"] = [annotation(source)]
    graph = build_graph([source])
    assert len(nodes(graph, "Annotation")) == len(nodes(graph, "Label")) == 1
    assert len(edges(graph, "annotates")) == len(edges(graph, "assigns_label")) == 1
    assert not nodes(graph, "EvidenceSpan")
    label = nodes(graph, "Label")[0]["properties"]
    assert label["codebook_status"] == "unavailable"
    assert label["scheme"] == "unknown-codebook:claims-original"
    ann = nodes(graph, "Annotation")[0]["properties"]
    assert ann["scope_status"] == "current_text_hash_bound"
    assert ann["status"] == "historical_automatic_unverified"
    assert ann["evidence_status"] == "no_validated_spans"


def test_earlier_body_annotations_do_not_annotate_recovered_current_body():
    source = row()
    source["annotations"] = [annotation(source, body_sha256="a" * 64)]
    source["issues"] = [{"code": "historical_annotations_prior_body"}]
    graph = build_graph([source])
    assert len(nodes(graph, "Annotation")) == 1
    assert len(edges(graph, "has_annotation_record")) == 1
    assert not edges(graph, "annotates")
    assert nodes(graph, "Annotation")[0]["properties"]["scope_status"] == "prior_or_unavailable_body"
    assert any(item["code"] == "annotation_not_current_text" for item in graph["warnings"])


def test_private_recovery_annotations_are_never_read_implicitly():
    source = row(raw={"previous_body_annotations": [{"labels": ["false_solutions"]}],
                      "previous_body": "PRIVATE"})
    graph = build_graph([source])
    assert not nodes(graph, "Annotation")
    assert "PRIVATE" not in json.dumps(graph)


def test_unknown_codebooks_are_scoped_by_annotation_version():
    source = row()
    source["annotations"] = [annotation(source), {"ordinal": 1, "payload": {
        **annotation(source)["payload"], "version": "claims-calibrated",
    }}]
    graph = build_graph([source])
    assert len(nodes(graph, "Label")) == 2
    assert len(nodes(graph, "Annotation")) == 2


def valid_evidence(source):
    return {"start": 0, "end": 37, "quote": source["body"][:37],
            "body_sha256": source["body_hash"], "version_id": source["version_id"]}


def test_exact_character_evidence_is_bound_but_not_called_semantic_verification():
    source = row()
    source["annotations"] = [annotation(source, evidence=[valid_evidence(source)])]
    graph = build_graph([source])
    span = nodes(graph, "EvidenceSpan")[0]["properties"]
    assert span["quote"] == source["body"][:37]
    assert span["semantic_support_status"] == "not_verified"
    assert edges(graph, "located_in")
    assert nodes(graph, "Annotation")[0]["properties"]["evidence_status"] == "has_validated_spans"
    assert validate_graph(graph, rows=[source]) == []


@pytest.mark.parametrize("change", [
    {"quote": "Fabricated"}, {"start": -1}, {"end": 1000}, {"start": True},
    {"body_sha256": "a" * 64}, {"version_id": "old-version"}, {"version_id": None},
])
def test_invalid_or_unbound_evidence_is_omitted(change):
    source = row()
    source["annotations"] = [annotation(source, evidence=[{**valid_evidence(source), **change}])]
    graph = build_graph([source])
    assert not nodes(graph, "EvidenceSpan")
    assert any(item["code"] == "evidence_span_rejected" for item in graph["warnings"])


def test_old_version_id_rejects_current_hash_annotation_and_evidence():
    source = row()
    source["annotations"] = [annotation(source, version_id="old-v", evidence=[valid_evidence(source)])]
    graph = build_graph([source])
    assert not nodes(graph, "EvidenceSpan")
    assert not edges(graph, "annotates")


def test_body_hash_mismatch_blocks_evidence_even_when_payload_agrees():
    source = row(body_hash="a" * 64)
    source["annotations"] = [annotation(source, evidence=[valid_evidence(source)])]
    graph = build_graph([source])
    assert not nodes(graph, "EvidenceSpan")
    assert not edges(graph, "annotates")


def test_deterministic_order_duplicate_rows_and_conflicting_rows():
    first, second = row(), row(record_id="r2", version_id="v2")
    assert build_graph([first, second]) == build_graph([second, first])
    assert build_graph([first, first]) == build_graph([first])
    with pytest.raises(ValueError, match="Conflicting rows"):
        build_graph([first, row(title="Conflicting")])


def test_public_projection_omits_private_paths_explanations_and_arbitrary_fields():
    source = row(raw={"secret": "PRIVATE_RAW"}, provenance=[{"source": "PRIVATE_PATH"}],
                 secret="PRIVATE_TOP", issues=[{"code": "fine", "detail": "PRIVATE_ISSUE"}])
    source["annotations"] = [annotation(source, source="PRIVATE_SOURCE", explanations={"why": "PRIVATE_EXPLANATION"},
                                         token="PRIVATE_TOKEN", labels=["emissions_reduction", "C:/private/key.txt"])]
    payload = json.dumps(build_graph([source]))
    assert "PRIVATE" not in payload
    assert "private/key" not in payload
    assert "emissions_reduction" in payload


@pytest.mark.parametrize("url", ["file:///C:/secret", "javascript:alert(1)", "https://user:password@example.org/", "https://example.org/a\n"])
def test_unsafe_urls_are_never_graph_source_artifacts(url):
    graph = build_graph([row(url=url, archive_url=url)])
    assert not nodes(graph, "SourceArtifact")


def test_online_url_is_reference_and_reviewed_capture_is_hash_bound():
    source = row(archive_url="https://archive.example.org/copy")
    attachment = {"asset_id": "a1", "sha256": "f" * 64, "url": "/records/r1/attachments/a1",
                  "identity_status": "reviewed_local_capture", "source_path": "PRIVATE_PATH"}
    graph = build_graph([source], attachments_by_record={"r1": [attachment]})
    artifacts = {node["properties"]["kind"]: node["properties"] for node in nodes(graph, "SourceArtifact")}
    assert not artifacts["web_reference"]["immutable_capture"]
    assert not artifacts["archive_reference"]["immutable_capture"]
    assert artifacts["reviewed_pdf_capture"]["immutable_capture"]
    assert len(edges(graph, "derived_from")) == 1
    assert "PRIVATE" not in json.dumps(graph)
    hidden = build_graph([source], links_enabled=False, attachments_by_record={"r1": [attachment]})
    assert not nodes(hidden, "SourceArtifact")
    assert "https://" not in json.dumps(hidden)


@pytest.mark.parametrize("change", [{"url": "/records/r2/attachments/a1"}, {"sha256": "bad"},
                                     {"identity_status": "title_match_candidate"}])
def test_unreviewed_or_cross_record_attachment_is_omitted(change):
    attachment = {"asset_id": "a1", "sha256": "f" * 64, "url": "/records/r1/attachments/a1",
                  "identity_status": "reviewed_local_capture", **change}
    assert not edges(build_graph([row()], attachments_by_record={"r1": [attachment]}), "derived_from")


def test_validator_rejects_missing_endpoint_predicate_and_wrong_domain():
    original = build_graph([row()])
    for change, match in (({"source": "absent"}, "missing endpoint"),
                          ({"predicate": "owns"}, "Unknown graph predicate"),
                          ({"predicate": "has_evidence"}, "domain or range")):
        graph = copy.deepcopy(original)
        graph["edges"][0].update(change)
        with pytest.raises(ValueError, match=match):
            validate_graph(graph)


def test_validator_rechecks_evidence_against_supplied_original_text():
    source = row()
    source["annotations"] = [annotation(source, evidence=[valid_evidence(source)])]
    graph = build_graph([source])
    nodes(graph, "EvidenceSpan")[0]["properties"]["quote"] = "Fabricated"
    with pytest.raises(ValueError, match="Evidence"):
        validate_graph(graph, rows=[source])


def test_recovery_issue_remains_visible_when_current_annotations_are_empty():
    from datetime import datetime, timezone

    source = row(created_at=datetime(2026, 9, 25, 1, 2, tzinfo=timezone.utc), issues=[
        {"code": "historical_annotations_prior_body"}, {"code": "body_partial_recovery"},
        {"code": "PRIVATE_ARBITRARY_CODE"},
    ])
    graph = build_graph([source])
    version = nodes(graph, "TextVersion")[0]["properties"]
    assert version["stored_at"] == "2026-09-25T01:02:00+00:00"
    assert version["body_status"] == "partial_or_quality_limited"
    assert version["completeness"] == "not_established"
    assert version["quality_codes"] == ["body_partial_recovery"]
    assert any(warning["code"] == "historical_annotations_prior_body" for warning in graph["warnings"])
    assert "PRIVATE_ARBITRARY" not in json.dumps(graph)
    assert graph["identity_policy"]["entity_resolution"] == "exact_source_values_only_no_alias_merging"
    assert "SponsorCandidate" in graph["node_types"]


@pytest.mark.parametrize("code", ["body_source_partial", "body_source_completeness_unestablished"])
def test_saved_text_limit_survives_graph_projection_without_inventing_pdf(code):
    source = row(issues=[{"code": code, "source": "private-review", "detail": "private-review"}])
    graph = build_graph([source])
    version = nodes(graph, "TextVersion")[0]["properties"]
    assert version["quality_codes"] == [code]
    assert version["completeness"] == "not_established"
    assert version["body_status"] == "partial_or_quality_limited"
    assert not edges(graph, "derived_from")
    assert "private-review" not in json.dumps(graph)


def test_annotation_edges_keep_safe_source_digest_row_and_basis():
    source = row()
    source["annotations"] = [annotation(source, source_sha256="f" * 64, source_row=9,
                                         source="PRIVATE_PATH")]
    graph = build_graph([source])
    provenance = edges(graph, "assigns_label")[0]["provenance"]["annotation_source"]
    assert provenance == {"source_sha256": "f" * 64, "source_row": 9, "basis": "url_and_exact_body"}
    assert "PRIVATE_PATH" not in json.dumps(graph)


@pytest.mark.parametrize("change", [{"record_id": "another-record"}, {"version_id": "another-version"}])
def test_validator_rejects_wrong_edge_record_or_version_provenance(change):
    graph = build_graph([row()])
    relation = edges(graph, "has_text_version")[0]
    relation["provenance"].update(change)
    with pytest.raises(ValueError, match="provenance conflicts"):
        validate_graph(graph)


def test_article_path_counts_match_existing_matrix_without_counting_annotation_edges():
    from observatory.analytics import (
        sponsor_publisher_matrix,
        sponsor_publisher_matrix_from_counts,
    )

    sources = [row(), row(record_id="r2", version_id="v2"),
               row(record_id="r3", version_id="v3", sponsor="", publisher=""),
               row(record_id="r4", version_id="v4", sponsor="cera", publisher="The Post"),
               row(record_id="r5", version_id="v5", sponsor="ExxonMobil")]
    sources[0]["annotations"] = [annotation(sources[0], labels=["label_a", "label_b"],
                                            evidence=[valid_evidence(sources[0])])]
    graph = build_graph(sources)
    counts = relationship_counts(graph)
    assert sum(item["count"] for item in counts) == len(sources)
    assert {tuple(item.values()) for item in counts} >= {("exxonmobil", "The Times", 2), ("", "", 1)}
    assert sponsor_publisher_matrix_from_counts(counts) == sponsor_publisher_matrix(sources)
    assert not any(node["label"] == "(Unknown)" for node in graph["nodes"])


def test_graph_schema_export_cannot_mutate_the_module_predicate_dictionary():
    graph = build_graph([row()])
    graph["predicate_definitions"]["has_text_version"]["domain"].append("Wrong")
    assert PREDICATES["has_text_version"]["domain"] == ["Article"]


def test_malformed_metadata_is_not_elevated_to_current_version_binding():
    source = row(issues=None)
    source["annotations"] = [annotation(source, version_id={"not": "an identifier"},
                                         basis={"secret": "PRIVATE"})]
    graph = build_graph([source])
    assert not edges(graph, "annotates")
    assert nodes(graph, "Annotation")[0]["properties"]["basis"] == "not_recorded"
    assert "PRIVATE" not in json.dumps(graph)
