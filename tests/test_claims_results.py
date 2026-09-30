from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import MappingProxyType

import pytest

from observatory.claims_results import parse_saved_result
from observatory.claims_taxonomy import TaxonomyBundle


@pytest.fixture
def taxonomy(tmp_path):
    return TaxonomyBundle(
        source_directory=tmp_path,
        file_hashes=MappingProxyType({"fixture": "0" * 64}),
        bundle_fingerprint="1" * 64,
        subclaims=MappingProxyType({"NC_1": "Definition one", "NC_2": "Definition two"}),
        superclaims=MappingProxyType({"SC_1": "Parent one", "SC_2": "Parent two"}),
        raw_claim_superclaim_map=MappingProxyType({"NC_1": "SC_1", "NC_2": "SC_2"}),
        claim_superclaim_map=MappingProxyType({"NC_1": "SC_1", "NC_2": "SC_2"}),
        history=MappingProxyType({}), history_last_updated="2026-04-22T18:30:41", issues=(),
    )


def response(action="match_existing_category", **changes):
    value = {
        "action_type": action, "source_snippet": "A preserved source sentence.",
        "paragraph_number": "1", "matched_categories": ["NC_1"],
        "new_categories": [], "updated_categories": [],
        "super_claim": "SC_1", "rationale": "The sentence fits this candidate definition.",
    }
    value.update(changes)
    return value


def row(responses=None, *, content=None, raw_changes=None, message_changes=None, **changes):
    message = {
        "role": "assistant", "refusal": None,
        "content": content if content is not None else json.dumps({
            "responses": responses if responses is not None else [response()],
        }),
    }
    message.update(message_changes or {})
    raw = {"model": "recorded-test-model", "created": 1776864000,
           "choices": [{"finish_reason": "stop", "message": message}]}
    raw.update(raw_changes or {})
    value = {"original_id": "17", "text": "A preserved source sentence.",
             "raw_response": json.dumps(raw), "source_snippet": '["Misleading flat quote"]'}
    value.update(changes)
    return value


def codes(result):
    return {issue.code for issue in result.errors}


def test_valid_candidate_keeps_provenance_and_never_approves(taxonomy):
    saved = row()
    decoded = parse_saved_result(saved, taxonomy)
    assert decoded.method == "saved_upstream_llm_response"
    assert decoded.original_id == "17"
    assert decoded.recorded_model == "recorded-test-model"
    assert decoded.timestamp == "2026-04-22T13:20:00+00:00"
    assert decoded.raw_response_sha256 == hashlib.sha256(saved["raw_response"].encode()).hexdigest()
    assert decoded.input_text_sha256 == hashlib.sha256(saved["text"].encode()).hexdigest()
    assert decoded.taxonomy_bundle_fingerprint == taxonomy.bundle_fingerprint
    assert decoded.report_state == "candidate_results"
    assert decoded.review_required and not decoded.association_is_approved and not decoded.semantics_is_approved
    candidate = decoded.claims[0]
    assert (candidate.nc_id, candidate.sc_id, candidate.mapping_state) == ("NC_1", "SC_1", "mapped")
    assert candidate.source_snippet == "A preserved source sentence."
    assert candidate.source_snippet != "Misleading flat quote"
    assert candidate.raw_json_pointer == "/responses/0/matched_categories/0"
    assert decoded.raw_content_pointer == "/choices/0/message/content"
    assert json.loads(json.dumps(decoded.to_dict()))["claims"][0]["nc_id"] == "NC_1"


def test_each_action_uses_only_its_corresponding_category_column(taxonomy):
    decoded = parse_saved_result(row([
        response("create_new_category", new_categories=["<NC_2>Definition two<NC_2>"], super_claim="SC_2"),
        response("update_existing_category", updated_categories=["<NC_2>Definition two<NC_2>"], super_claim="SC_2"),
        response(matched_categories=["NC_1", "NC_2"], super_claim="SC_1"),
    ]), taxonomy)
    assert [(c.response_index, c.category_index, c.category_field, c.nc_id) for c in decoded.claims] == [
        (0, 0, "new_categories", "NC_2"), (1, 0, "updated_categories", "NC_2"),
        (2, 0, "matched_categories", "NC_1"), (2, 1, "matched_categories", "NC_2"),
    ]
    assert decoded.claims[-1].sc_id == "SC_2"
    assert "response_superclaim_mapping_conflict" in codes(decoded)


def test_repeated_category_occurrences_are_preserved_with_their_own_snippet(taxonomy):
    decoded = parse_saved_result(row([
        response(source_snippet="First source occurrence."),
        response(source_snippet="Second source occurrence."),
    ]), taxonomy)
    assert [c.source_snippet for c in decoded.claims] == ["First source occurrence.", "Second source occurrence."]
    assert [c.response_index for c in decoded.claims] == [0, 1]


