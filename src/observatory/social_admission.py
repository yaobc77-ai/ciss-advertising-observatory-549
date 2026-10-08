"""Prepare a reviewed company-post corpus without altering source rows or a DB.

Only supplied Twitter status URLs define a post. Duplicate observations are
retained as source variants; disagreement never establishes a company or text.
"""

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .import_records import CanonicalRecord, load_records
from .models import Issue
from .quality import body_hash, inspect_body, valid_url
from .social_annotations import social_annotation_details

SCHEME = "collected-company-posts-unique-url-v1"
_POST = re.compile(r"/([A-Za-z0-9_]+)/status/(\d+)/?")
_BLOCK_TEXT = {"body_missing", "body_numeric", "body_video_placeholder", "body_garbled", "body_footer_only"}


def file_sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def canonical_post_url(platform, url):
    """Return a supplied URL's canonical identity, or None without guessing."""
    if not isinstance(platform, str) or platform.casefold() not in {"twitter", "x"}:
        return None
    if not isinstance(url, str) or url != url.strip() or not valid_url(url):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    match = _POST.fullmatch(parsed.path)
    if (parsed.hostname not in {"twitter.com", "www.twitter.com", "x.com", "www.x.com"}
            or parsed.username or parsed.password or port not in {None, 80, 443}
            or parsed.query or parsed.fragment or not match):
        return None
    return f"https://twitter.com/{match[1].lower()}/status/{match[2]}"


def _decision_binding(data_path, log_path):
    data_sha = file_sha256(data_path)
    data = json.loads(Path(data_path).read_text(encoding="utf-8-sig"))
    decisions = {item["id"]: item for item in data.get("decision_items", [])}
    latest = {}
    for line in Path(log_path).read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            event = json.loads(line)
            if event.get("item_id") in {"D02", "D03"}:
                latest[event["item_id"]] = event
    bound = {}
    for key, choice in (("D02", "collected_posts"), ("D03", "unique_post")):
        event, item = latest.get(key), decisions.get(key)
        if (not event or not item or event.get("status") != "confirmed"
                or event.get("decision_choice") != choice or event.get("source_data_sha256") != data_sha
                or choice not in {option["id"] for option in item["decision"]["options"]}):
            raise ValueError(f"Current, source-bound confirmed {key} selection is required")
        bound[key] = {name: event.get(name) for name in (
            "id", "item_id", "status", "decision_choice", "choice_label", "note",
            "source_data_sha256", "saved_at_utc", "revision", "reviewer_identity",
        )}
    return {"data_sha256": data_sha, "log_sha256": file_sha256(log_path), "selections": bound}


def _historical_signature(record):
    view = social_annotation_details({"dataset": "social", "body": record.body,
                                     "body_hash": body_hash(record.body), "annotations": record.annotations})
    return tuple((item["key"], item["state"]) for item in view["values"])


def _variant(record, source_sha, line):
    """Private preserved source plus a narrowly projected public view."""
    return {"source_record_id": record.record_id, "source_line": line,
            "source_sha256": source_sha, "source_record": record.model_dump(mode="json")}


def _public_url(value):
    if not valid_url(value) or value != value.strip():
        return ""
    parsed = urlsplit(value)
    return value if not parsed.username and not parsed.password else ""


