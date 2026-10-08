"""Prepare a supplied social-post export offline; preparation is not publication.

The documented company affiliation is not proof of a paid advertisement. All
records remain outside counts and retrieval until a separate review admits them.
Source post text stays unchanged; image/audio/reference text stays in ``raw``.
"""

import hashlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from zipfile import ZipFile

from pydantic import ValidationError

from .models import Issue, RecordInput
from .quality import body_hash, inspect_body, valid_url

SOCIAL_LABELS = (
    "green_binary", "decreasing_emissions", "renewable_energy",
    "other_viable_solutions", "false_solutions", "recycling_waste_management",
    "nature_conservation_references", "broad_environmental_concepts",
    "green_policies_politics", "fossil_fuel_binary", "fossil_fuel_explicit",
    "petrochemicals_other_ff_derivatives_and_industry", "fossil_fuel_implicit",
)
MAX_JSON_BYTES = 256 * 1024 * 1024
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}")
_POST_PATH = re.compile(r"/[^/]+/status/(\d+)/?")
EXPLANATION_FIELDS = ("green_explanation", "fossil_fuel_explanation", "explanation")
EXPLANATION_ENCODING = "json-string-nul-v1"


def _storage_explanations(row: dict) -> tuple[dict, dict]:
    """Escape NUL only in old generated explanations, preserving exact originals.

    Each original is a JSON string stored as text: json.loads restores it,
    including NUL and any pre-existing literal escape sequence. The input row
    stays untouched. Other unsupported source strings still fail validation.
    """
    originals = {
        key: json.dumps(row[key], ensure_ascii=True)
        for key in EXPLANATION_FIELDS
        if isinstance(row.get(key), str) and "\x00" in row[key]
    }
    if not originals:
        return row, {}
    projected = dict(row)
    for key in originals:
        projected[key] = row[key].replace("\x00", r"\u0000")
    return projected, {"scheme": EXPLANATION_ENCODING, "original_json_strings": originals}


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object keys are ambiguous.")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("JSON must not contain NaN or Infinity.")


def _read_source(source: Path) -> tuple[list[dict], dict]:
    source_data = source.read_bytes()
    receipt = {"source": str(source.resolve()), "source_sha256": _checksum(source_data)}
    if source.suffix.casefold() == ".zip":
        with ZipFile(source) as archive:
            members = archive.infolist()
            for member in members:
                normalized = member.filename.replace("\\", "/")
                path = PurePosixPath(normalized)
                if path.is_absolute() or ".." in path.parts or ":" in normalized:
                    raise ValueError("Archive contains an unsafe member path.")
            candidates = [
                item for item in members
                if PurePosixPath(item.filename.replace("\\", "/")).name
                == "claims_twitter_sample.json"
            ]
            if len(candidates) != 1:
                raise ValueError("Archive requires exactly one claims_twitter_sample.json member.")
            member = candidates[0]
            if member.file_size > MAX_JSON_BYTES:
                raise ValueError("Social JSON exceeds the offline preparation size limit.")
            data = archive.read(member)
            receipt["source_member"] = member.filename
            documentation = [
                item for item in members
                if PurePosixPath(item.filename.replace("\\", "/")).name
                == "Data documentation.docx"
            ]
            if len(documentation) == 1:
                receipt["documentation_member"] = documentation[0].filename
                receipt["documentation_sha256"] = _checksum(archive.read(documentation[0]))
    elif source.suffix.casefold() == ".json":
        data = source_data
    else:
        raise ValueError("Social preparation accepts a supplied ZIP archive or JSON export.")
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("Social JSON exceeds the offline preparation size limit.")
    receipt["json_sha256"] = _checksum(data)
    rows = json.loads(
        data.decode("utf-8-sig"), object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
    )
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Social export must be a nonempty JSON array of objects.")
    return rows, receipt


def _source_id(row: dict) -> str:
    value = row.get("id")
    if not isinstance(value, str) or not _SOURCE_ID.fullmatch(value):
        raise ValueError("A stable textual source post ID is required.")
    return value


def _original_url(row: dict) -> str:
    urls = row.get("urls", {}).get("post_url") if isinstance(row.get("urls"), dict) else None
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list) or any(not isinstance(item, str) for item in urls):
        raise ValueError("Original post URLs must be a string or list of strings.")
    post = row.get("post")
    uid = post.get("junkipedia_uid") if isinstance(post, dict) else None
    if not isinstance(uid, str) or not uid.isdigit():
        raise ValueError("The source platform post ID is required for URL identity matching.")
    candidates = set()
    for url in urls:
        if not valid_url(url) or url != url.strip():
            continue
        parsed = urlsplit(url)
        match = _POST_PATH.fullmatch(parsed.path)
        if (
            parsed.hostname in {"twitter.com", "www.twitter.com", "x.com", "www.x.com"}
            and not parsed.username and not parsed.password and not parsed.query
            and not parsed.fragment and match and match.group(1) == uid
        ):
            candidates.add(url)
    if len(candidates) != 1:
        raise ValueError("Original post URL requires one unambiguous status URL matching the source platform ID.")
    return candidates.pop()


