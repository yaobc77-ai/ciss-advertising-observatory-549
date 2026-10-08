"""Observation hashes and explicit policy guard exact original social text."""

import json
from copy import deepcopy

import pytest

from observatory.import_records import load_records
from observatory.models import RecordInput
from observatory.quality import body_hash
from observatory.social_admission import SCHEME, file_sha256
from observatory.social_source_retrieval import (
    SCHEMA_VERSION,
    prepare_source_retrieval,
    source_policy_enabled,
    source_version_id,
    validated_observations,
)

URL = "https://twitter.com/example/status/9001"


def observation(number, body):
    return RecordInput(
        record_id="junkipedia:" + str(number),
        dataset="social",
        platform="Twitter",
        url=URL,
        title="Supplied source",
        account="Example",
        sponsor="Example company",
        published_at="2024-01-01",
        body=body,
        countable=False,
        retrievable=False,
        archive_url=f"https://www.junkipedia.org/posts/{number}",
        raw={
            "body_sha256": body_hash(body),
            "source_row": {
                "id": str(number),
                "post_text": body,
                "private_path": "C:/PRIVATE",
            },
        },
        issues=[{"code": "original_note", "detail": "Preserved source note"}],
        annotations=[
            {"status": "historical_automatic_unverified", "private": "C:/PRIVATE"}
        ],
    ).model_dump(mode="json")


def admitted(
    bodies=("An exact supplied post.",),
    *,
    source_sha="b" * 64,
    reason="paused_sentence_source_validation",
    retrievable=False,
):
    records = [observation(101 + i, body) for i, body in enumerate(bodies)]
    row = deepcopy(records[0])
    row.update(
        record_id="social-post:" + body_hash("twitter\n" + URL),
        countable=True,
        retrievable=retrievable,
    )
    row["raw"]["source_variants"] = [
        {
            "source_record_id": r["record_id"],
            "source_line": i + 1,
            "source_sha256": source_sha,
            "source_record": r,
        }
        for i, r in enumerate(records)
    ]
    row["raw"]["social_admission"] = {
        "scheme": SCHEME,
        "scope": "collected_company_posts",
        "paid_ad_status": "unknown",
        "count_unit": "platform_canonical_original_post_url",
        "canonical_url": URL,
        "member_count": len(records),
        "source_sha256": source_sha,
        "conflicting_fields": ["body"] if len(set(bodies)) > 1 else [],
        "selected_source_record_id": records[0]["record_id"],
        "selected_source_line": 1,
        "selection_basis": "lexicographically_first_source_id_for_display_only_not_adjudication",
        "retrieval_status": reason,
    }
    return row


def enabled(row):
    value = deepcopy(row)
    observations = validated_observations(value)
    old_version = source_version_id(value)
    value["retrievable"] = True
    value["raw"]["social_source_retrieval"] = {
        "schema_version": SCHEMA_VERSION,
        "source_preservation": True,
        "status": "enabled_source_observations",
        "reason": value["raw"]["social_admission"]["retrieval_status"],
        "expected_old_version_id": old_version,
        "expected_old_body_hash": body_hash(value["body"]),
        "input_batch_sha256": "c" * 64,
        "source_observation_versions": {
            o["source_observation_id"]: o["source_version_id"] for o in observations
        },
        "semantic_completeness_verified": False,
        "paid_ad_status": "unknown",
    }
    return value


def test_independent_observations_keep_exact_body_hashes_and_no_private_projection():
    row = admitted(
        ("Supplied post.", "Supplied post. Extra linked text."),
        reason="paused_body_disagreement",
    )
    before = deepcopy(row)
    result = validated_observations(row)
    assert row == before
    assert [v["body"] for v in result] == [
        "Supplied post.",
        "Supplied post. Extra linked text.",
    ]
    assert len({v["source_version_id"] for v in result}) == 2
    assert all(
        v["observation_count"] == 2 and v["source_conflicts"] == ["body"]
        for v in result
    )
    assert result[1]["source_version_id"] == source_version_id(
        row["raw"]["source_variants"][1]["source_record"]
    )
    assert "PRIVATE" not in json.dumps(result) and "source_row" not in json.dumps(
        result
    )
    assert set(result[0]) == {
        "source_observation_id",
        "source_version_id",
        "source_body_hash",
        "body",
        "quality_codes",
        "source_conflicts",
        "observation_count",
        "url",
        "archive_url",
    }


def test_legacy_and_native_rows_do_not_enable_coverage_route():
    assert source_policy_enabled(admitted(retrievable=True)) is False
    assert source_policy_enabled({"dataset": "native", "raw": {}}) is False


