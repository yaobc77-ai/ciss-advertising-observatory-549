"""Historical source states require exact provenance, not inferred negatives."""

import hashlib
import json
from copy import deepcopy

import pytest

from observatory.social_annotations import (
    SCHEME,
    SOCIAL_LABELS,
    SOCIAL_STATES,
    STATUS,
    parse_social_state_id,
    social_annotation_details,
    social_label_metadata,
    social_state_id,
    social_state_options,
)


def record():
    body = "Our source post mentions energy."
    digest = hashlib.sha256(body.encode()).hexdigest()
    values = dict.fromkeys(SOCIAL_LABELS, False)
    return {
        "dataset": "social", "record_id": "s1", "version_id": "v1",
        "body": body, "body_hash": digest,
        "annotations": [{"ordinal": 0, "payload": {
            "version": SCHEME, "status": STATUS,
            "basis": "supplied_source_post_id_and_exact_body",
            "taxonomy_mapping": "source_keys_preserved_no_native_label_mapping",
            "values": values, "labels": [], "source_sha256": "a" * 64,
            "source_row": 3, "body_sha256": digest,
            "explanations": {"green_explanation": "An old model explanation.", "explanation": None},
        }}],
    }


def assert_unknown(value, state):
    assert value["validation_state"] == state
    assert len(value["values"]) == 13
    assert all(item["value"] is None and item["state"] == "unknown" for item in value["values"])
    assert value["explanations"] == []
    assert "source_sha256" not in value["provenance"]


def test_recorded_false_is_distinct_from_missing_and_not_a_fact_finding():
    value = social_annotation_details(record())
    assert value["validation_state"] == "bound"
    assert all(item["value"] is False and item["state"] == "source_false" for item in value["values"])
    assert {item["level"] for item in value["values"]} == {"macro", "subcode"}
    assert sum(item["level"] == "macro" for item in value["values"]) == 2
    assert "not reviewed" in value["note"]
    assert value["provenance"] == {"record_id": "s1", "version_id": "v1",
                                    "body_hash": record()["body_hash"], "source_sha256": "a" * 64, "source_row": 3}


def test_true_states_use_exact_source_values_without_inferring_macro_hierarchy():
    row = record()
    payload = row["annotations"][0]["payload"]
    payload["values"]["renewable_energy"] = True
    payload["labels"] = ["renewable_energy"]
    by_key = {item["key"]: item for item in social_annotation_details(row)["values"]}
    assert by_key["renewable_energy"]["state"] == "source_true"
    assert by_key["green_binary"]["state"] == "source_false"


@pytest.mark.parametrize("entries", [None, [], {}, [{"payload": None}], [False], [{"payload": {"version": "claims-calibrated"}}]])
def test_no_usable_scheme_is_unknown(entries):
    row = record()
    row["annotations"] = entries
    assert_unknown(social_annotation_details(row), "missing")


@pytest.mark.parametrize("key,bad", [
    ("status", "reviewed"), ("basis", "inferred_from_title"), ("taxonomy_mapping", "native"),
    ("values", []), ("labels", ["green_binary"]), ("labels", None),
    ("source_sha256", "private/path"), ("source_sha256", "A" * 64),
    ("source_row", True), ("source_row", 0), ("source_row", -1), ("source_row", 3.0),
    ("body_sha256", "b" * 64), ("version_id", "old-version"),
])
def test_unbound_or_invalid_metadata_cannot_establish_false(key, bad):
    row = record()
    row["annotations"][0]["payload"][key] = bad
    assert_unknown(social_annotation_details(row), "invalid")


@pytest.mark.parametrize("bad", [0, 1, None, "false", [], {}])
def test_boolean_values_are_not_coerced(bad):
    row = record()
    row["annotations"][0]["payload"]["values"]["green_binary"] = bad
    assert_unknown(social_annotation_details(row), "invalid")


@pytest.mark.parametrize("mutation", ["missing_key", "extra_key", "old_wrapper_version"])
def test_partial_or_ambiguous_structure_stays_unknown(mutation):
    row = record()
    if mutation == "missing_key":
        del row["annotations"][0]["payload"]["values"]["green_binary"]
    elif mutation == "extra_key":
        row["annotations"][0]["payload"]["values"]["secret"] = False
    else:
        row["annotations"][0]["version_id"] = "old-v"
    assert_unknown(social_annotation_details(row), "invalid")


def test_duplicate_scheme_is_ambiguous_even_if_both_payloads_agree():
    row = record()
    row["annotations"].append(deepcopy(row["annotations"][0]))
    assert_unknown(social_annotation_details(row), "ambiguous")


