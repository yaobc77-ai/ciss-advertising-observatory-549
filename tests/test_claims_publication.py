"""Review and source-binding gates, using only explicitly synthetic file artifacts."""

import hashlib
import json
from dataclasses import replace

import pytest
from claims_publication_fixtures import (
    approve_synthetic,
    publication_files,
    read_review,
    refresh_audit,
    write_review,
)

from observatory.claims_publication import (
    AUDIT_FILES,
    ClaimsPublicationError,
    load_audit,
    prepare_claims_import,
    validate_prepared_import,
    write_review_template,
)
from observatory.claims_taxonomy import load_taxonomy_bundle


def _read(files, name):
    return json.loads((files["audit_dir"] / name).read_text(encoding="utf-8"))


def _write(files, name, value):
    (files["audit_dir"] / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _rehash(files):
    manifest = _read(files, "audit_output_manifest.json")
    manifest["artifacts"] = {name: hashlib.sha256((files["audit_dir"] / name).read_bytes()).hexdigest() for name in AUDIT_FILES}
    _write(files, "audit_output_manifest.json", manifest)


def _new_template(files):
    files["review_path"] = files["review_path"].parent / "new-pending-review.json"
    write_review_template(files["audit_dir"], files["review_path"])


def _prepare(files):
    return prepare_claims_import(files["audit_dir"], files["bundle_dir"], files["review_path"])


def _bind_changed_taxonomy_or_label(files):
    bundle = load_taxonomy_bundle(files["bundle_dir"])
    _write(files, "selected_bundle_manifest.json", bundle.to_manifest())
    report = _read(files, "claims_integration_dry_run.json")
    report["taxonomy"] = bundle.to_manifest()
    _write(files, "claims_integration_dry_run.json", report)
    results = _read(files, "claims_result_candidates.json")
    keys = []
    for row in results:
        row["taxonomy_bundle_fingerprint"] = bundle.bundle_fingerprint
        for claim in row["claims"]:
            for evidence in claim["source_evidence_candidates"]:
                evidence["candidate_key"] = hashlib.sha256(json.dumps({
                    "upstream_result_id": row["original_id"], "raw_response_sha256": row["raw_response_sha256"],
                    "run_id": row["run_id"], "response_pointer": claim["raw_json_pointer"],
                    "nc_id": claim["nc_id"], "taxonomy": bundle.bundle_fingerprint,
                    "version_id": evidence["version_id"], "start": evidence["start"], "end": evidence["end"],
                }, sort_keys=True).encode("utf-8")).hexdigest()
                keys.append(evidence["candidate_key"])
    _write(files, "claims_result_candidates.json", results)
    files["candidate_keys"] = tuple(keys)
    refresh_audit(files)
    _new_template(files)


def test_template_is_pending_complete_and_never_overwrites_review(tmp_path):
    files = publication_files(tmp_path)
    before = files["review_path"].read_bytes()
    audit, review = load_audit(files["audit_dir"]), read_review(files)
    assert len(audit.candidates) == len(review["decisions"]) == 3
    assert set(review["decisions"]) == set(files["candidate_keys"])
    assert review["authority"] == {"status": "pending", "owner": None, "reviewed_at": None,
                                   "publication_meaning": "taxonomy_assignment"}
    assert all(decision["publication"] == "hold" and decision["source_review"]["decision"] == "pending"
               and decision["semantic_review"]["definition_sha256"] is None for decision in review["decisions"].values())
    with pytest.raises(ClaimsPublicationError, match="new review file"):
        write_review_template(files["audit_dir"], files["review_path"])
    assert files["review_path"].read_bytes() == before


def test_pending_hold_plan_prepares_without_authority_and_creates_no_negative_labels(tmp_path):
    files = publication_files(tmp_path)
    prepared = _prepare(files)
    validate_prepared_import(prepared)
    assert prepared.publications == () and prepared.held_count == 3 and prepared.rejected_count == 0
    assert prepared.summary()["publication_applied"] is False
    assert prepared.summary()["negative_labels_created"] == 0
    assert prepared.summary()["unprocessed_results_are_negative"] is False
    assert prepared.summary()["whole_article_classification"] is False
    assert files["body"] not in json.dumps(prepared.summary())
    assert "_audit_directory" not in prepared.summary()


def test_three_positive_assignments_preserve_two_distinct_article_sources_and_provenance(tmp_path):
    files = publication_files(tmp_path)
    approve_synthetic(files)
    prepared = _prepare(files)
    assert len(prepared.publications) == 3
    assert len({item["record_id"] for item in prepared.publications}) == 2
    assert {item["review_state"] for item in prepared.publications} == {"automatic_unverified"}
    audit = load_audit(files["audit_dir"])
    for publication in prepared.publications:
        candidate = audit.candidates[publication["candidate_key"]]
        assert publication["upstream_claim"] == candidate.claim
        assert publication["upstream_result"]["raw_response_sha256"] == candidate.result["raw_response_sha256"]
        assert publication["upstream_result"]["input_text_sha256"] == candidate.result["input_text_sha256"]
        assert publication["definitions"]["current_nc"] == candidate.claim["current_candidate_definition"]
        assert publication["evidence"]["quote"] == candidate.evidence["quote"]
        record = next(record for record in files["records"] if record["record_id"] == publication["record_id"])
        assert record["body"][publication["evidence"]["start"]:publication["evidence"]["end"]] == publication["evidence"]["quote"]
        assert publication["publication_meaning"] == "taxonomy_assignment"
        assert publication["reviewed_definition_sha256"] is None
    assert not set(prepared.run_metadata).intersection({"authority", "review", "retrieval_snapshot", "source_projection", "checked_at_utc"})
    validate_prepared_import(prepared)


def test_supported_semantics_bind_exact_definition_and_remain_asserted_reviewer_identity(tmp_path):
    files = publication_files(tmp_path)
    approve_synthetic(files, semantic="supported")
    prepared = _prepare(files)
    assert {item["review_state"] for item in prepared.publications} == {"human_supported"}
    assert all(item["reviewed_definition_sha256"] == item["expected_definition_sha256"] for item in prepared.publications)
    assert prepared.summary()["reviewer_identity"] == "asserted_in_review_file_not_independently_verified"
    assert "Synthetic semantic assertion" not in json.dumps(prepared.summary())


@pytest.mark.parametrize("filename", AUDIT_FILES)
def test_every_audit_artifact_is_byte_bound(tmp_path, filename):
    files = publication_files(tmp_path)
    path = files["audit_dir"] / filename
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ClaimsPublicationError, match="SHA256"):
        load_audit(files["audit_dir"])