def test_definition_and_mapping_drift_are_explicit_and_not_repaired(taxonomy):
    decoded = parse_saved_result(row([response(
        "create_new_category", new_categories=["<NC_1>Historical definition<NC_1>"],
        super_claim="<SC_2>Historical parent<SC_2>",
    )]), taxonomy)
    candidate = decoded.claims[0]
    assert candidate.historical_inline_definition == "Historical definition"
    assert candidate.current_candidate_definition == "Definition one"
    assert candidate.definition_drift
    assert candidate.sc_id == "SC_1" and candidate.raw_response_superclaim_id == "SC_2"
    assert candidate.current_candidate_superclaim_definition == "Parent one"
    assert candidate.raw_response_current_superclaim_definition == "Parent two"
    assert candidate.superclaim_definition_drift
    assert {"subclaim_definition_drift", "superclaim_definition_drift", "response_superclaim_mapping_conflict"} <= codes(decoded)
    assert candidate.report_state == decoded.report_state == "needs_review"


@pytest.mark.parametrize("value", ["NC_0", "NC_01", "nc_1", "SC_1", "NC_1 ", "<NC_1>text<NC_2>", "<NC_1> </NC_1>"])
def test_invalid_subclaim_identifiers_are_not_silently_repaired(taxonomy, value):
    decoded = parse_saved_result(row([response(matched_categories=[value])]), taxonomy)
    assert not decoded.claims
    assert decoded.report_state == "needs_review"
    assert "invalid_subclaim_identifier" in codes(decoded)


@pytest.mark.parametrize("mapping,state,code", [
    ({}, "unmapped", "unmapped_subclaim"),
    ({"NC_1": "SC_99"}, "dangling_superclaim", "dangling_superclaim"),
])
def test_missing_or_dangling_mapping_does_not_guess_a_parent(taxonomy, mapping, state, code):
    bundle = replace(taxonomy, raw_claim_superclaim_map=MappingProxyType(mapping),
                     claim_superclaim_map=MappingProxyType({}))
    decoded = parse_saved_result(row(), bundle)
    assert decoded.claims[0].sc_id == mapping.get("NC_1")
    assert decoded.claims[0].mapping_state == state
    assert code in codes(decoded)


def test_unknown_subclaim_remains_an_invalid_candidate(taxonomy):
    decoded = parse_saved_result(row([response(matched_categories=["NC_99"])]), taxonomy)
    assert decoded.claims[0].nc_id == "NC_99"
    assert decoded.claims[0].current_candidate_definition is None
    assert decoded.claims[0].mapping_state == "unknown_subclaim"
    assert "unknown_subclaim" in codes(decoded)
    assert decoded.claims[0].report_state == "needs_review"


def test_no_relevant_claim_with_categories_is_a_contradiction_not_a_class(taxonomy):
    decoded = parse_saved_result(row([response("no_relevant_claim")]), taxonomy)
    assert not decoded.claims
    assert decoded.response_states[0].state == "contradictory_no_relevant_claim"
    assert decoded.report_state == "needs_review"
    assert "no_relevant_claim_has_categories" in codes(decoded)


@pytest.mark.parametrize("responses,state", [
    ([response("no_relevant_claim", matched_categories=[])], "no_relevant_claim_unreviewed"),
    ([{"confirmation": "Nothing further was emitted."}], "meta_only"),
    ([response("custom_action")], "unknown_action"),
    ([response(matched_categories=[])], "no_claims_emitted"),
])
def test_no_label_and_meta_responses_are_distinct_and_never_negative(taxonomy, responses, state):
    decoded = parse_saved_result(row(responses), taxonomy)
    assert not decoded.claims
    assert decoded.response_states[0].state == state
    assert decoded.report_state == "needs_review" and decoded.review_required
    assert "negative" not in json.dumps(decoded.to_dict())


def test_empty_response_list_is_review_not_negative(taxonomy):
    decoded = parse_saved_result(row([]), taxonomy)
    assert decoded.report_state == "needs_review"
    assert "no_reviewable_claim_candidates" in codes(decoded)


@pytest.mark.parametrize("fence", ["```json\n{}\n```", "```\n{}\n```", "```JSON\r\n{}\r\n```"])
def test_whole_content_json_fences_are_supported(taxonomy, fence):
    content = json.dumps({"responses": [response()]})
    decoded = parse_saved_result(row(content=fence.format(content)), taxonomy)
    assert decoded.report_state == "candidate_results"


@pytest.mark.parametrize("content_template", [
    "A recorded introduction.\n```json\n{}\n```",
    "```json\n{}\n```\nA recorded completeness note.",
])
def test_single_fence_with_other_text_is_retained_as_review(taxonomy, content_template):
    content = json.dumps({"responses": [response()]})
    decoded = parse_saved_result(row(content=content_template.format(content)), taxonomy)
    assert decoded.report_state == "needs_review"
    assert len(decoded.claims) == 1
    assert "json_fence_surrounding_content" in codes(decoded)