@pytest.mark.parametrize("mutation", ["body_changed", "body_not_string", "bad_stored_hash"])
def test_stored_body_itself_must_match_its_hash(mutation):
    row = record()
    if mutation == "body_changed":
        row["body"] += " A change."
    elif mutation == "body_not_string":
        row["body"] = None
    else:
        row["body_hash"] = "invalid"
    assert_unknown(social_annotation_details(row), "body_mismatch")


def test_new_body_version_cannot_reuse_old_annotation():
    row = record()
    row["body"] += " New content."
    row["body_hash"] = hashlib.sha256(row["body"].encode()).hexdigest()
    assert_unknown(social_annotation_details(row), "invalid")


def test_public_projection_is_allowlisted_and_does_not_mutate_source():
    row = record()
    payload = row["annotations"][0]["payload"]
    row["raw"] = {"source": "PRIVATE-SENTINEL"}
    payload["source_path"] = "PRIVATE-SENTINEL"
    payload["explanations"]["unexpected_private_key"] = "PRIVATE-SENTINEL"
    payload["explanations"]["explanation"] = {"path": "PRIVATE-SENTINEL"}
    before = deepcopy(row)
    value = social_annotation_details(row)
    assert "PRIVATE-SENTINEL" not in json.dumps(value)
    assert value["explanations"] == [{"key": "green_explanation", "label": "Historical green-messaging explanation",
                                       "text": "An old model explanation.", "truncated": False}]
    assert row == before


def test_bounded_explanations_explicitly_report_truncation():
    row = record()
    row["annotations"][0]["payload"]["explanations"]["explanation"] = "A" * 2100
    item = social_annotation_details(row)["explanations"][-1]
    assert len(item["text"]) == 2000 and item["truncated"] is True


def test_escaped_legacy_explanation_is_marked_and_never_decoded_for_display():
    row = record()
    payload = row["annotations"][0]["payload"]
    payload["explanations"]["green_explanation"] = r"Old \u0000 result"
    payload["explanation_storage"] = {"scheme": "json-string-nul-v1", "escaped_fields": ["green_explanation"]}
    row["raw"] = {"source_string_storage": {"original_json_strings": {"green_explanation": '"PRIVATE-SENTINEL\\u0000"'}}}
    view = social_annotation_details(row)
    assert view["validation_state"] == "bound"
    assert view["explanations"][0]["text"] == r"Old \u0000 result"
    assert "U+0000" in view["explanations"][0]["storage_note"]
    assert "PRIVATE-SENTINEL" not in json.dumps(view)
    assert all(item["value"] is False for item in view["values"])


def test_prepared_direct_payload_and_unrelated_schemes_are_supported():
    row = record()
    row["annotations"] = [row["annotations"][0]["payload"], {"version": "another-scheme", "values": {}}]
    assert social_annotation_details(row)["validation_state"] == "bound"


def test_native_is_not_reclassified_or_given_social_fields():
    row = record()
    row["dataset"] = "native"
    assert social_annotation_details(row) is None


def test_public_source_state_ids_cover_exactly_39_independent_options():
    metadata = social_label_metadata()
    options = social_state_options()
    assert [item["key"] for item in metadata] == list(SOCIAL_LABELS)
    assert len(options) == len({item["value"] for item in options}) == 39
    assert sum(item["level"] == "macro" for item in metadata) == 2
    for key in SOCIAL_LABELS:
        for state in SOCIAL_STATES:
            value = social_state_id(key, state)
            assert parse_social_state_id(value) == (key, state)
            assert value in {item["value"] for item in options}
    assert all("Source export recorded" in item["label"] or "Unknown annotation" in item["label"] for item in options)


@pytest.mark.parametrize("value", [SCHEME, f"{SCHEME}:renewable_energy", f"{SCHEME}:renewable_energy:true",
                                   f"{SCHEME}:invented:unknown", f"{SCHEME}:green_binary:unknown:extra",
                                   f"{SCHEME}:green_binary:unknown ", 1, None])
def test_malformed_ids_are_rejected_instead_of_silently_ignored(value):
    with pytest.raises(ValueError):
        parse_social_state_id(value)


@pytest.mark.parametrize("value", ["renewable_energy", "claims-calibrated", "Renewable energy", "another:code:state"])
def test_other_schemes_are_distinct_from_social_source_state_ids(value):
    assert parse_social_state_id(value) is None


def test_options_and_metadata_do_not_expose_mutable_shared_lists():
    metadata, options = social_label_metadata(), social_state_options()
    metadata[0]["label"] = "CHANGED"
    options[0]["value"] = "CHANGED"
    assert social_label_metadata()[0]["label"] == "Green messaging"
    assert parse_social_state_id(social_state_options()[0]["value"]) == ("green_binary", "source_true")
