"""Validate literal social source observations without resolving disagreements.

The explicit policy changes retrieval eligibility, not the saved text, historical
labels, advertising identity, or completeness of the original post.
"""

import hashlib
import json
import re
import tempfile
from collections import Counter
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from .chunking import chunk_body
from .import_records import CanonicalRecord, load_records
from .quality import body_hash, inspect_body
from .social_admission import SCHEME, canonical_post_url, file_sha256

SCHEMA_VERSION = "social-source-observations-v1"
_HASH = re.compile(r"[0-9a-f]{64}")
_SOURCE_ID = re.compile(r"junkipedia:[0-9]+")
_HARD_BLOCKS = {"body_missing", "body_numeric", "body_wrong_type", "body_video_placeholder", "body_garbled"}
_REASONS = {"paused_sentence_source_validation", "paused_text_quality", "paused_body_disagreement"}
_CONFLICTS = {"body", "sponsor", "account", "published_at", "platform", "historical_labels"}


def source_version_id(value):
    """Hash the complete original JSON object, with the same canonical JSON rules as record versions."""
    try:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Source observation must contain valid JSON") from exc
    return body_hash(serialized)


def _hash(value):
    return isinstance(value, str) and bool(_HASH.fullmatch(value))


def _safe_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        return value if (parsed.scheme in {"http", "https"} and parsed.hostname
                         and not parsed.username and not parsed.password) else ""
    except ValueError:
        return ""


def validated_observations(payload):
    """Return only text, hashes, public URLs and bounded source-quality flags.

    This works for frozen admission rows as well as explicit policy rows. It does
    not authorize indexing a paused row. Invalid or changed sources fail closed;
    callers must also verify the enclosing current record version.
    """
    if not isinstance(payload, dict) or payload.get("dataset") != "social" or payload.get("countable") is not True:
        raise ValueError("A counted social admission payload is required")
    raw = payload.get("raw")
    admission = raw.get("social_admission") if isinstance(raw, dict) else None
    variants = raw.get("source_variants") if isinstance(raw, dict) else None
    if (not isinstance(admission, dict) or admission.get("scheme") != SCHEME
            or admission.get("scope") != "collected_company_posts"
            or admission.get("count_unit") != "platform_canonical_original_post_url"
            or admission.get("paid_ad_status") != "unknown"
            or not isinstance(variants, list) or not variants
            or type(admission.get("member_count")) is not int
            or admission["member_count"] != len(variants)
            or not _hash(admission.get("source_sha256"))):
        raise ValueError("Invalid source admission or observation membership")
    canonical = canonical_post_url(payload.get("platform"), payload.get("url"))
    if (not canonical or canonical != payload.get("url") or canonical != admission.get("canonical_url")
            or payload.get("record_id") != "social-post:" + body_hash("twitter\n" + canonical)):
        raise ValueError("Social post identity does not match its canonical source URL")
    conflicts = admission.get("conflicting_fields")
    if (not isinstance(conflicts, list) or any(not isinstance(v, str) or v not in _CONFLICTS for v in conflicts)
            or len(set(conflicts)) != len(conflicts)):
        raise ValueError("Invalid source disagreement fields")
    public, originals, seen = [], [], set()
    for entry in variants:
        if not isinstance(entry, dict) or not isinstance(entry.get("source_record"), dict):
            raise ValueError("Malformed source observation")
        record = entry["source_record"]
        identifier = entry.get("source_record_id")
        source_raw = record.get("raw")
        source_row = source_raw.get("source_row") if isinstance(source_raw, dict) else None
        body = record.get("body")
        if (not isinstance(identifier, str) or not _SOURCE_ID.fullmatch(identifier) or identifier in seen
                or record.get("record_id") != identifier or record.get("dataset") != "social"
                or record.get("countable") is not False or record.get("retrievable") is not False
                or type(entry.get("source_line")) is not int or entry["source_line"] < 1
                or entry.get("source_sha256") != admission["source_sha256"]
                or canonical_post_url(record.get("platform"), record.get("url")) != canonical
                or not isinstance(body, str) or not isinstance(source_row, dict)
                or source_row.get("post_text") != body or source_raw.get("body_sha256") != body_hash(body)):
            raise ValueError("Source observation identity, literal text or hash changed")
        quality = sorted({issue.code for issue in inspect_body(body)})
        if set(quality) & _HARD_BLOCKS:
            raise ValueError("Source observation has missing, placeholder, numeric or garbled text")
        seen.add(identifier)
        originals.append(record)
        public.append({"source_observation_id": identifier,
                       "source_version_id": source_version_id(record),
                       "source_body_hash": body_hash(body), "body": body,
                       "quality_codes": quality, "source_conflicts": list(conflicts),
                       "observation_count": len(variants), "url": canonical,
                       "archive_url": _safe_url(record.get("archive_url", ""))})
    selected = next((entry for entry in variants
                     if entry["source_record_id"] == admission.get("selected_source_record_id")), None)
    if (selected is None or selected["source_record_id"] != min(seen)
            or type(admission.get("selected_source_line")) is not int
            or admission.get("selected_source_line") != selected["source_line"]
            or admission.get("selection_basis") != "lexicographically_first_source_id_for_display_only_not_adjudication"
            or payload.get("body") != selected["source_record"]["body"]
            or raw.get("body_sha256") != body_hash(payload["body"])
            or raw.get("source_row") != selected["source_record"]["raw"]["source_row"]):
        raise ValueError("Current display text does not match the preserved selected observation")
    for field in ("body", "sponsor", "account", "published_at", "platform"):
        values = [record.get(field) for record in originals]
        if any(not isinstance(value, str) and not (field == "published_at" and value is None) for value in values):
            raise ValueError("Source observation metadata has an invalid type")
        actual = len(set(values)) > 1
        if actual != (field in conflicts):
            raise ValueError("Source disagreement flag does not match observations")
    if "social_source_retrieval" in raw:
        _validate_policy(payload, public)
    return public