@pytest.mark.parametrize("content", [
    "```json\n{}", "{\"responses\": [", "[]",
    '{"responses":[],"responses":[]}', '{"responses":[],"score":NaN}',
    '{"responses":[],"score":1e999}', '{"responses":{},"note":"bad"}',
])
def test_malformed_or_embedded_json_is_quarantined(taxonomy, content):
    decoded = parse_saved_result(row(content=content), taxonomy)
    assert decoded.report_state == "quarantined"
    assert not decoded.claims
    assert all(issue.severity == "quarantine" for issue in decoded.errors)


def test_multiple_explicit_json_fences_are_ambiguous_and_quarantined(taxonomy):
    first = json.dumps({"responses": [response()]})
    second = json.dumps({"responses": []})
    decoded = parse_saved_result(row(content=f"```json\n{first}\n```\n```json\n{second}\n```"), taxonomy)
    assert decoded.report_state == "quarantined"
    assert not decoded.claims and "multiple_json_fences" in codes(decoded)


def test_json_inside_unmarked_prose_is_not_scavenged(taxonomy):
    decoded = parse_saved_result(row(content='Some prose {"responses": []} more prose'), taxonomy)
    assert decoded.report_state == "quarantined"


def test_unknown_superclaim_and_meta_category_contradiction_are_explicit(taxonomy):
    decoded = parse_saved_result(row([
        response(super_claim="SC_99"),
        response("confirmation"),
    ]), taxonomy)
    assert len(decoded.claims) == 1
    assert {"unknown_response_superclaim", "response_superclaim_mapping_conflict", "meta_response_has_categories"} <= codes(decoded)


def test_empty_inline_definitions_and_lone_surrogates_are_rejected(taxonomy):
    invalid_definition = parse_saved_result(row([response(matched_categories=["<NC_1> <NC_1>"])]), taxonomy)
    assert not invalid_definition.claims and "invalid_subclaim_identifier" in codes(invalid_definition)
    invalid_unicode = parse_saved_result(row([response(source_snippet="\ud800")]), taxonomy)
    assert invalid_unicode.report_state == "quarantined"


@pytest.mark.parametrize("changes,code", [
    ({"message_changes": {"refusal": "I cannot perform this task."}}, "model_refusal"),
    ({"raw_changes": {"choices": [{"finish_reason": "length", "message": {}}]}}, "generation_not_complete"),
    ({"raw_changes": {"choices": []}}, "single_saved_choice_required"),
    ({"raw_changes": {"created": True}}, "invalid_recorded_timestamp"),
    ({"raw_changes": {"created": 10**30}}, "invalid_recorded_timestamp"),
    ({"raw_changes": {"model": None}}, "missing_recorded_model"),
    ({"message_changes": {"content": None}}, "missing_saved_content"),
    ({"raw_response": None}, "missing_raw_response"),
    ({"original_id": 17}, "invalid_original_id"),
    ({"text": ""}, "invalid_saved_input_text"),
])
def test_unavailable_truncated_or_refused_outputs_are_quarantined(taxonomy, changes, code):
    decoded = parse_saved_result(row(**changes), taxonomy)
    assert decoded.report_state == "quarantined" and not decoded.claims
    assert code in codes(decoded)


@pytest.mark.parametrize("responses", [
    [None], [response(matched_categories="NC_1")],
    [response(matched_categories=[42])], [response(action_type=3)],
    [{"action_type": "match_existing_category", "source_snippet": "A sentence."}],
])
def test_malformed_response_schema_quarantines_the_entire_row(taxonomy, responses):
    decoded = parse_saved_result(row([response(), *responses]), taxonomy)
    assert decoded.report_state == "quarantined" and not decoded.claims


def test_missing_quote_and_rationale_preserve_candidate_for_review(taxonomy):
    decoded = parse_saved_result(row([response(source_snippet="", rationale=None)]), taxonomy)
    assert decoded.claims[0].source_snippet is None
    assert decoded.claims[0].rationale is None
    assert {"missing_source_snippet", "missing_rationale"} <= codes(decoded)
    assert decoded.report_state == "needs_review"


def test_inputs_and_taxonomy_are_not_changed(taxonomy):
    saved = row()
    before = json.dumps(saved, sort_keys=True)
    parse_saved_result(saved, taxonomy)
    assert json.dumps(saved, sort_keys=True) == before
    assert taxonomy.subclaims["NC_1"] == "Definition one"


def test_types_are_explicit(taxonomy):
    with pytest.raises(TypeError):
        parse_saved_result([], taxonomy)
    with pytest.raises(TypeError):
        parse_saved_result(row(), {})