def test_short_social_terms_remain_literal_and_keep_quality_note():
    body = "Drivers get £750 charging credit. Terms and conditions apply."
    row = enabled(admitted((body,), reason="paused_text_quality"))
    assert source_policy_enabled(row)
    assert validated_observations(row)[0]["body"] == body
    assert "body_footer_only" in validated_observations(row)[0]["quality_codes"]


@pytest.mark.parametrize("body", ["", " \n", "None", "video", "123", "bad \ufffd text"])
def test_general_bad_source_text_is_not_autoenabled(body):
    with pytest.raises(ValueError, match="missing, placeholder, numeric or garbled"):
        validated_observations(admitted((body,)))


@pytest.mark.parametrize(
    "mutation",
    [
        "scheme",
        "member_count",
        "bool_member_count",
        "canonical",
        "post_id",
        "selected_line",
        "selected_id",
        "display_body",
        "display_hash",
        "raw_row",
        "source_id",
        "source_line",
        "bool_source_line",
        "source_hash",
        "source_body",
        "source_body_hash",
        "source_post_text",
        "duplicate_source",
        "conflict",
        "json_nan",
    ],
)
def test_malformed_or_drifted_source_fails(mutation):
    row = admitted()
    raw = row["raw"]
    admission = raw["social_admission"]
    variant = raw["source_variants"][0]
    source = variant["source_record"]
    if mutation == "scheme":
        admission["scheme"] = "unreviewed"
    elif mutation == "member_count":
        admission["member_count"] = 2
    elif mutation == "bool_member_count":
        admission["member_count"] = True
    elif mutation == "canonical":
        admission["canonical_url"] = URL + "2"
    elif mutation == "post_id":
        row["record_id"] = "social-post:" + "d" * 64
    elif mutation == "selected_line":
        admission["selected_source_line"] = 9
    elif mutation == "selected_id":
        admission["selected_source_record_id"] = "junkipedia:999"
    elif mutation == "display_body":
        row["body"] = "Changed display body."
    elif mutation == "display_hash":
        raw["body_sha256"] = "0" * 64
    elif mutation == "raw_row":
        raw["source_row"]["post_text"] = "Changed source row."
    elif mutation == "source_id":
        variant["source_record_id"] = "junkipedia:999"
    elif mutation == "source_line":
        variant["source_line"] = 0
    elif mutation == "bool_source_line":
        variant["source_line"] = True
    elif mutation == "source_hash":
        variant["source_sha256"] = "0" * 64
    elif mutation == "source_body":
        source["body"] = "Changed source body."
    elif mutation == "source_body_hash":
        source["raw"]["body_sha256"] = "0" * 64
    elif mutation == "source_post_text":
        source["raw"]["source_row"]["post_text"] = "Changed source field."
    elif mutation == "duplicate_source":
        raw["source_variants"].append(deepcopy(variant))
        admission["member_count"] = 2
    elif mutation == "conflict":
        admission["conflicting_fields"] = ["body"]
    elif mutation == "json_nan":
        source["raw"]["invalid"] = float("nan")
    with pytest.raises(ValueError):
        validated_observations(row)


@pytest.mark.parametrize(
    "mutation",
    [
        "schema",
        "preservation",
        "reason",
        "bodyhash",
        "oldversion",
        "versions",
        "truth",
        "retrievable",
    ],
)
def test_explicit_marker_cannot_bypass_source_binding(mutation):
    row = enabled(admitted())
    policy = row["raw"]["social_source_retrieval"]
    if mutation == "schema":
        policy["schema_version"] = "other"
    elif mutation == "preservation":
        policy["source_preservation"] = False
    elif mutation == "reason":
        policy["reason"] = "arbitrary"
    elif mutation == "bodyhash":
        policy["expected_old_body_hash"] = "0" * 64
    elif mutation == "oldversion":
        policy["expected_old_version_id"] = "invalid"
    elif mutation == "versions":
        policy["source_observation_versions"] = {}
    elif mutation == "truth":
        policy["semantic_completeness_verified"] = True
    elif mutation == "retrievable":
        row["retrievable"] = False
    with pytest.raises(ValueError):
        source_policy_enabled(row)


def test_full_source_json_drift_changes_version_and_invalidates_explicit_policy():
    row = enabled(admitted())
    row["raw"]["source_variants"][0]["source_record"]["annotations"].append(
        {"changed": True}
    )
    with pytest.raises(ValueError, match="drifted"):
        validated_observations(row)


@pytest.mark.parametrize("field", ["sponsor", "account", "published_at", "platform"])
def test_malformed_observation_metadata_raises_value_error(field):
    row = admitted()
    row["raw"]["source_variants"][0]["source_record"][field] = {"invalid": True}
    with pytest.raises(ValueError):
        validated_observations(row)