@pytest.mark.parametrize("mutation", ["incomplete", "boolean_schema", "published", "missing", "extra"])
def test_manifest_requires_exact_complete_unpublished_artifact_set(tmp_path, mutation):
    files = publication_files(tmp_path)
    manifest = _read(files, "audit_output_manifest.json")
    if mutation == "incomplete":
        manifest["complete"] = False
    elif mutation == "boolean_schema":
        manifest["schema_version"] = True
    elif mutation == "published":
        manifest["labels_published"] = True
    elif mutation == "missing":
        manifest["artifacts"].pop("claims_evidence_review.csv")
    else:
        manifest["artifacts"]["../unexpected.json"] = "0" * 64
    _write(files, "audit_output_manifest.json", manifest)
    with pytest.raises(ClaimsPublicationError):
        load_audit(files["audit_dir"])


@pytest.mark.parametrize("bad_json", ['{"complete":true,"complete":true}', '{"evil":NaN}', '{"evil":1e999}'])
def test_duplicate_keys_and_nonfinite_numbers_are_rejected(tmp_path, bad_json):
    files = publication_files(tmp_path)
    (files["audit_dir"] / "claims_integration_dry_run.json").write_text(bad_json, encoding="utf-8")
    _rehash(files)
    with pytest.raises(ClaimsPublicationError):
        load_audit(files["audit_dir"])