def _admit_group(members, canonical, source_sha, binding):
    # Stable choice is only an observation for display, never a resolution.
    ordered = sorted(members, key=lambda entry: entry[1].record_id)
    selected_line, selected = ordered[0]
    payload = selected.model_dump(mode="json")
    fields = ("body", "sponsor", "account", "published_at", "platform")
    conflicts = [field for field in fields if len({getattr(record, field) for _, record in ordered}) > 1]
    signatures = {_historical_signature(record) for _, record in ordered}
    if len(signatures) > 1:
        conflicts.append("historical_labels")
    payload["record_id"] = "social-post:" + body_hash("twitter\n" + canonical)
    payload["url"] = canonical
    payload["platform"] = "Twitter"
    payload["title"] = f"Twitter post · {canonical.rsplit('/', 1)[-1]}"
    for field in ("sponsor", "account"):
        if field in conflicts:
            payload[field] = ""
    if "published_at" in conflicts:
        payload["published_at"] = None
    if "historical_labels" in conflicts or "body" in conflicts:
        payload["annotations"] = []
    blockers = {issue.code for issue in inspect_body(selected.body)} & _BLOCK_TEXT
    retrievable = bool(selected.body.strip()) and not blockers and "body" not in conflicts
    payload["countable"] = True
    payload["retrievable"] = retrievable
    payload["retrieval_end"] = None
    payload["retrieval_ranges"] = None
    payload["issues"] = [issue for issue in payload["issues"] if issue["code"] != "social_admission_pending"]
    payload["issues"].append(Issue(
        code="social_collected_company_posts", severity="info",
        detail="User-approved collected company-post corpus; paid-ad status remains unknown. Counts use platform plus canonical original post URL.",
    ).model_dump())
    if conflicts:
        payload["issues"].append(Issue(
            code="social_source_variants_disagree", detail="Source observations disagree; affected public metadata is unknown, and differing bodies are excluded from retrieval.",
        ).model_dump())
    payload["raw"]["admission_status"] = "user_confirmed_company_post_policy"
    payload["raw"]["social_admission"] = {
        "scheme": SCHEME, "scope": "collected_company_posts", "paid_ad_status": "unknown",
        "count_unit": "platform_canonical_original_post_url", "canonical_url": canonical,
        "member_count": len(ordered), "conflicting_fields": conflicts,
        "selected_source_record_id": selected.record_id, "selected_source_line": selected_line,
        "selection_basis": "lexicographically_first_source_id_for_display_only_not_adjudication",
        "source_sha256": source_sha, "review_data_sha256": binding["data_sha256"],
        "decision_event_ids": {key: item["id"] for key, item in binding["selections"].items()},
        "retrieval_status": "enabled_source_post_text_only" if retrievable else (
            "paused_body_disagreement" if "body" in conflicts else "paused_text_quality"),
        "customer_acceptance": False,
    }
    # Duplicate records retain every complete observation, including old values
    # and reversible string encodings. Singletons already retain source_row.
    payload["raw"]["source_variants"] = [_variant(record, source_sha, line) for line, record in ordered]
    payload["provenance"].append({"role": "reviewed_company_post_unique_url_policy",
                                  "source_sha256": source_sha, "scheme": SCHEME,
                                  "review_data_sha256": binding["data_sha256"]})
    return CanonicalRecord.model_validate_json(json.dumps(payload, ensure_ascii=False)), conflicts