def test_bool_selected_line_and_source_eligibility_cannot_impersonate_frozen_input():
    row = admitted()
    row["raw"]["social_admission"]["selected_source_line"] = True
    with pytest.raises(ValueError):
        validated_observations(row)
    row = admitted()
    row["raw"]["source_variants"][0]["source_record"]["retrievable"] = True
    with pytest.raises(ValueError):
        validated_observations(row)


@pytest.mark.parametrize(
    "archive",
    [
        "file:///C:/PRIVATE",
        "https://user:secret@example.com/private",
        " https://example.com",
    ],
)
def test_archive_projection_does_not_expose_local_or_credential_url(archive):
    row = admitted()
    row["raw"]["source_variants"][0]["source_record"]["archive_url"] = archive
    assert validated_observations(row)[0]["archive_url"] == ""


def input_files(tmp_path, bodies, reason):
    original = tmp_path / "original.jsonl"
    source_records = [observation(101 + i, body) for i, body in enumerate(bodies)]
    original.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in source_records),
        encoding="utf-8",
    )
    row = admitted(bodies, source_sha=file_sha256(original), reason=reason)
    source = tmp_path / "records.jsonl"
    source.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    return source, original, row


def test_preparation_preserves_text_issues_and_conflicts_and_exact_old_import_version(
    tmp_path,
):
    source, original, row = input_files(
        tmp_path, ("A post.", "A post. Added preview."), "paused_body_disagreement"
    )
    before = (source.read_bytes(), original.read_bytes())
    out = tmp_path / "prepared"
    receipt = prepare_source_retrieval(
        source, original_source=original, out=out, expected_changes=1
    )
    target = json.loads((out / "records.jsonl").read_text(encoding="utf-8"))
    assert target["retrievable"] is True
    assert target["body"] == row["body"] and target["annotations"] == row["annotations"]
    assert target["issues"] == row["issues"]
    assert target["raw"]["source_variants"] == row["raw"]["source_variants"]
    assert target["raw"]["social_admission"] == row["raw"]["social_admission"]
    old_imported = (
        load_records(source, dataset="social").records[0].model_dump(mode="json")
    )
    expected = json.loads((out / "expected_current.json").read_text(encoding="utf-8"))
    assert expected[row["record_id"]]["version_id"] == source_version_id(old_imported)
    assert target["provenance"] == old_imported["provenance"]
    assert (
        receipt["source_observations"]
        == receipt["original_observations_reconciled"]
        == 2
    )
    assert (
        receipt["changed_records"] == 1
        and receipt["uncovered_nonwhitespace_characters"] == 0
    )
    assert (source.read_bytes(), original.read_bytes()) == before
    with pytest.raises(ValueError, match="new directory"):
        prepare_source_retrieval(source, original_source=original, out=out)
    assert (source.read_bytes(), original.read_bytes()) == before


def test_coverage_preparation_recovers_real_symbol_without_altering_body(tmp_path):
    body = "♨️Where are new district heating networks being explored?"
    source, original, _ = input_files(
        tmp_path, (body,), "paused_sentence_source_validation"
    )
    receipt = prepare_source_retrieval(
        source, original_source=original, out=tmp_path / "new"
    )
    assert receipt["strict_failure_records"] == 1
    assert receipt["uncovered_nonwhitespace_characters"] == 0
    assert (
        json.loads((tmp_path / "new/records.jsonl").read_text(encoding="utf-8"))["body"]
        == body
    )


def test_bad_source_or_count_fails_before_output_creation(tmp_path):
    source, original, _ = input_files(tmp_path, ("video",), "paused_text_quality")
    out = tmp_path / "new"
    with pytest.raises(ValueError):
        prepare_source_retrieval(source, original_source=original, out=out)
    assert not out.exists()
    source, original, _ = input_files(
        tmp_path, ("Source text.",), "paused_text_quality"
    )
    with pytest.raises(ValueError, match="expected count"):
        prepare_source_retrieval(
            source, original_source=original, out=out, expected_changes=2
        )
    assert not out.exists()


def test_original_snapshot_reconciliation_rejects_metadata_drift(tmp_path):
    source, original, _ = input_files(
        tmp_path, ("Source text.",), "paused_sentence_source_validation"
    )
    changed = json.loads(original.read_text(encoding="utf-8"))
    changed["title"] = "Different metadata"
    original.write_text(json.dumps(changed) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="snapshot"):
        prepare_source_retrieval(source, original_source=original, out=tmp_path / "new")
    assert not (tmp_path / "new").exists()