@pytest.mark.parametrize("mutation", ["duplicate_header", "extra_cell", "nonfinite_offset", "filled_review"])
def test_csv_is_strict_and_cannot_independently_change_the_audit(tmp_path, mutation):
    files = publication_files(tmp_path)
    path = files["audit_dir"] / "claims_evidence_review.csv"
    text = path.read_text(encoding="utf-8-sig")
    if mutation == "duplicate_header":
        text = text.replace("original_id,upstream_article_id", "original_id,original_id", 1)
    elif mutation == "extra_cell":
        lines = text.splitlines()
        lines[1] += ",unexpected"
        text = "\n".join(lines) + "\n"
    elif mutation == "nonfinite_offset":
        text = text.replace(",23,38,lower emissions,", ",NaN,38,lower emissions,", 1)
        if ",NaN," not in text:
            text = text.replace(",22,37,lower emissions,", ",NaN,37,lower emissions,", 1)
        assert ",NaN," in text
    else:
        text = text.replace(",pending,pending,,", ",confirmed,pending,Synthetic Reviewer,", 1)
    path.write_text(text, encoding="utf-8-sig")
    _rehash(files)
    with pytest.raises(ClaimsPublicationError):
        load_audit(files["audit_dir"])


@pytest.mark.parametrize("field", ["current", "active", "inside_retrieval_scope", "matches_all_retained_article_paragraphs"])
def test_unique_current_quote_must_independently_satisfy_each_source_flag(tmp_path, field):
    files = publication_files(tmp_path)
    rows = _read(files, "claims_result_candidates.json")
    rows[0]["claims"][0]["source_evidence_candidates"][0][field] = False
    _write(files, "claims_result_candidates.json", rows)
    refresh_audit(files)
    with pytest.raises(ClaimsPublicationError, match="uniquely source-bound"):
        load_audit(files["audit_dir"])


def test_duplicate_candidate_decisions_and_unknown_or_omitted_keys_fail(tmp_path):
    files = publication_files(tmp_path)
    review = read_review(files)
    key = files["candidate_keys"][0]
    raw = files["review_path"].read_text(encoding="utf-8")
    raw = raw.replace(f'"{key}": {{', f'"{key}": {{}}, "{key}": {{', 1)
    files["review_path"].write_text(raw, encoding="utf-8")
    with pytest.raises(ClaimsPublicationError, match="Duplicate JSON"):
        _prepare(files)
    for unknown in (False, True):
        edited = json.loads(json.dumps(review))
        removed = edited["decisions"].pop(key)
        if unknown:
            edited["decisions"]["f" * 64] = removed
        write_review(files, edited)
        with pytest.raises(ClaimsPublicationError, match="review decisions"):
            _prepare(files)


@pytest.mark.parametrize("field", ["audit_manifest_sha256", "run_id", "dataset", "taxonomy_bundle_fingerprint"])
def test_review_cannot_rebind_to_a_different_audit_or_run(tmp_path, field):
    files = publication_files(tmp_path)
    review = read_review(files)
    review[field] = "changed"
    write_review(files, review)
    with pytest.raises(ClaimsPublicationError, match="differs"):
        _prepare(files)


@pytest.mark.parametrize("field", ["quote", "start", "nc_id", "sc_id"])
def test_review_cannot_edit_source_locations_or_categories(tmp_path, field):
    files = publication_files(tmp_path)
    review = read_review(files)
    review["decisions"][files["candidate_keys"][0]][field] = "manual change"
    write_review(files, review)
    with pytest.raises(ClaimsPublicationError, match="unsupported fields"):
        _prepare(files)


def test_publish_requires_authority_source_review_and_aware_dates(tmp_path):
    files = publication_files(tmp_path)
    review = approve_synthetic(files)
    valid = json.loads(json.dumps(review))
    for section, changes in (
        ("authority", {"status": "pending", "owner": None, "reviewed_at": None}),
        ("authority", {"owner": ""}),
        ("authority", {"reviewed_at": "2026-01-03"}),
        ("authority", {"publication_meaning": "greenwashing_finding"}),
        ("source_review", {"decision": "pending", "reviewer": None, "reviewed_at": None, "note": None}),
        ("source_review", {"reviewer": ""}),
        ("source_review", {"reviewed_at": "2026-01-03T00:00:00"}),
        ("source_review", {"note": ""}),
    ):
        changed = json.loads(json.dumps(valid))
        target = changed["authority"] if section == "authority" else changed["decisions"][files["candidate_keys"][0]][section]
        target.update(changes)
        write_review(files, changed)
        with pytest.raises(ClaimsPublicationError):
            _prepare(files)