def _record(row: dict, number: int, receipt: dict) -> RecordInput:
    row, string_storage = _storage_explanations(row)
    source_id = _source_id(row)
    url = _original_url(row)
    body = row.get("post_text")
    platform = row.get("platform")
    if not isinstance(body, str) or not isinstance(platform, str) or platform.casefold() != "twitter":
        raise ValueError("This adapter requires textual post_text and the documented Twitter platform.")
    issues = [
        Issue(code="social_ad_status_unknown", detail="The export contains company social posts; paid-ad status has not been verified."),
        Issue(code="social_admission_pending", detail="Preparation does not admit records to advertising counts or RAG retrieval."),
    ]
    if string_storage:
        issues.append(Issue(
            code="social_explanation_nul_escaped", severity="info",
            detail="U+0000 in historical generated explanations is displayed as a literal \\u0000; exact originals are preserved as JSON strings in raw.source_string_storage.",
        ))
    for finding in inspect_body(body):
        if finding.code not in {"body_short", "body_question_only", "body_related_navigation"}:
            issues.append(finding)
    published_at = None
    date_time = row.get("date_time")
    published_value = date_time.get("published_at") if isinstance(date_time, dict) else None
    try:
        if not isinstance(published_value, str):
            raise ValueError("Missing publication timestamp")
        timestamp = datetime.fromisoformat(published_value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("Timestamp requires a timezone")
        published_at = timestamp.astimezone(timezone.utc).date()
    except ValueError:
        issues.append(Issue(code="social_publication_date_missing_or_invalid", detail="No valid timezone-qualified publication timestamp; collection dates and year are not substituted."))
    archive_url = row.get("junkipedia_link")
    if not isinstance(archive_url, str) or not valid_url(archive_url):
        archive_url = ""
        issues.append(Issue(code="social_archive_url_missing_or_invalid", detail="No valid supplied Junkipedia archive URL."))
    parent = row.get("parent_entity")
    company = parent if isinstance(parent, str) else ""
    if company:
        issues.append(Issue(code="social_company_not_verified_sponsor", detail="parent_entity is the documented company affiliation for filtering; it does not establish paid sponsorship."))
    channel = row.get("channel")
    account = channel.get("name") if isinstance(channel, dict) else None
    account = account if isinstance(account, str) else ""
    annotations = []
    if all(type(row.get(label)) is bool for label in SOCIAL_LABELS):
        values = {label: row[label] for label in SOCIAL_LABELS}
        annotations.append({
            "version": "claims-social-export-v1", "status": "historical_automatic_unverified",
            "labels": [label for label, value in values.items() if value], "values": values,
            "basis": "supplied_source_post_id_and_exact_body", "source_sha256": receipt["json_sha256"],
            "source_row": number, "body_sha256": body_hash(body),
            "explanations": {
                key: row[key] for key in EXPLANATION_FIELDS
                if key in row
            },
            "taxonomy_mapping": "source_keys_preserved_no_native_label_mapping",
        })
        if string_storage:
            annotations[-1]["explanation_storage"] = {
                "scheme": EXPLANATION_ENCODING,
                "escaped_fields": list(string_storage["original_json_strings"]),
            }
    else:
        issues.append(Issue(code="social_historical_labels_invalid", detail="All documented social label fields must be actual JSON booleans; no partial or coerced annotations attached."))
    provenance = {
        **receipt, "row": number, "row_basis": "json_array_one_based",
        "role": "supplied_social_post_export", "source_post_id": source_id,
        "source_asset_id": f"sha256:{receipt['json_sha256']}", "sha256": receipt["json_sha256"],
    }
    for issue in issues:
        issue.source = receipt["source"]
        issue.row = number
    return RecordInput(
        record_id=f"junkipedia:{source_id}", dataset="social", url=url,
        published_at=published_at, sponsor=company, platform=platform, account=account,
        body=body, title=f"{account or 'Twitter'} post · {source_id}",
        archive_url=archive_url, countable=False, retrievable=False,
        raw={
            "source_row": row, "source_post_id": source_id, "ad_status": "unknown",
            "admission_status": "not_reviewed", "company_field": "parent_entity",
            "sponsor_basis": "company_affiliation_not_verified_paid_sponsor",
            "date_precision": "timestamp" if published_at else "unknown",
            "date_basis": "date_time.published_at_utc" if published_at else "unknown",
            "date_source": receipt["source"], "body_sha256": body_hash(body),
            "body_scope": "post_text_only_image_audio_and_reference_text_not_merged",
            **({"source_string_storage": string_storage} if string_storage else {}),
        },
        provenance=[provenance], issues=issues, annotations=annotations,
    )


def prepare_social_archive(source: Path, *, out: Path, report: Path) -> dict:
    """Write private canonical JSONL and audit counts without DB/model/network calls.

    Outputs must be new files. Unsupported or ambiguous rows are counted in the
    receipt; the input remains the immutable source of every row. No automatic
    review/approval flag is exposed.
    """
    source, out, report = Path(source), Path(out), Path(report)
    targets = [source.resolve(), out.resolve(), report.resolve()]
    if len(set(targets)) != 3:
        raise ValueError("Input, JSONL output and report must be distinct paths.")
    if out.exists() or report.exists():
        raise ValueError("Preparation outputs already exist; choose new paths.")
    rows, receipt = _read_source(source)
    ids = Counter(row.get("id") for row in rows if isinstance(row.get("id"), str))
    url_rows = defaultdict(list)
    body_rows = defaultdict(list)
    for number, row in enumerate(rows, 1):
        try:
            url_rows[_original_url(row)].append(number)
        except ValueError:
            pass
        if isinstance(row.get("post_text"), str) and row["post_text"].strip():
            body_rows[body_hash(row["post_text"])].append(number)
    issue_counts, platforms, companies = Counter(), Counter(), Counter()
    rejected, output_count, annotation_count, dates, archive_count = [], 0, 0, [], 0
    out.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=out.parent, delete=False) as handle:
            temp_path = Path(handle.name)
            for number, row in enumerate(rows, 1):
                try:
                    source_id = _source_id(row)
                    if ids[source_id] > 1:
                        raise ValueError("Repeated source post ID; all ambiguous rows excluded from canonical output.")
                    record = _record(row, number, receipt)
                    for lookup, key, code, detail in (
                        (url_rows, record.url, "duplicate_original_post_url", "Same original post URL appears in multiple source rows; no automatic merge or distinct-ad count."),
                        (body_rows, body_hash(record.body), "duplicate_body", "Exact source body occurs in multiple rows; original post identities are preserved."),
                    ):
                        if len(lookup.get(key, [])) > 1:
                            record.raw[f"{code}_source_rows"] = lookup[key]
                            record.issues.append(Issue(code=code, severity="info", source=receipt["source"], row=number, detail=detail))
                    handle.write(record.model_dump_json() + "\n")
                    output_count += 1
                    annotation_count += bool(record.annotations)
                    archive_count += bool(record.archive_url)
                    platforms[record.platform] += 1
                    companies[record.sponsor] += 1
                    if record.published_at:
                        dates.append(record.published_at.isoformat())
                    issue_counts.update(issue.code for issue in record.issues)
                except ValidationError:
                    # ValidationError's default string includes the input.
                    # Reports must not disclose source posts or explanations.
                    rejected.append({"row": number, "reason": "Source record failed storage or canonical validation; original retained in the supplied export."})
                except ValueError as exc:
                    rejected.append({"row": number, "reason": str(exc)})
        result = {
            "format_version": 1, "prepared_at": datetime.now(timezone.utc).isoformat(),
            **receipt, "output": str(out.resolve()), "input_rows": len(rows),
            "prepared_records": output_count, "rejected_rows": rejected,
            "rejected_row_count": len(rejected), "countable_records": 0, "retrievable_records": 0,
            "publication_status": "offline_staged_not_imported_or_published",
            "ad_status": "unknown_no_verified_paid_ad_admission",
            "ads_data_nonnull_rows": sum(row.get("ads_data") is not None for row in rows),
            "historical_annotations": annotation_count,
            "historical_label_keys": list(SOCIAL_LABELS),
            "taxonomy_status": "historical_automatic_unverified_no_native_label_mapping",
            "valid_archive_links": archive_count,
            "publication_date_range": [min(dates), max(dates)] if dates else None,
            "publication_dates_available": len(dates),
            "platform_counts": dict(platforms), "company_counts": dict(companies),
            "unique_original_post_urls": len(url_rows),
            "duplicate_original_url_groups": sum(len(group) > 1 for group in url_rows.values()),
            "duplicate_original_url_rows": sum(len(group) for group in url_rows.values() if len(group) > 1),
            "duplicate_body_groups": sum(len(group) > 1 for group in body_rows.values()),
            "duplicate_body_rows": sum(len(group) for group in body_rows.values() if len(group) > 1),
            "issue_counts": dict(issue_counts),
            "text_scope": "post_text; source image/audio/referenced text remains raw and is not cited as original body",
            "historical_explanation_storage": {
                "scheme": EXPLANATION_ENCODING,
                "escaped_record_count": issue_counts["social_explanation_nul_escaped"],
                "originals": "raw.source_string_storage.original_json_strings; decode each value with json.loads",
                "scope": "generated explanations only; post_text and label values unchanged",
            },
            "output_sha256": _checksum(temp_path.read_bytes()),
        }
        # Reports contain counts and field status, never source post bodies.
        with report.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        # The temporary file shares the output directory/filesystem. A hard link
        # publishes the complete file atomically and refuses an existing target.
        try:
            os.link(temp_path, out)
        except OSError:
            report.unlink(missing_ok=True)
            raise
        return result
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)