def _validate_policy(payload, observations):
    policy = payload["raw"].get("social_source_retrieval")
    versions = {item["source_observation_id"]: item["source_version_id"] for item in observations}
    if (not isinstance(policy, dict) or policy.get("schema_version") != SCHEMA_VERSION
            or policy.get("source_preservation") is not True
            or policy.get("status") != "enabled_source_observations"
            or not isinstance(policy.get("reason"), str) or policy["reason"] not in _REASONS
            or policy.get("reason") != payload["raw"]["social_admission"].get("retrieval_status")
            or policy.get("expected_old_body_hash") != body_hash(payload["body"])
            or not _hash(policy.get("expected_old_version_id"))
            or not _hash(policy.get("input_batch_sha256"))
            or policy.get("source_observation_versions") != versions
            or policy.get("semantic_completeness_verified") is not False
            or policy.get("paid_ad_status") != "unknown"
            or payload.get("retrievable") is not True):
        raise ValueError("Invalid or drifted explicit source retrieval policy")


def source_policy_enabled(payload):
    """Route only explicitly validated updated rows; legacy rows remain strict."""
    raw = payload.get("raw") if isinstance(payload, dict) else None
    if not isinstance(raw, dict) or "social_source_retrieval" not in raw:
        return False
    validated_observations(payload)
    return True


def _import_payload(row, checksum, line, name):
    normalized = CanonicalRecord.model_validate_json(json.dumps(row, ensure_ascii=False)).model_dump(mode="json")
    normalized["provenance"].append({"source": name, "source_asset_id": "sha256:" + checksum,
                                     "sha256": checksum, "row": line, "row_basis": "jsonl_line",
                                     "role": "canonical_import"})
    return normalized


def _coverage(body):
    chunks = chunk_body(body, strategy="sentence_coverage")
    covered = bytearray(len(body))
    for chunk in chunks:
        start, end = chunk["start"], chunk["end"]
        if not 0 <= start < end <= len(body) or body[start:end] != chunk["text"] or chunk["token_count"] > 600:
            raise ValueError("Source observation chunk failed exact coverage validation")
        covered[start:end] = b"\1" * (end - start)
    if not chunks or any(not char.isspace() and not covered[i] for i, char in enumerate(body)):
        raise ValueError("Source observation has uncovered non-whitespace characters")
    return len(chunks)