@pytest.mark.parametrize("field", ["reviewer", "reviewed_at", "note", "definition_sha256"])
def test_supported_review_requires_named_traceable_exact_definition_binding(tmp_path, field):
    files = publication_files(tmp_path)
    review = approve_synthetic(files, semantic="supported")
    semantic = review["decisions"][files["candidate_keys"][0]]["semantic_review"]
    semantic[field] = "0" * 64 if field == "definition_sha256" else None
    write_review(files, review)
    with pytest.raises(ClaimsPublicationError):
        _prepare(files)


def test_unsupported_assignment_is_rejected_without_becoming_article_negative(tmp_path):
    files = publication_files(tmp_path)
    approve_synthetic(files, semantic="unsupported")
    prepared = _prepare(files)
    assert prepared.publications == () and prepared.rejected_count == 3
    assert prepared.summary()["negative_labels_created"] == 0


@pytest.mark.parametrize("drift", ["definition", "raw_sc"])
def test_drift_or_superclaim_conflict_requires_supported_current_definition_review(tmp_path, drift):
    files = publication_files(tmp_path)
    rows = _read(files, "claims_result_candidates.json")
    claim = rows[0]["claims"][0]
    if drift == "definition":
        claim["historical_inline_definition"] = "Synthetic old definition."
        claim["definition_drift"] = True
        claim["errors"] = [{"code": "subclaim_definition_drift", "pointer": claim["raw_json_pointer"], "severity": "review"}]
    else:
        claim["raw_response_superclaim_id"] = "SC_2"
        claim["errors"] = [{"code": "response_superclaim_mapping_conflict", "pointer": claim["raw_json_pointer"], "severity": "review"}]
    _write(files, "claims_result_candidates.json", rows)
    refresh_audit(files)
    _new_template(files)
    approve_synthetic(files, candidate_keys=(files["candidate_keys"][0],))
    with pytest.raises(ClaimsPublicationError, match="Automatic publication"):
        _prepare(files)
    approve_synthetic(files, semantic="supported", candidate_keys=(files["candidate_keys"][0],))
    prepared = _prepare(files)
    assert len(prepared.publications) == 1 and prepared.held_count == 2
    publication = prepared.publications[0]
    assert publication["review_state"] == "human_supported"
    assert publication["definition_drift"] if drift == "definition" else publication["raw_superclaim_conflict"]
    assert publication["upstream_claim"]["errors"] == claim["errors"]


def test_unmapped_sc_is_explicitly_null_after_supported_review(tmp_path):
    files = publication_files(tmp_path)
    path = files["bundle_dir"] / "claim_superclaim_map.json"
    mapping = json.loads(path.read_text(encoding="utf-8"))
    mapping.pop("NC_1")
    path.write_text(json.dumps(mapping), encoding="utf-8")
    rows = _read(files, "claims_result_candidates.json")
    claim = rows[0]["claims"][0]
    claim.update({"sc_id": None, "mapping_state": "unmapped", "current_candidate_superclaim_definition": None})
    _write(files, "claims_result_candidates.json", rows)
    _bind_changed_taxonomy_or_label(files)
    approve_synthetic(files, semantic="supported")
    publication = next(item for item in _prepare(files).publications if item["nc_id"] == "NC_1")
    assert publication["sc_id"] is None and publication["mapping_state"] == "unmapped"
    assert publication["definitions"]["current_sc"] is None
    assert publication["upstream_claim"]["raw_response_superclaim_id"] == "SC_1"
    assert publication["raw_superclaim_conflict"]


def test_unknown_nc_definition_cannot_be_published(tmp_path):
    files = publication_files(tmp_path)
    rows = _read(files, "claims_result_candidates.json")
    claim = rows[0]["claims"][0]
    claim.update({"nc_id": "NC_99", "sc_id": None, "mapping_state": "unknown_subclaim",
                  "current_candidate_definition": None, "current_candidate_superclaim_definition": None})
    _write(files, "claims_result_candidates.json", rows)
    _bind_changed_taxonomy_or_label(files)
    approve_synthetic(files)
    with pytest.raises(ClaimsPublicationError, match="defined NC"):
        _prepare(files)


