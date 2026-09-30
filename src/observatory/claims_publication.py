"""Prepare reviewed positive CLAIMS assignments without publishing or changing data.

The audit, taxonomy and reviewer file are byte-bound source material. Named
reviewers and owners are assertions in that file, not independently authenticated
human identities. Missing, held and rejected assignments never become negative
article labels or proof that the whole original article was classified.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .claims_projection import ProjectedClaimSpan, validate_projected_claim_span
from .claims_spans import ClaimSpan, validate_claim_span
from .claims_taxonomy import TaxonomyBundle, load_taxonomy_bundle

AUDIT_FILES = (
    "claims_article_linkage.json", "claims_evidence_review.csv", "claims_integration_dry_run.json",
    "claims_result_candidates.json", "claims_source_linkage.json",
    "claims_source_linkage_candidates.csv", "selected_bundle_manifest.json",
)
_SOURCE_FIELDS = (
    "source_result_id", "upstream_article_id", "state", "input_join_valid", "record_id", "version_id",
    "body_hash", "current", "active", "matches_all_retained_article_paragraphs",
    "inside_retrieval_scope", "start", "end", "match_method", "quote",
)
_EVIDENCE_FIELDS = (
    "original_id", "upstream_article_id", "run_id", "recorded_model", "timestamp", "report_state",
    "source_linkage_state", "response_index", "action_type", "nc_id", "sc_id", "mapping_state",
    "definition_drift", "current_candidate_definition", "historical_inline_definition", "source_snippet",
    "quote_state", "record_id", "version_id", "body_hash", "title", "publisher", "sponsor", "url",
    "current", "active", "matches_all_retained_article_paragraphs", "inside_retrieval_scope",
    "start", "end", "quote", "match_method", "lossy", "raw_json_pointer", "candidate_key",
    "issue_codes", "source_identity_review", "semantic_support_review", "reviewer", "reviewed_at",
)
_SHA = re.compile(r"[0-9a-f]{64}")
_NC = re.compile(r"NC_[1-9][0-9]*")
_SC = re.compile(r"SC_[1-9][0-9]*")
_ELIGIBLE_FLAGS = ("current", "active", "inside_retrieval_scope", "matches_all_retained_article_paragraphs")


class ClaimsPublicationError(ValueError):
    """Source binding or explicit publication review is invalid."""


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ClaimsPublicationError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _constant(value):
    raise ClaimsPublicationError(f"Nonfinite JSON constant: {value}")


def _check_values(value):
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_values(key)
            _check_values(item)
    elif isinstance(value, list):
        for item in value:
            _check_values(item)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ClaimsPublicationError("JSON number is not finite")


def _json(data: bytes, location: str, kind=dict):
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_object_pairs, parse_constant=_constant)
        _check_values(value)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ClaimsPublicationError(f"{location}: {exc}") from exc
    if not isinstance(value, kind):
        raise ClaimsPublicationError(f"{location}: expected {kind.__name__}")
    return value


def _keys(value, expected, location):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ClaimsPublicationError(f"{location}: missing or unsupported fields")


def _text(value, location):
    if not isinstance(value, str) or not value.strip():
        raise ClaimsPublicationError(f"{location}: nonempty string required")
    return value


def _sha(value, location):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ClaimsPublicationError(f"{location}: lowercase SHA256 required")
    return value


def _identifier(value, pattern, location):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ClaimsPublicationError(f"{location}: invalid taxonomy identifier")


def _timestamp(value, location):
    _text(value, location)
    try:
        parsed = datetime.fromisoformat(value)
        if ("T" not in value and " " not in value) or parsed.utcoffset() is None:
            raise ValueError("A timezone-aware date and time is required")
    except ValueError as exc:
        raise ClaimsPublicationError(f"{location}: timezone-aware ISO timestamp required") from exc


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ClaimsPublicationError(f"Cannot read {path.name}: {exc}") from exc


def _csv(data, fields, location):
    try:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
        if reader.fieldnames != list(fields) or len(set(reader.fieldnames or ())) != len(fields):
            raise ClaimsPublicationError(f"{location}: invalid or duplicate CSV headers")
        rows = list(reader)
        if any(None in row or any(value is None for value in row.values()) for row in rows):
            raise ClaimsPublicationError(f"{location}: missing or extra CSV cells")
        return rows
    except (UnicodeError, csv.Error) as exc:
        raise ClaimsPublicationError(f"{location}: invalid CSV") from exc


def _csv_values(row, fields):
    return {key: "" if row.get(key) is None else str(row.get(key, "")) for key in fields}


def _expected_source_csv(rows):
    return [_csv_values({**row, **candidate}, _SOURCE_FIELDS)
            for row in rows for candidate in row["candidates"] or [{}]]


def _expected_evidence_csv(rows):
    expected = []
    for row in rows:
        for claim in row["claims"] or [{}]:
            evidence = claim.get("source_evidence_candidates", [])
            current = [item for item in evidence if item["current"] and item["active"]]
            issues = sorted({item["code"] for item in (*row["errors"], *claim.get("errors", ()))})
            for item in current or evidence or [{}]:
                expected.append(_csv_values({
                    **row, **claim, **item, "report_state": row["report_state"],
                    "issue_codes": "|".join(issues), "source_identity_review": "pending",
                    "semantic_support_review": "pending", "reviewer": "", "reviewed_at": "",
                }, _EVIDENCE_FIELDS))
    return expected


def definition_fingerprint(nc_id, nc_definition, sc_id, sc_definition, mapping_state) -> str:
    """Bind a review to the exact current NC definition, SC definition and mapping."""
    _identifier(nc_id, _NC, "nc_id")
    _text(nc_definition, "nc_definition")
    if sc_id is None:
        if sc_definition is not None or mapping_state != "unmapped":
            raise ClaimsPublicationError("An unmapped definition must have a null SC")
    else:
        _identifier(sc_id, _SC, "sc_id")
        _text(sc_definition, "sc_definition")
        if mapping_state != "mapped":
            raise ClaimsPublicationError("A mapped definition must declare mapped")
    payload = json.dumps({
        "nc_id": nc_id, "nc_definition": nc_definition, "sc_id": sc_id,
        "sc_definition": sc_definition, "mapping_state": mapping_state,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _hash(b"observatory.claims-publication-definition.v1\0" + payload)


def _expected_definition(claim):
    definition = claim.get("current_candidate_definition")
    if not isinstance(definition, str) or not definition.strip():
        return None
    mapped = claim.get("mapping_state") == "mapped"
    return definition_fingerprint(
        claim["nc_id"], definition, claim.get("sc_id") if mapped else None,
        claim.get("current_candidate_superclaim_definition") if mapped else None,
        "mapped" if mapped else "unmapped",
    )


@dataclass(frozen=True)
class AuditCandidate:
    result: dict
    claim: dict
    evidence: dict
    expected_definition_sha256: str | None
    source_issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaimsAudit:
    directory: Path
    manifest_sha256: str
    artifact_hashes: dict[str, str]
    report: dict
    taxonomy_manifest: dict
    results: tuple[dict, ...]
    candidates: dict[str, AuditCandidate]


def _source_span(candidate, location):
    if not isinstance(candidate, dict):
        raise ClaimsPublicationError(f"{location}: expected object")
    for key in ("record_id", "version_id"):
        _text(candidate.get(key), f"{location}/{key}")
    _sha(candidate.get("body_hash"), f"{location}/body_hash")
    start, end, quote = candidate.get("start"), candidate.get("end"), candidate.get("quote")
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise ClaimsPublicationError(f"{location}: invalid original character offsets")
    if not isinstance(quote, str) or len(quote) != end - start or not quote.strip():
        raise ClaimsPublicationError(f"{location}: quote length differs from original offsets")
    for flag in _ELIGIBLE_FLAGS:
        if type(candidate.get(flag)) is not bool:
            raise ClaimsPublicationError(f"{location}/{flag}: boolean required")


def _eligible_candidates(report, source_rows, article_rows, result_rows):
    source_lookup, article_lookup, result_ids, candidates = {}, {}, set(), {}
    for row in source_rows:
        if not isinstance(row, dict):
            raise ClaimsPublicationError("Source linkage rows must be objects")
        key = _text(row.get("source_result_id"), "source_result_id")
        if key in source_lookup or not isinstance(row.get("candidates"), list):
            raise ClaimsPublicationError("Duplicate source result or invalid candidates")
        if type(row.get("input_join_valid")) is not bool or row.get("association_is_approved", False) is not False:
            raise ClaimsPublicationError("Source audit cannot assert approval")
        for candidate in row["candidates"]:
            _source_span(candidate, "source candidate")
            if candidate.get("dataset") != report["dataset"]:
                raise ClaimsPublicationError("Source candidate dataset differs from the audit")
        source_lookup[key] = row
    for row in article_rows:
        if not isinstance(row, dict):
            raise ClaimsPublicationError("Article linkage rows must be objects")
        key = _text(row.get("upstream_article_id"), "upstream_article_id")
        if key in article_lookup or row.get("association_is_approved") is not False:
            raise ClaimsPublicationError("Duplicate article or invented article approval")
        article_lookup[key] = row
        for name in ("all_retained_paragraph_version_candidates", "current_version_candidates", "current_record_candidates"):
            values = row.get(name)
            if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
                raise ClaimsPublicationError("Article source candidates must be identifier arrays")
            if len(set(values)) != len(values):
                raise ClaimsPublicationError("Article source candidates contain duplicate identifiers")
    for row in result_rows:
        if not isinstance(row, dict):
            raise ClaimsPublicationError("Saved result rows must be objects")
        key = _text(row.get("original_id"), "original_id")
        if key in result_ids or key not in source_lookup:
            raise ClaimsPublicationError("Duplicate result or missing source linkage")
        result_ids.add(key)
        if row.get("run_id") != report["upstream_run_id"] or row.get("taxonomy_bundle_fingerprint") != report["taxonomy"]["bundle_fingerprint"]:
            raise ClaimsPublicationError("Saved result run/taxonomy binding differs from the audit")
        if row.get("association_is_approved") is not False or row.get("semantics_is_approved") is not False:
            raise ClaimsPublicationError("Saved candidates cannot assert source or semantic approval")
        if not isinstance(row.get("claims"), list) or not isinstance(row.get("errors"), list):
            raise ClaimsPublicationError("Malformed saved result candidates")
        for name in ("recorded_model", "prompt_identifier", "processing_code_revision"):
            if row.get(name) is not None and not isinstance(row[name], str):
                raise ClaimsPublicationError("Saved model provenance must be strings or null")
        source = source_lookup[key]
        for claim in row["claims"]:
            if not isinstance(claim, dict):
                raise ClaimsPublicationError("Saved claim candidates must be objects")
            evidence = claim.get("source_evidence_candidates")
            if not isinstance(evidence, list) or claim.get("association_is_approved") is not False:
                raise ClaimsPublicationError("Malformed claim evidence or invented approval")
            _identifier(claim.get("nc_id"), _NC, "claim/nc_id")
            for item in evidence:
                _source_span(item, "claim evidence")
                _sha(item.get("candidate_key"), "candidate_key")
            eligible = [item for item in evidence if all(item[flag] for flag in _ELIGIBLE_FLAGS)]
            if claim.get("quote_state") != "unique_current_quote_candidate":
                continue
            if len(eligible) != 1 or len(claim.get("input_snippet_locations", ())) != 1 or not source["input_join_valid"]:
                raise ClaimsPublicationError("Unique quote candidate is not uniquely source-bound")
            item = eligible[0]
            if item.get("dataset") != report["dataset"]:
                raise ClaimsPublicationError("Quote candidate dataset differs from the audit")
            _sha(row.get("raw_response_sha256"), "raw_response_sha256")
            _sha(row.get("input_text_sha256"), "input_text_sha256")
            for name in ("response_index", "category_index"):
                if type(claim.get(name)) is not int or claim[name] < 0:
                    raise ClaimsPublicationError("Claim category/response indices must be nonnegative integers")
            expected_pointer = f"/responses/{claim['response_index']}/{claim.get('category_field')}/{claim['category_index']}"
            if claim.get("raw_json_pointer") != expected_pointer:
                raise ClaimsPublicationError("Claim JSON pointer differs from its category/response indices")
            if row["input_text_sha256"] != source.get("input_sha256"):
                raise ClaimsPublicationError("Saved input hash differs from source linkage")
            article = article_lookup.get(row.get("upstream_article_id"))
            if (not article or source.get("upstream_article_id") != row.get("upstream_article_id")
                or article.get("retained_paragraph_order_checked") is not True
                or item["version_id"] not in article.get("all_retained_paragraph_version_candidates", ())
                or item["version_id"] not in article.get("current_version_candidates", ())
                or item["record_id"] not in article.get("current_record_candidates", ())):
                raise ClaimsPublicationError("Quote candidate lacks retained-article source binding")
            parents = [parent for parent in source["candidates"]
                       if parent["record_id"] == item["record_id"] and parent["version_id"] == item["version_id"]
                       and parent["body_hash"] == item["body_hash"] and parent["current"] and parent["active"]
                       and parent["matches_all_retained_article_paragraphs"]
                       and parent["start"] <= item["start"] < item["end"] <= parent["end"]]
            if not parents or not any(parent["quote"][item["start"] - parent["start"]:item["end"] - parent["start"]] == item["quote"] for parent in parents):
                raise ClaimsPublicationError("Claim quote differs from its original paragraph slice")
            snippet = _text(claim.get("source_snippet"), "source_snippet")
            for parent in parents:
                local_start, local_end = item["start"] - parent["start"], item["end"] - parent["start"]
                try:
                    if item.get("match_method") == "upstream_ascii_projection_v1":
                        validate_projected_claim_span(parent["quote"], snippet, ProjectedClaimSpan(
                            local_start, local_end, item["quote"], item.get("lossy"), item["match_method"]))
                    else:
                        validate_claim_span(parent["quote"], snippet, ClaimSpan(
                            local_start, local_end, item["quote"], item.get("match_method")))
                        if item.get("lossy", False) is not False:
                            raise ValueError("Strict source matching cannot be lossy")
                    break
                except (TypeError, ValueError):
                    continue
            else:
                raise ClaimsPublicationError("Claim quote does not validate against its declared matching method")
            expected_key = _hash(json.dumps({
                "upstream_result_id": key, "raw_response_sha256": row["raw_response_sha256"],
                "run_id": row["run_id"], "response_pointer": claim["raw_json_pointer"],
                "nc_id": claim["nc_id"], "taxonomy": row["taxonomy_bundle_fingerprint"],
                "version_id": item["version_id"], "start": item["start"], "end": item["end"],
            }, sort_keys=True).encode("utf-8"))
            if item["candidate_key"] != expected_key or expected_key in candidates:
                raise ClaimsPublicationError("Duplicate or invalid quote candidate key")
            issues = {issue for parent in parents for issue in parent.get("source_issues", ())}
            if any(not isinstance(issue, str) for issue in issues):
                raise ClaimsPublicationError("Source issue codes must be strings")
            candidates[expected_key] = AuditCandidate(row, claim, item, _expected_definition(claim), tuple(sorted(issues)))
    if result_ids != set(source_lookup):
        raise ClaimsPublicationError("Source linkage and saved result coverage differ")
    return candidates


def load_audit(directory: str | Path) -> ClaimsAudit:
    """Read the seven complete, SHA-bound audit artifacts; approve nothing."""
    try:
        root = Path(directory).resolve(strict=True)
        if not root.is_dir():
            raise ClaimsPublicationError("Audit directory must be a directory")
        manifest_path = (root / "audit_output_manifest.json").resolve(strict=True)
        if not manifest_path.is_relative_to(root):
            raise ClaimsPublicationError("Audit manifest must remain inside its directory")
        manifest_data = _read(manifest_path)
        manifest = _json(manifest_data, "audit_output_manifest.json")
        _keys(manifest, {"schema_version", "artifacts", "complete", "labels_published"}, "audit manifest")
        if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 or manifest["complete"] is not True or manifest["labels_published"] is not False:
            raise ClaimsPublicationError("Audit must be complete schema_version 1 with no published labels")
        _keys(manifest["artifacts"], AUDIT_FILES, "audit artifacts")
        contents = {}
        for name, fingerprint in manifest["artifacts"].items():
            _sha(fingerprint, f"artifacts/{name}")
            path = (root / name).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise ClaimsPublicationError("Audit artifact must remain inside its directory")
            contents[name] = _read(path)
            if _hash(contents[name]) != fingerprint:
                raise ClaimsPublicationError(f"{name}: SHA256 differs from the audit manifest")
    except OSError as exc:
        raise ClaimsPublicationError(f"Audit cannot be read: {exc}") from exc
    report = _json(contents["claims_integration_dry_run.json"], "audit report")
    taxonomy = _json(contents["selected_bundle_manifest.json"], "selected taxonomy")
    if report.get("dataset") not in {"native", "social"}:
        raise ClaimsPublicationError("Audit must select one explicit dataset")
    _text(report.get("upstream_run_id"), "upstream_run_id")
    if (taxonomy != report.get("taxonomy") or type(taxonomy.get("schema_version")) is not int
        or taxonomy["schema_version"] != 1):
        raise ClaimsPublicationError("Selected taxonomy differs from the audit report")
    _sha(taxonomy.get("bundle_fingerprint"), "taxonomy fingerprint")
    if taxonomy.get("authority_status") != "candidate_not_approved" or taxonomy.get("structurally_valid") is not True:
        raise ClaimsPublicationError("Taxonomy audit cannot assert authority approval")
    if report.get("source_unchanged_after_audit") is not True:
        raise ClaimsPublicationError("Source changed during the audit")
    for key in ("model_calls", "database_writes", "results_published", "candidate_associations_approved"):
        if type(report.get(key)) is not int or report[key] != 0:
            raise ClaimsPublicationError("Read-only audit cannot assert calls, writes or approval")
    result_rows = _json(contents["claims_result_candidates.json"], "result candidates", list)
    source_rows = _json(contents["claims_source_linkage.json"], "source linkage", list)
    article_rows = _json(contents["claims_article_linkage.json"], "article linkage", list)
    try:
        candidates = _eligible_candidates(report, source_rows, article_rows, result_rows)
        source_csv = _csv(contents["claims_source_linkage_candidates.csv"], _SOURCE_FIELDS, "source CSV")
        evidence_csv = _csv(contents["claims_evidence_review.csv"], _EVIDENCE_FIELDS, "evidence CSV")
        if source_csv != _expected_source_csv(source_rows) or evidence_csv != _expected_evidence_csv(result_rows):
            raise ClaimsPublicationError("CSV audit rows differ from the JSON source candidates")
    except (KeyError, TypeError, IndexError) as exc:
        raise ClaimsPublicationError("Malformed audit candidate structure") from exc
    return ClaimsAudit(root, _hash(manifest_data), dict(manifest["artifacts"]), report, taxonomy,
                       tuple(result_rows), candidates)


def write_review_template(audit_dir: str | Path, out_path: str | Path) -> dict:
    """Write one new pending review file; never fill or imply human approval."""
    audit = load_audit(audit_dir)
    path = Path(out_path)
    value = {
        "schema_version": 1, "audit_manifest_sha256": audit.manifest_sha256,
        "run_id": audit.report["upstream_run_id"], "dataset": audit.report["dataset"],
        "taxonomy_bundle_fingerprint": audit.taxonomy_manifest["bundle_fingerprint"],
        "authority": {"status": "pending", "owner": None, "reviewed_at": None,
                      "publication_meaning": "taxonomy_assignment"},
        "decisions": {key: {
            "expected_definition_sha256": candidate.expected_definition_sha256,
            "source_review": {"decision": "pending", "reviewer": None, "reviewed_at": None, "note": None},
            "semantic_review": {"decision": "pending", "reviewer": None, "reviewed_at": None,
                                "note": None, "definition_sha256": None},
            "publication": "hold",
        } for key, candidate in sorted(audit.candidates.items())},
    }
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        raise ClaimsPublicationError(f"Choose a new review file in an existing directory: {exc}") from exc
    return {"review_path": str(path.resolve()), "candidate_count": len(audit.candidates),
            "authority_status": "pending", "publication": "hold", "results_published": 0}


@dataclass(frozen=True)
class PreparedClaimsImport:
    run_id: str
    dataset: str
    audit_manifest_sha256: str
    audit_report_sha256: str
    review_manifest_sha256: str
    taxonomy_bundle: TaxonomyBundle
    run_metadata: dict
    publications: tuple[dict, ...]
    held_count: int
    rejected_count: int
    _audit_directory: Path = field(repr=False)
    _bundle_directory: Path = field(repr=False)
    _review_path: Path = field(repr=False)

    def summary(self) -> dict:
        """Report preparation counts only; omit source quotations and reviewer notes."""
        return {
            "schema_version": 1, "run_id": self.run_id, "dataset": self.dataset,
            "taxonomy_bundle_fingerprint": self.taxonomy_bundle.bundle_fingerprint,
            "audit_manifest_sha256": self.audit_manifest_sha256,
            "audit_report_sha256": self.audit_report_sha256,
            "review_manifest_sha256": self.review_manifest_sha256,
            "prepared_publications": len(self.publications), "held_count": self.held_count,
            "rejected_count": self.rejected_count,
            "review_states": dict(sorted(Counter(item["review_state"] for item in self.publications).items())),
            "publication_applied": False, "negative_labels_created": 0,
            "unprocessed_results_are_negative": False, "whole_article_classification": False,
            "reviewer_identity": "asserted_in_review_file_not_independently_verified",
        }


def _review_block(value, *, semantic=False):
    fields = {"decision", "reviewer", "reviewed_at", "note"}
    if semantic:
        fields.add("definition_sha256")
    _keys(value, fields, "semantic review" if semantic else "source review")
    allowed = {"pending", "supported", "unsupported"} if semantic else {"pending", "confirmed", "rejected"}
    if value["decision"] not in allowed:
        raise ClaimsPublicationError("Unknown review decision")
    if value["decision"] == "pending":
        if any(value[key] is not None for key in fields - {"decision"}):
            raise ClaimsPublicationError("Pending review fields must remain null")
    else:
        _text(value["reviewer"], "reviewer")
        _timestamp(value["reviewed_at"], "reviewed_at")
        _text(value["note"], "review note")
        if semantic and value["definition_sha256"] is not None:
            _sha(value["definition_sha256"], "reviewed definition")


def _run_metadata(audit):
    model_provenance = sorted({(row.get("recorded_model"), row.get("prompt_identifier"),
                               row.get("processing_code_revision")) for row in audit.results}, key=repr)
    return {
        "processing_method": "saved_upstream_llm_response",
        "input_file_sha256": {name: fingerprint for name, fingerprint in
                              audit.report.get("input_file_sha256", {}).items()
                              if name in {"sampled_50_articles_paragraphs.csv",
                                          "native_ads_paragraphs_cleaned.csv",
                                          "greenwashing_discourse_analysis.db"}},
        "taxonomy_file_sha256": dict(audit.taxonomy_manifest["file_hashes"]),
        "saved_result_count": len(audit.results),
        "model_provenance": [{"recorded_model": model, "prompt_identifier": prompt,
                              "processing_code_revision": revision}
                             for model, prompt, revision in model_provenance],
    }


def prepare_claims_import(audit_dir: str | Path, bundle_dir: str | Path,
                          review_path: str | Path) -> PreparedClaimsImport:
    """Validate review gates and prepare positive payloads; perform no database writes."""
    audit = load_audit(audit_dir)
    bundle = load_taxonomy_bundle(bundle_dir)
    if bundle.to_manifest() != audit.taxonomy_manifest:
        raise ClaimsPublicationError("Actual taxonomy bundle differs from the audited bytes")
    path = Path(review_path).resolve()
    review_bytes = _read(path)
    review = _json(review_bytes, "review manifest")
    _keys(review, {"schema_version", "audit_manifest_sha256", "run_id", "dataset",
                   "taxonomy_bundle_fingerprint", "authority", "decisions"}, "review manifest")
    if type(review["schema_version"]) is not int or review["schema_version"] != 1:
        raise ClaimsPublicationError("Review schema_version must be 1")
    for key, expected in {
        "audit_manifest_sha256": audit.manifest_sha256, "run_id": audit.report["upstream_run_id"],
        "dataset": audit.report["dataset"], "taxonomy_bundle_fingerprint": bundle.bundle_fingerprint,
    }.items():
        if review[key] != expected:
            raise ClaimsPublicationError(f"Review {key} differs from the audited source")
    authority = review["authority"]
    _keys(authority, {"status", "owner", "reviewed_at", "publication_meaning"}, "authority")
    if authority["publication_meaning"] != "taxonomy_assignment":
        raise ClaimsPublicationError("Publication meaning must be taxonomy_assignment")
    if authority["status"] == "approved":
        _text(authority["owner"], "authority owner")
        _timestamp(authority["reviewed_at"], "authority reviewed_at")
    elif authority["status"] == "pending":
        if authority["owner"] is not None or authority["reviewed_at"] is not None:
            raise ClaimsPublicationError("Pending authority fields must remain null")
    else:
        raise ClaimsPublicationError("Unknown authority status")
    decisions = review["decisions"]
    _keys(decisions, audit.candidates, "review decisions")
    publications, held, rejected = [], 0, 0
    for key, candidate in sorted(audit.candidates.items()):
        decision = decisions[key]
        _keys(decision, {"expected_definition_sha256", "source_review", "semantic_review", "publication"}, "candidate decision")
        if decision["expected_definition_sha256"] != candidate.expected_definition_sha256:
            raise ClaimsPublicationError("Review cannot alter its audited definition fingerprint")
        source_review, semantic_review = decision["source_review"], decision["semantic_review"]
        _review_block(source_review)
        _review_block(semantic_review, semantic=True)
        if semantic_review["decision"] == "supported" and (
            candidate.expected_definition_sha256 is None
            or semantic_review["definition_sha256"] != candidate.expected_definition_sha256
        ):
            raise ClaimsPublicationError("Supported semantic review must bind the exact current definition")
        if decision["publication"] not in {"hold", "reject", "publish"}:
            raise ClaimsPublicationError("Unknown publication decision")
        if decision["publication"] == "hold":
            held += 1
            continue
        if decision["publication"] == "reject":
            rejected += 1
            continue
        if authority["status"] != "approved" or source_review["decision"] != "confirmed":
            raise ClaimsPublicationError("Publication requires approved authority and confirmed source review")
        if semantic_review["decision"] == "unsupported":
            rejected += 1
            continue
        row, claim, evidence = candidate.result, candidate.claim, candidate.evidence
        nc_id = claim["nc_id"]
        definition = bundle.subclaims.get(nc_id)
        if not definition:
            raise ClaimsPublicationError("Publication requires a defined NC label")
        sc_id = bundle.claim_superclaim_map.get(nc_id)
        sc_definition = bundle.superclaims.get(sc_id) if sc_id else None
        mapping_state = "mapped" if sc_id else "unmapped"
        expected_definition = definition_fingerprint(nc_id, definition, sc_id, sc_definition, mapping_state)
        if candidate.expected_definition_sha256 != expected_definition:
            raise ClaimsPublicationError("Audited current definition differs from the actual taxonomy")
        conflict = claim.get("raw_response_superclaim_id") is not None and claim["raw_response_superclaim_id"] != sc_id
        drift = bool(claim.get("definition_drift") or claim.get("superclaim_definition_drift")
                     or (claim.get("historical_inline_definition") is not None
                         and claim["historical_inline_definition"] != definition)
                     or claim.get("sc_id") != bundle.raw_claim_superclaim_map.get(nc_id)
                     or claim.get("mapping_state") not in {"mapped", "unmapped"}
                     or any(issue.code == "history_definition_mismatch" and issue.subclaim_id == nc_id for issue in bundle.issues))
        supported = semantic_review["decision"] == "supported"
        if supported and semantic_review["definition_sha256"] != expected_definition:
            raise ClaimsPublicationError("Supported semantic review must bind the exact current definition")
        if not supported and (drift or conflict):
            raise ClaimsPublicationError("Automatic publication cannot resolve definition/mapping drift or raw SC conflicts")
        publication = {
            "schema_version": 1, "candidate_key": key, "record_id": evidence["record_id"],
            "version_id": evidence["version_id"], "body_hash": evidence["body_hash"],
            "dataset": audit.report["dataset"], "run_id": audit.report["upstream_run_id"],
            "taxonomy_bundle_fingerprint": bundle.bundle_fingerprint,
            "nc_id": nc_id, "sc_id": sc_id, "mapping_state": mapping_state,
            "review_state": "human_supported" if supported else "automatic_unverified",
            "publication_meaning": "taxonomy_assignment", "authority": copy.deepcopy(authority),
            "source_review": copy.deepcopy(source_review), "semantic_review": copy.deepcopy(semantic_review),
            "reviewed_definition_sha256": semantic_review["definition_sha256"] if supported else None,
            "expected_definition_sha256": expected_definition,
            "definitions": {"current_nc": definition, "current_sc": sc_definition,
                            "historical_nc": claim.get("historical_inline_definition"),
                            "historical_sc": claim.get("historical_superclaim_definition")},
            "definition_drift": drift, "raw_superclaim_conflict": conflict,
            "evidence": {name: evidence.get(name) for name in ("start", "end", "quote", "match_method")},
            "source_evidence_metadata": copy.deepcopy(evidence),
            "source_issues": list(candidate.source_issues),
            "upstream_result": {name: copy.deepcopy(value) for name, value in row.items() if name != "claims"},
            "upstream_claim": copy.deepcopy(claim),
            "audit_provenance": {"audit_manifest_sha256": audit.manifest_sha256,
                                 "audit_report_sha256": audit.artifact_hashes["claims_integration_dry_run.json"],
                                 "source_snapshot_sha256": audit.report.get("source_snapshot_sha256"),
                                 "retrieval_snapshot": copy.deepcopy(audit.report.get("retrieval_snapshot")),
                                 "input_file_sha256": copy.deepcopy(audit.report.get("input_file_sha256")),
                                 "normalization_contract": copy.deepcopy(audit.report.get("normalization_contract")),
                                 "normalization_fingerprint": audit.report.get("normalization_fingerprint")},
        }
        publication["evidence"]["lossy"] = evidence.get("lossy", False)
        publications.append(publication)
    return PreparedClaimsImport(
        audit.report["upstream_run_id"], audit.report["dataset"], audit.manifest_sha256,
        audit.artifact_hashes["claims_integration_dry_run.json"], _hash(review_bytes), bundle,
        _run_metadata(audit), tuple(publications), held, rejected,
        audit.directory, bundle.source_directory, path,
    )


def validate_prepared_import(prepared: PreparedClaimsImport) -> None:
    """Reload source-bound gates and reject changed files or mutable prepared payloads."""
    if not isinstance(prepared, PreparedClaimsImport):
        raise TypeError("Expected PreparedClaimsImport")
    expected = prepare_claims_import(prepared._audit_directory, prepared._bundle_directory, prepared._review_path)
    for name in ("run_id", "dataset", "audit_manifest_sha256", "audit_report_sha256", "review_manifest_sha256",
                 "run_metadata", "publications", "held_count", "rejected_count"):
        if getattr(prepared, name) != getattr(expected, name):
            raise ClaimsPublicationError(f"Prepared import {name} differs from its validated source files")
    if prepared.taxonomy_bundle != expected.taxonomy_bundle:
        raise ClaimsPublicationError("Prepared taxonomy differs from its validated source files")
