"""Synthetic file-backed review data; none of these identities are real reviewers."""

import hashlib
import json
from pathlib import Path

from observatory.claims_linkage import _write_candidate_csv, _write_evidence_review_csv
from observatory.claims_publication import AUDIT_FILES, write_review_template
from observatory.claims_taxonomy import load_taxonomy_bundle


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_review(files):
    return json.loads(Path(files["review_path"]).read_text(encoding="utf-8"))


def write_review(files, review):
    _write(Path(files["review_path"]), review)


def refresh_audit(files):
    """Regenerate synthetic CSV mirrors and byte hashes after intentional test edits."""
    root = Path(files["audit_dir"])
    source = json.loads((root / "claims_source_linkage.json").read_text(encoding="utf-8"))
    results = json.loads((root / "claims_result_candidates.json").read_text(encoding="utf-8"))
    _write_candidate_csv(root / "claims_source_linkage_candidates.csv", source)
    _write_evidence_review_csv(root / "claims_evidence_review.csv", results)
    _write(root / "audit_output_manifest.json", {
        "schema_version": 1, "complete": True, "labels_published": False,
        "artifacts": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in AUDIT_FILES},
    })


def publication_files(tmp_path):
    """Return two synthetic articles, three positive candidates and pending review."""
    root = Path(tmp_path)
    audit_dir, bundle_dir = root / "publication-audit", root / "publication-taxonomy"
    audit_dir.mkdir()
    bundle_dir.mkdir()
    definitions = {
        "NC_1": "The advertisement states that emissions are lower.",
        "NC_2": "The advertisement promotes clean energy.",
        "NC_3": "The advertisement promotes gas investment.",
    }
    for name, value in {
        "greenwashing_codebook.json": definitions,
        "greenwashing_superclaims.json": {"SC_1": "Environmental promises", "SC_2": "Fossil fuel development"},
        "claim_superclaim_map.json": {"NC_1": "SC_1", "NC_2": "SC_1", "NC_3": "SC_2"},
        "greenwashing_claim_history.json": {
            "last_updated": "2026-01-01T00:00:00+00:00",
            "claims": {key: {"current_text": value, "history": []} for key, value in definitions.items()},
        },
    }.items():
        _write(bundle_dir / name, value)
    bundle = load_taxonomy_bundle(bundle_dir)
    records = []
    for index, body in enumerate((
        "Synthetic company says lower emissions and clean energy. Retained context.",
        "Another synthetic company describes its gas investment. Retained second context.",
    ), start=1):
        record_id = f"00000000-0000-4000-8000-{index:012d}"
        records.append({
            "record_id": record_id, "version_id": _hash(f"synthetic-version-{index}"),
            "body_hash": _hash(body), "body": body, "dataset": "native",
            "url": f"https://example.org/synthetic-ad-{index}",
            "title": f"Synthetic article {index}", "publisher": "Synthetic Publisher",
            "sponsor": f"synthetic-company-{index}",
        })
    run_id = "saved-claims2:" + _hash("synthetic upstream database")
    source_rows, article_rows, result_rows = [], [], []
    candidate_keys = []
    for index, record in enumerate(records, start=1):
        result_id, article_id = str(index), str(100 + index)
        body = record["body"]
        parent = {name: value for name, value in record.items() if name != "body"}
        parent.update({"current": True, "active": True, "inside_retrieval_scope": True,
                       "matches_all_retained_article_paragraphs": True,
                       "start": 0, "end": len(body), "quote": body,
                       "match_method": "exact", "source_issues": []})
        source_rows.append({
            "source_result_id": result_id, "upstream_article_id": article_id, "input_csv_row": index + 1,
            "input_join_valid": True, "input_sha256": _hash(body), "state": "unique_current_text_candidate",
            "candidate_version_ids": [record["version_id"]], "candidates": [parent],
            "association_is_approved": False, "review_state": "automatic_unverified",
        })
        article_rows.append({
            "upstream_article_id": article_id, "supplied_paragraphs": 1, "located_paragraphs": 1,
            "all_retained_paragraph_version_candidates": [record["version_id"]],
            "current_version_candidates": [record["version_id"]], "current_record_candidates": [record["record_id"]],
            "best_partial_coverage": 1, "input_is_complete_original_article": False,
            "retained_paragraph_order_checked": True, "association_is_approved": False,
        })
        row = {
            "original_id": result_id, "recorded_model": "synthetic-recorded-model",
            "timestamp": "2026-01-01T00:00:00+00:00", "raw_response_sha256": _hash(f"synthetic-response-{index}"),
            "input_text_sha256": _hash(body), "taxonomy_bundle_fingerprint": bundle.bundle_fingerprint,
            "report_state": "candidate_results", "claims": [], "response_states": [], "errors": [],
            "method": "saved_upstream_llm_response", "raw_content_pointer": "/choices/0/message/content",
            "review_required": True, "association_is_approved": False, "semantics_is_approved": False,
            "run_id": run_id, "upstream_article_id": article_id, "prompt_identifier": None,
            "processing_code_revision": None, "source_linkage_state": "unique_current_text_candidate",
        }
        selected = [("NC_1", "lower emissions"), ("NC_2", "clean energy")] if index == 1 else [("NC_3", "gas investment")]
        for response_index, (nc_id, snippet) in enumerate(selected):
            start, sc_id = body.index(snippet), bundle.claim_superclaim_map[nc_id]
            pointer = f"/responses/{response_index}/matched_categories/0"
            evidence = {name: value for name, value in record.items() if name != "body"}
            evidence.update({
                "current": True, "active": True, "inside_retrieval_scope": True,
                "matches_all_retained_article_paragraphs": True, "start": start,
                "end": start + len(snippet), "quote": snippet, "match_method": "exact",
            })
            candidate_key = _hash(json.dumps({
                "upstream_result_id": result_id, "raw_response_sha256": row["raw_response_sha256"],
                "run_id": run_id, "response_pointer": pointer, "nc_id": nc_id,
                "taxonomy": bundle.bundle_fingerprint, "version_id": record["version_id"],
                "start": evidence["start"], "end": evidence["end"],
            }, sort_keys=True))
            evidence["candidate_key"] = candidate_key
            candidate_keys.append(candidate_key)
            row["claims"].append({
                "response_index": response_index, "category_index": 0, "action_type": "match_existing_category",
                "category_field": "matched_categories", "nc_id": nc_id, "sc_id": sc_id, "mapping_state": "mapped",
                "historical_inline_definition": definitions[nc_id], "current_candidate_definition": definitions[nc_id],
                "definition_drift": False, "raw_response_superclaim_id": sc_id,
                "historical_superclaim_definition": bundle.superclaims[sc_id],
                "current_candidate_superclaim_definition": bundle.superclaims[sc_id],
                "raw_response_current_superclaim_definition": bundle.superclaims[sc_id],
                "superclaim_definition_drift": False, "source_snippet": snippet, "rationale": "Synthetic test assertion.",
                "raw_json_pointer": pointer, "report_state": "candidate", "errors": [],
                "input_snippet_locations": [{"start": start, "end": start + len(snippet), "quote": snippet, "match_method": "exact"}],
                "source_evidence_candidates": [evidence], "quote_state": "unique_current_quote_candidate",
                "association_is_approved": False, "review_state": "automatic_unverified",
            })
        result_rows.append(row)
    report = {
        "schema_version": 1, "checked_at_utc": "2026-01-02T00:00:00+00:00", "dataset": "native",
        "source_projection": "strict", "taxonomy": bundle.to_manifest(), "upstream_run_id": run_id,
        "input_file_sha256": {name: _hash("synthetic " + name) for name in (
            "sampled_50_articles_paragraphs.csv", "native_ads_paragraphs_cleaned.csv", "greenwashing_discourse_analysis.db")},
        "input_paragraphs": 2, "saved_results": 2, "upstream_articles": 2,
        "missing_saved_result_ids": ["synthetic-unprocessed"], "source_snapshot_sha256": _hash("synthetic source snapshot"),
        "retrieval_snapshot": {"active_profile": "synthetic", "data_version": "synthetic"},
        "source_unchanged_after_audit": True, "model_calls": 0, "database_writes": 0,
        "results_published": 0, "candidate_associations_approved": 0,
    }
    for name, value in {
        "claims_integration_dry_run.json": report, "selected_bundle_manifest.json": bundle.to_manifest(),
        "claims_source_linkage.json": source_rows, "claims_article_linkage.json": article_rows,
        "claims_result_candidates.json": result_rows,
    }.items():
        _write(audit_dir / name, value)
    files = {"audit_dir": audit_dir, "bundle_dir": bundle_dir, "review_path": root / "synthetic-review.json",
             "candidate_keys": tuple(candidate_keys), "records": tuple(records),
             **{name: records[0][name] for name in ("body", "record_id", "version_id", "body_hash")}}
    refresh_audit(files)
    write_review_template(audit_dir, files["review_path"])
    return files


def approve_synthetic(files, *, semantic="pending", candidate_keys=None):
    """TEST ONLY: create explicit synthetic reviewer assertions for selected candidates."""
    review = read_review(files)
    review["authority"] = {"status": "approved", "owner": "Synthetic test authority",
                           "reviewed_at": "2026-01-03T00:00:00+00:00", "publication_meaning": "taxonomy_assignment"}
    for key in candidate_keys or files["candidate_keys"]:
        decision = review["decisions"][key]
        decision["publication"] = "publish"
        decision["source_review"] = {"decision": "confirmed", "reviewer": "Synthetic source reviewer",
                                     "reviewed_at": "2026-01-03T00:00:00+00:00", "note": "Synthetic source identity assertion."}
        if semantic != "pending":
            decision["semantic_review"] = {
                "decision": semantic, "reviewer": "Synthetic semantic reviewer",
                "reviewed_at": "2026-01-03T00:00:00+00:00", "note": "Synthetic semantic assertion for test only.",
                "definition_sha256": decision["expected_definition_sha256"] if semantic == "supported" else None,
            }
    write_review(files, review)
    return review