def test_unclassified_and_unprocessed_rows_do_not_become_negatives(tmp_path):
    files = publication_files(tmp_path)
    rows = _read(files, "claims_result_candidates.json")
    rows[1]["claims"] = []
    rows[1]["report_state"] = "needs_review"
    rows[1]["errors"] = [{"code": "no_reviewable_claims", "pointer": "/", "severity": "review"}]
    _write(files, "claims_result_candidates.json", rows)
    refresh_audit(files)
    _new_template(files)
    prepared = _prepare(files)
    assert prepared.held_count == 2 and prepared.publications == () and prepared.rejected_count == 0
    assert prepared.run_metadata["saved_result_count"] == 2
    assert _read(files, "claims_integration_dry_run.json")["missing_saved_result_ids"] == ["synthetic-unprocessed"]
    assert prepared.summary()["negative_labels_created"] == 0


def test_validate_prepared_detects_mutable_payload_and_source_file_changes(tmp_path):
    files = publication_files(tmp_path)
    approve_synthetic(files, semantic="supported")
    prepared = _prepare(files)
    prepared.publications[0]["evidence"]["quote"] = "Invented claim"
    with pytest.raises(ClaimsPublicationError, match="publications"):
        validate_prepared_import(prepared)
    fresh = _prepare(files)
    review = read_review(files)
    review["decisions"][files["candidate_keys"][0]]["publication"] = "hold"
    write_review(files, review)
    with pytest.raises(ClaimsPublicationError, match="review_manifest_sha256"):
        validate_prepared_import(fresh)


def test_validate_prepared_checks_taxonomy_content_not_just_manifest_counts(tmp_path):
    files = publication_files(tmp_path)
    prepared = _prepare(files)
    corrupted = replace(prepared.taxonomy_bundle, subclaims={
        **prepared.taxonomy_bundle.subclaims, "NC_1": "Forged definition with identical file metadata."
    })
    assert corrupted.to_manifest() == prepared.taxonomy_bundle.to_manifest()
    with pytest.raises(ClaimsPublicationError, match="Prepared taxonomy"):
        validate_prepared_import(replace(prepared, taxonomy_bundle=corrupted))


def test_run_identity_metadata_omits_downstream_projection_and_audit_variations(tmp_path):
    files = publication_files(tmp_path)
    first = _prepare(files).run_metadata
    report = _read(files, "claims_integration_dry_run.json")
    report["checked_at_utc"] = "2030-01-01T00:00:00+00:00"
    report["retrieval_snapshot"] = {"data_version": "another downstream index"}
    report["source_projection"] = "upstream-ascii-v1"
    report["input_file_sha256"]["clean_paragraph_data.ipynb"] = "0" * 64
    _write(files, "claims_integration_dry_run.json", report)
    refresh_audit(files)
    _new_template(files)
    assert _prepare(files).run_metadata == first


def test_other_candidate_review_does_not_change_this_candidates_publication_payload(tmp_path):
    files = publication_files(tmp_path)
    approve_synthetic(files, semantic="supported")
    before = _prepare(files)
    review = read_review(files)
    second_key = files["candidate_keys"][1]
    review["decisions"][second_key]["publication"] = "hold"
    write_review(files, review)
    after = _prepare(files)
    first_key = files["candidate_keys"][0]
    assert next(item for item in before.publications if item["candidate_key"] == first_key) == next(
        item for item in after.publications if item["candidate_key"] == first_key
    )
    assert before.review_manifest_sha256 != after.review_manifest_sha256
    assert all("review_manifest_sha256" not in item["audit_provenance"] for item in before.publications)


@pytest.mark.parametrize("filename", ["claims_source_linkage.json", "claims_article_linkage.json", "claims_result_candidates.json"])
def test_non_object_audit_rows_fail_as_validation_errors(tmp_path, filename):
    files = publication_files(tmp_path)
    _write(files, filename, [None])
    _rehash(files)
    with pytest.raises(ClaimsPublicationError, match="must be objects"):
        load_audit(files["audit_dir"])


def test_category_index_and_pointer_binding_cannot_be_invented(tmp_path):
    files = publication_files(tmp_path)
    rows = _read(files, "claims_result_candidates.json")
    rows[0]["claims"][0]["response_index"] = True
    _write(files, "claims_result_candidates.json", rows)
    refresh_audit(files)
    with pytest.raises(ClaimsPublicationError, match="nonnegative integers"):
        load_audit(files["audit_dir"])