def prepare_social_admission(source, *, review_data, reviews, out):
    """Write a new import-ready plan; zero DB/model/network operations.

    Invalid URLs remain in the byte-exact source snapshot and a separate
    quarantine file. They never receive an invented source identity.
    """
    source, review_data, reviews, out = map(Path, (source, review_data, reviews, out))
    if out.exists():
        raise ValueError("Admission output must be a new directory")
    originals = {str(path.resolve()): file_sha256(path) for path in (source, review_data, reviews)}
    binding = _decision_binding(review_data, reviews)
    source_sha = originals[str(source.resolve())]
    groups, quarantined, seen = defaultdict(list), [], set()
    # CanonicalRecord permits a blank URL; quarantine preserves such rows while
    # valid output is subsequently checked by the ordinary import loader.
    for line_number, line in enumerate(source.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        record = CanonicalRecord.model_validate_json(line)
        if (record.record_id in seen or record.dataset != "social" or record.countable or record.retrievable
                or record.raw.get("body_sha256") != body_hash(record.body)):
            raise ValueError("Unique unadmitted, body-bound social source records are required")
        seen.add(record.record_id)
        canonical = canonical_post_url(record.platform, record.url)
        if canonical is None:
            quarantined.append((line_number, record))
        else:
            groups[("twitter", canonical)].append((line_number, record))
    if not seen:
        raise ValueError("Source must contain social rows")
    out.mkdir(parents=True)
    records_path = out / "records.jsonl"
    conflict_groups, count_conflicts = [], Counter()
    source_members = {}
    countable, retrievable = 0, 0
    with records_path.open("x", encoding="utf-8", newline="\n") as stream:
        for (_, canonical), members in sorted(groups.items()):
            record, conflicts = _admit_group(members, canonical, source_sha, binding)
            stream.write(record.model_dump_json() + "\n")
            countable += int(record.countable)
            retrievable += int(record.retrievable)
            source_members[record.record_id] = [item.record_id for _, item in members]
            if conflicts:
                count_conflicts.update(conflicts)
                conflict_groups.append({"record_id": record.record_id, "url": canonical,
                                        "conflicting_fields": conflicts,
                                        "source_record_ids": source_members[record.record_id]})
    with (out / "quarantined.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        for line, record in quarantined:
            stream.write(json.dumps({"source_line": line, "reason": "No unambiguous supplied Twitter status URL",
                                     "record": record.model_dump(mode="json")}, ensure_ascii=False) + "\n")
    # Byte preservation is independent from representative metadata decisions.
    (out / "source_records.original.jsonl").write_bytes(source.read_bytes())
    (out / "review_data.original.json").write_bytes(review_data.read_bytes())
    (out / "reviews.original.jsonl").write_bytes(reviews.read_bytes())
    (out / "members.json").write_text(json.dumps(source_members, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    valid = load_records(records_path, dataset="social") if groups else None
    if valid is not None and len(valid.records) != len(groups):
        raise ValueError("Canonical output validation did not reconcile all groups")
    if {str(path.resolve()): file_sha256(path) for path in (source, review_data, reviews)} != originals:
        raise ValueError("Source or review changed during preparation; do not import outputs")
    receipt = {
        "schema_version": SCHEME, "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "prepared_not_imported_or_published", "scope": "collected_company_posts_not_verified_paid_ads",
        "count_unit": "platform_canonical_original_post_url", "source_records": len(seen),
        "unique_posts": len(groups), "quarantined_source_rows": len(quarantined),
        "duplicate_groups": sum(len(members) > 1 for members in groups.values()),
        "duplicate_rows": sum(len(members) for members in groups.values() if len(members) > 1),
        "countable_records": countable, "retrievable_records": retrievable,
        "conflict_counts": dict(count_conflicts), "conflict_groups": conflict_groups,
        "all_source_members_preserved": sum(map(len, source_members.values())) + len(quarantined) == len(seen),
        "originals": originals, "review_binding": binding,
        "canonical_sha256": file_sha256(records_path), "database_connections": 0, "model_calls": 0,
        "customer_acceptance": False, "local_application_import_executed": False,
        "rollback": "Remove only new canonical record IDs listed in members.json, retaining existing source/native rows; restore a verified pre-import backup if needed.",
    }
    (out / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def public_social_admission(value, *, current_body=None, current_url=None):
    """Project approved identity and exact observations; omit all private raw."""
    if not isinstance(value, dict):
        return None
    admission, variants = value.get("social_admission"), value.get("source_variants")
    if (not isinstance(admission, dict) or admission.get("scheme") != SCHEME
            or admission.get("scope") != "collected_company_posts"
            or admission.get("count_unit") != "platform_canonical_original_post_url"
            or not isinstance(variants, list) or not variants
            or admission.get("member_count") != len(variants)):
        return None
    public = []
    for entry in variants:
        if not isinstance(entry, dict) or not isinstance(entry.get("source_record"), dict):
            return None
        record = entry["source_record"]
        if (record.get("dataset") != "social" or record.get("record_id") != entry.get("source_record_id")
                or not isinstance(record.get("body"), str)
                or canonical_post_url(record.get("platform"), record.get("url")) != admission.get("canonical_url")):
            return None
        body = record["body"]
        projection = social_annotation_details({"dataset": "social", "body": body,
                                                "body_hash": body_hash(body), "annotations": record.get("annotations", [])})
        public.append({"source_record_id": entry["source_record_id"],
                       "title": record.get("title", ""), "company": record.get("sponsor", ""),
                       "account": record.get("account", ""), "platform": record.get("platform", ""),
                       "published_at": record.get("published_at"), "body": body,
                       "url": _public_url(record.get("url", "")),
                       "archive_url": _public_url(record.get("archive_url", "")),
                       "historical_states": projection["values"], "body_sha256": body_hash(body)})
    selected = next((item for item in public if item["source_record_id"] == admission.get("selected_source_record_id")), None)
    if (selected is None or current_body is not None and selected["body"] != current_body
            or current_url is not None and current_url != admission.get("canonical_url")):
        return None
    return {"scope": "Collected company social posts", "paid_ad_status": "Unknown — not verified paid ads",
            "count_unit": "One post per platform and canonical original post URL",
            "member_count": len(public), "conflicting_fields": admission.get("conflicting_fields", []),
            "retrieval_status": admission.get("retrieval_status"), "variants": public,
            "selected_source_record_id": admission.get("selected_source_record_id"),
            "customer_acceptance": False}