def prepare_source_retrieval(source, *, original_source, out, expected_changes=None):
    """Prepare only paused rows in a fresh atomic directory; never import or rewrite inputs."""
    source, original_source, out = map(Path, (source, original_source, out))
    if out.exists():
        raise ValueError("Output must be a new directory; preserve prior and partial attempts")
    input_sha, original_sha = file_sha256(source), file_sha256(original_source)
    expected_original = {}
    changed, bindings, checks = [], {}, []
    count = observations_count = coverage_chunks = strict_failure_records = 0
    reasons = Counter()
    seen = set()
    with source.open(encoding="utf-8") as stream:
        for line, text in enumerate(stream, 1):
            row = json.loads(text)
            if row.get("record_id") in seen:
                raise ValueError("Duplicate current post identity")
            seen.add(row.get("record_id"))
            observations = validated_observations(row)
            count += 1
            observations_count += len(observations)
            for entry in row["raw"]["source_variants"]:
                if entry["source_sha256"] != original_sha or entry["source_line"] in expected_original:
                    raise ValueError("Source observation membership does not match the original snapshot")
                expected_original[entry["source_line"]] = source_version_id(entry["source_record"])
            if row.get("retrievable") is True:
                continue
            reason = row["raw"]["social_admission"].get("retrieval_status")
            if reason not in _REASONS or "social_source_retrieval" in row["raw"]:
                raise ValueError("Paused row has no supported explicit retrieval repair")
            old = _import_payload(row, input_sha, line, source.name)
            binding = {"version_id": source_version_id(old), "body_sha256": body_hash(old["body"]),
                       "url": old["url"], "dataset": "social"}
            target = deepcopy(old)
            target["retrievable"] = True
            target["raw"]["social_source_retrieval"] = {
                "schema_version": SCHEMA_VERSION, "source_preservation": True,
                "status": "enabled_source_observations", "reason": reason,
                "expected_old_version_id": binding["version_id"],
                "expected_old_body_hash": binding["body_sha256"], "input_batch_sha256": input_sha,
                "source_observation_versions": {o["source_observation_id"]: o["source_version_id"] for o in observations},
                "semantic_completeness_verified": False, "paid_ad_status": "unknown",
            }
            if not source_policy_enabled(target):
                raise ValueError("Prepared explicit policy failed validation")
            observation_checks, strict_failed = [], False
            for observation in observations:
                body = observation["body"]
                chunks = _coverage(body)
                coverage_chunks += chunks
                error = None
                try:
                    chunk_body(body, strategy="sentence")
                except ValueError as exc:
                    error = str(exc)
                    strict_failed = True
                observation_checks.append({"source_observation_id": observation["source_observation_id"],
                                           "source_version_id": observation["source_version_id"],
                                           "source_body_hash": observation["source_body_hash"],
                                           "coverage_chunks": chunks, "uncovered_nonwhitespace_characters": 0,
                                           "strict_error": error, "quality_codes": observation["quality_codes"]})
            strict_failure_records += strict_failed
            reasons[reason] += 1
            bindings[row["record_id"]] = binding
            changed.append(target)
            checks.append({"record_id": row["record_id"], "source_line": line, "url": row["url"],
                           "raw_jsonl_line_sha256": hashlib.sha256(text.encode()).hexdigest(),
                           "body_sha256": body_hash(row["body"]), "reason": reason,
                           "observations": observation_checks, "expected_old": binding})
    if expected_changes is not None and len(changed) != expected_changes:
        raise ValueError("Prepared changes do not match the explicitly expected count")
    if not changed:
        raise ValueError("No paused rows require source retrieval preparation")
    reconciled = 0
    with original_source.open(encoding="utf-8") as stream:
        for line, text in enumerate(stream, 1):
            if line in expected_original:
                if source_version_id(json.loads(text)) != expected_original[line]:
                    raise ValueError("Original frozen source JSON does not match a preserved observation")
                reconciled += 1
    if reconciled != len(expected_original):
        raise ValueError("Preserved observations are missing from the original frozen source")
    if (file_sha256(source), file_sha256(original_source)) != (input_sha, original_sha):
        raise ValueError("Input changed during preparation")
    # Validation completes before any output exists. Failed write stages remain
    # recoverable; no old/partial directory is deleted or overwritten.
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=out.name + "-partial-", dir=out.parent))
    records = stage / "records.jsonl"
    records.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                               for row in changed), encoding="utf-8", newline="\n")
    batch = load_records(records, dataset="social")
    if len(batch.records) != len(changed) or not all(source_policy_enabled(r.model_dump(mode="json")) for r in batch.records):
        raise ValueError("Import-ready changed records failed policy validation")
    targets = {r.record_id: source_version_id(r.model_dump(mode="json")) for r in batch.records}
    receipt = {"schema_version": SCHEMA_VERSION, "status": "prepared_not_imported_or_published",
               "source_sha256": input_sha, "original_source_sha256": original_sha,
               "source_records": count, "source_observations": observations_count,
               "original_observations_reconciled": reconciled,
               "changed_records": len(changed), "change_reasons": dict(reasons),
               "all_posts_retrievable_after_prepared_changes": count,
               "changed_observation_coverage_chunks": coverage_chunks,
               "strict_failure_records": strict_failure_records,
               "uncovered_nonwhitespace_characters": 0, "canonical_sha256": file_sha256(records),
               "input_files_preserved": True, "old_issues_status_annotations_and_raw_sources_preserved": True,
               "expected_old_basis": "Exact v2 load_records provenance; live versions must be checked by guarded import",
               "semantic_completeness_verified": False, "paid_ad_status": "unknown",
               "database_operations": 0, "network_calls": 0, "model_calls": 0,
               "index_activation": False, "customer_acceptance": False}
    for name, value in (("expected_current.json", bindings), ("target_versions.json", targets),
                        ("checks.json", checks), ("receipt.json", receipt)):
        (stage / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if out.exists() or (file_sha256(source), file_sha256(original_source)) != (input_sha, original_sha):
        raise ValueError("Output appeared or input changed; retain the partial directory and prepare afresh")
    stage.rename(out)
    return receipt
