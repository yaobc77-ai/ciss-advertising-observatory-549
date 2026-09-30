"""Read-only imports. Source rows are preserved; optional assets never replace the baseline."""

import csv
import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import openpyxl
import pandas as pd
import pandera.pandas as pa
from pandera.errors import SchemaErrors

from observatory.admissions import ADMISSIONS_PATH, read_admissions
from observatory.body_recoveries import apply_body_recoveries
from observatory.body_reviews import apply_body_reviews
from observatory.models import ImportBatch, Issue, RecordInput
from observatory.quality import (
    asset_url_reason,
    body_hash,
    inspect_body,
    mark_duplicate_bodies,
    missing,
    retrieval_boundary,
    valid_url,
)

NATIVE_CSV = Path("sources/FA25_SP26/final_dataset_cleaned.csv")
NATIVE_METADATA = Path("sources/Native Advertising Data/native_ad_dataset.xlsx")
COMBINED_CSV = Path(
    "sources/pdf_archive_20260915/nested_unique/native-ads-download/combined_ads_12-4-25.csv"
)
LEGACY_CSV = Path("sources/FA25_SP26/CLAIMS 1.0 Runs/CSVS/predictions_calibrated.csv")
ARCHIVE_INDEX = Path("analysis/pdf_archive/source_index.json")
# Risks are scoped to the exact asset hashes from the existing visual review,
# never to an arbitrary PDF title or a reusable numeric ID alone.
ARCHIVE_REVIEW_BY_HASH = {
    "595df8156049740ac3677f566147489abc17d6e179457a56c0caad83ff345dd0": "PDF-006: clipped infographic; numerical content is incomplete in extracted text.",
    "81b1c23e9cd961da137c5792d71486355aee2653081fe3c51487ee273ea73419": "PDF-020: repeated headers/navigation; article body is not reliably captured.",
    "1c6c2c16956e02683d58f9693e3be5f270bb6955fb4f56ccb22d4a8d2dddffc3": "PDF-085: another article is included from page 4; article boundaries need review.",
}
DISPLAY_FIELDS = ("url", "publisher", "title", "date", "sponsor", "keyword")
LABEL_FIELDS = (
    "green_labels.green_binary",
    "green_labels.decreasing_emissions",
    "green_labels.viable_solutions",
    "green_labels.false_solutions",
    "green_labels.recycling_waste_management",
    "green_labels.nature_animal_references",
    "green_labels.policies_programs",
    "green_labels.generic_environmental_references",
    "ff_labels.fossil_fuel_binary",
    "ff_labels.primary_product",
    "ff_labels.petrochemical_product",
    "ff_labels.infrastructure_and_production",
)


def _string(value: Any) -> str:
    return "" if missing(value) else str(value).strip()


def _raw_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _identify(dataset: str, url: str) -> str:
    # Preserve URL path/query/case. No unsafe cross-domain or campaign merging.
    return str(uuid5(NAMESPACE_URL, f"ciss-observatory:{dataset}:{url.strip()}"))


def _register(path: Path, source: str, batch: ImportBatch) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    batch.source_hashes[source] = digest
    return digest


def _provenance(
    source: str, digest: str, row: int, role: str, sheet: str | None = None
) -> dict:
    value = {
        "source": source,
        "source_asset_id": f"sha256:{digest}",
        "sha256": digest,
        "row": row,
        "row_basis": "logical_record_including_header",
        "role": role,
    }
    if sheet:
        value["sheet"] = sheet
    return value


def _csv_rows(
    path: Path, source: str, batch: ImportBatch
) -> tuple[list[str], list[tuple[int, dict]]]:
    if not path.is_file():
        batch.rejected.append(
            Issue(
                code="source_missing",
                severity="error",
                source=source,
                detail="Input file is unavailable.",
            )
        )
        return [], []
    _register(path, source, batch)
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            headers = next(reader, [])
            if (
                not headers
                or len(set(headers)) != len(headers)
                or any(not h.strip() for h in headers)
            ):
                batch.rejected.append(
                    Issue(
                        code="invalid_header",
                        severity="error",
                        source=source,
                        row=1,
                        detail="CSV requires nonempty, unique column names.",
                    )
                )
                return [], []
            rows = []
            for number, values in enumerate(reader, 2):
                if not values or not any(v.strip() for v in values):
                    continue
                if len(values) != len(headers):
                    batch.rejected.append(
                        Issue(
                            code="row_width",
                            severity="error",
                            source=source,
                            row=number,
                            detail=f"Expected {len(headers)} fields, received {len(values)}; row isolated.",
                        )
                    )
                    continue
                rows.append((number, dict(zip(headers, values, strict=True))))
            return headers, rows
    except (UnicodeError, csv.Error) as exc:
        batch.rejected.append(
            Issue(
                code="csv_unreadable",
                severity="error",
                source=source,
                detail=f"CSV could not be decoded/parsed: {type(exc).__name__}.",
            )
        )
        return [], []


def _validate_rows(
    headers: list[str],
    rows: list[tuple[int, dict]],
    required: tuple[str, ...],
    source: str,
    batch: ImportBatch,
    nonempty: tuple[str, ...] = (),
) -> list[tuple[int, dict]]:
    """Pandera validates actual input frames; bad row indices are quarantined."""
    columns = {name: pa.Column(str, nullable=False) for name in required}
    if "url" in columns:
        columns["url"] = pa.Column(
            str, checks=pa.Check(valid_url, element_wise=True), nullable=False
        )
    for name in nonempty:
        columns[name] = pa.Column(
            str,
            checks=pa.Check(lambda value: not missing(value), element_wise=True),
            nullable=False,
        )
    schema = pa.DataFrameSchema(columns, strict=False, coerce=False)
    frame = pd.DataFrame(
        [row for _, row in rows], columns=headers, index=[number for number, _ in rows]
    )
    try:
        schema.validate(frame, lazy=True)
        return rows
    except SchemaErrors as exc:
        failures = exc.failure_cases
        structural = failures[failures["index"].isna()]
        if not structural.empty:
            absent = sorted(set(required) - set(headers))
            batch.rejected.append(
                Issue(
                    code="input_schema",
                    severity="error",
                    source=source,
                    detail=f"Input schema failed; missing columns: {absent}; no rows imported from this input.",
                )
            )
            return []
        bad_rows = {int(value) for value in failures["index"]}
        for number in sorted(bad_rows):
            bad_columns = sorted(
                {
                    str(value)
                    for value in failures.loc[failures["index"] == number, "column"]
                }
            )
            batch.rejected.append(
                Issue(
                    code="input_row_schema",
                    severity="error",
                    source=source,
                    row=number,
                    detail=f"Invalid fields {bad_columns}; row isolated by Pandera validation.",
                )
            )
        return [(number, row) for number, row in rows if number not in bad_rows]


def _date_value(
    value: Any, *, source: str, row: int, fmt: str | None = None, notes: str = ""
) -> tuple[date | None, list[Issue], str]:
    def issue(code: str, detail: str) -> Issue:
        return Issue(code=code, source=source, row=row, detail=detail)

    if missing(value):
        return (
            None,
            [
                issue(
                    "date_missing",
                    "Publication date unavailable; excluded only from date-dependent statistics.",
                )
            ],
            "unknown",
        )
    if "url date does not match the date in dataset" in notes.casefold():
        return (
            None,
            [
                issue(
                    "date_source_conflict",
                    "Source notes explicitly flag a URL/date mismatch; original date retained for review.",
                )
            ],
            "conflict",
        )
    # This source note records an alternative publication date, not a formatting
    # issue. Avoid replacing it with a guessed year or the archive capture date.
    if "article publish date" in notes.casefold() and "wayback" in notes.casefold():
        return (
            None,
            [
                issue(
                    "date_source_conflict",
                    "Source notes identify an alternative publication date through an archived capture; unresolved.",
                )
            ],
            "conflict",
        )
    raw = str(value).strip()
    if re.fullmatch(r"\d{4}", raw):
        return (
            None,
            [issue("date_partial", "Year-only date retained; no month/day invented.")],
            "year",
        )
    if re.fullmatch(r"\d{4}-\d{1,2}", raw):
        return (
            None,
            [issue("date_partial", "Month-only date retained; no day invented.")],
            "month",
        )
    try:
        if isinstance(value, datetime):
            parsed = value.date()
        elif isinstance(value, date):
            parsed = value
        elif fmt:
            parsed = datetime.strptime(raw, fmt).date()  # noqa: DTZ007 - Preserve the source calendar date, not an invented timezone.
        else:
            parsed = datetime.fromisoformat(raw).date()
    except (ValueError, TypeError):
        return (
            None,
            [
                issue(
                    "date_unparseable",
                    "Date does not match the explicitly selected format; raw value retained.",
                )
            ],
            "unknown",
        )
    if parsed.year < 2000:
        return (
            None,
            [
                issue(
                    "date_outlier",
                    "Pre-2000 date is outside this digital-ad corpus's expected period; requires review, not automatic correction.",
                )
            ],
            "review",
        )
    return parsed, [], "day"


def _metadata(root: Path, batch: ImportBatch) -> dict[str, tuple[int, dict, str]]:
    path = root / NATIVE_METADATA
    source = NATIVE_METADATA.as_posix()
    if not path.is_file():
        batch.rejected.append(
            Issue(
                code="metadata_missing",
                source=source,
                detail="Metadata supplement unavailable; baseline rows remain importable.",
            )
        )
        return {}
    _register(path, source, batch)
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    headers = [cell.value for cell in next(sheet.iter_rows())]
    if "url" not in headers:
        workbook.close()
        batch.rejected.append(
            Issue(
                code="metadata_schema",
                severity="error",
                source=source,
                detail="Metadata supplement lacks URL column.",
            )
        )
        return {}
    found: dict[str, tuple[int, dict, str]] = {}
    duplicated = set()
    for number, values in enumerate(sheet.iter_rows(min_row=2, values_only=True), 2):
        row = {
            name: _raw_value(value) for name, value in zip(headers, values, strict=True)
        }
        url = _string(row.get("url"))
        if not url:
            continue
        if url in found:
            duplicated.add(url)
            batch.rejected.append(
                Issue(
                    code="metadata_duplicate_url",
                    source=source,
                    row=number,
                    detail="Ambiguous supplement URL; no first-row-wins join.",
                )
            )
        else:
            found[url] = (number, row, sheet.title)
    workbook.close()
    for url in duplicated:
        found.pop(url)
    return found


def _body_issues(
    body: str, source: str, row: int, *, social: bool = False
) -> tuple[list[Issue], bool]:
    boundary = retrieval_boundary(body)
    issues = inspect_body(body if boundary is None else body[:boundary])
    if boundary is not None:
        issues.extend(
            issue
            for issue in inspect_body(body)
            if issue.code in {"body_related_navigation", "body_truncated_suspected"}
        )
    # Complete social posts can naturally be shorter than an article or be a
    # single question. The future social feed must not inherit article length gates.
    for issue in issues:
        issue.source, issue.row = source, row
        if social and issue.code in {"body_short", "body_question_only"}:
            issue.severity = "info"
    return issues, not any(issue.severity != "info" for issue in issues)


def _attach_archive_candidates(
    root: Path, records: list[RecordInput], batch: ImportBatch
) -> None:
    path = root / ARCHIVE_INDEX
    if not path.is_file():
        return
    source = ARCHIVE_INDEX.as_posix()
    digest = _register(path, source, batch)
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise TypeError("Archive index must be a list")
    except (UnicodeError, ValueError, TypeError) as exc:
        batch.rejected.append(
            Issue(
                code="archive_index_invalid",
                source=source,
                detail=f"Optional archive index could not be read: {type(exc).__name__}.",
            )
        )
        return
    records_by_url = {record.url: record for record in records}
    for number, entry in enumerate(entries, 1):
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("source_urls"), list)
            or not all(isinstance(url, str) for url in entry["source_urls"])
            or not isinstance(entry.get("path"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", "")))
        ):
            batch.rejected.append(
                Issue(
                    code="archive_index_row_invalid",
                    source=source,
                    row=number,
                    detail="Archive candidate needs source_urls, a local path, and a SHA-256 recorded by the index.",
                )
            )
            continue
        review = []
        if entry.get("low_text"):
            review.append("Index flags low extracted text.")
        if entry.get("extraction_error"):
            review.append("Index reports a PDF text-extraction error.")
        if entry["sha256"] in ARCHIVE_REVIEW_BY_HASH:
            review.append(ARCHIVE_REVIEW_BY_HASH[entry["sha256"]])
        for url in sorted({url.strip() for url in entry["source_urls"]}):
            record = records_by_url.get(url)
            if record is None:
                continue
            provenance = _provenance(
                source, digest, number, "internal_archive_candidate"
            )
            provenance.update(
                {
                    "row_basis": "json_array_item_1_based",
                    "archive_id": entry.get("id", ""),
                    "archive_path": entry["path"],
                    "archive_sha256": entry["sha256"],
                    "archive_hash_verification": "recorded_in_index_not_rehashed",
                    "match_state": "candidate_unverified",
                    "basis": "index_candidate_source_url_exact_match",
                    "index_source_match": entry.get("source_match", ""),
                    "candidate_url": url,
                    "low_text": entry.get("low_text"),
                    "review_reasons": review,
                }
            )
            record.provenance.append(provenance)
            record.issues.append(
                Issue(
                    code="archive_review" if review else "archive_candidate_unverified",
                    severity="review" if review else "info",
                    source=source,
                    row=number,
                    detail=f"{entry.get('id', '')}: archive association remains unverified. "
                    + " ".join(review)
                    + " CSV body eligibility is assessed independently.",
                )
            )


def _attach_legacy(root: Path, records: list[RecordInput], batch: ImportBatch) -> None:
    path = root / LEGACY_CSV
    if not path.is_file():
        return  # Optional historical artifacts never prevent a baseline import.
    source = LEGACY_CSV.as_posix()
    headers, rows = _csv_rows(path, source, batch)
    required = (
        "url",
        "text",
        *LABEL_FIELDS,
        *(field + "_cal" for field in LABEL_FIELDS),
    )
    rows = _validate_rows(headers, rows, required, source, batch)
    counts = Counter(row["url"].strip() for _, row in rows)
    records_by_url = {record.url: record for record in records}
    digest = batch.source_hashes[source]
    for number, row in rows:
        url = row["url"].strip()
        record = records_by_url.get(url)
        if counts[url] != 1 or record is None or row["text"] != record.body:
            batch.rejected.append(
                Issue(
                    code="legacy_identity_unverified",
                    source=source,
                    row=number,
                    detail="Historical labels require one unique matching URL and exact full body; labels not attached.",
                )
            )
            continue
        if any(
            row[field + suffix].strip().lower() not in {"true", "false", "0", "1"}
            for suffix in ("", "_cal")
            for field in LABEL_FIELDS
        ):
            batch.rejected.append(
                Issue(
                    code="legacy_label_invalid",
                    source=source,
                    row=number,
                    detail="Historical boolean labels are malformed; annotations not attached.",
                )
            )
            continue
        for suffix, version in (("", "claims-original"), ("_cal", "claims-calibrated")):
            values = {
                field: row[field + suffix].strip().lower() in {"true", "1"}
                for field in LABEL_FIELDS
            }
            record.annotations.append(
                {
                    "version": version,
                    "labels": [field for field, value in values.items() if value],
                    "values": values,
                    "basis": "url_and_exact_body",
                    "body_sha256": body_hash(record.body),
                    "source": source,
                    "source_sha256": digest,
                    "source_row": number,
                    "legacy_doc_id": row.get("doc_id", ""),
                    "status": "historical_automatic_unverified",
                    "explanations": {
                        key: row.get(key, "")
                        for key in (
                            "green_labels.explanation",
                            "ff_labels.explanation",
                            "calib_rationale",
                        )
                    },
                }
            )
        record.provenance.append(
            _provenance(source, digest, number, "legacy_labels_exact_match")
        )


def _add_candidates(root: Path, baseline_urls: set[str], batch: ImportBatch) -> None:
    path = root / COMBINED_CSV
    if not path.is_file():
        # The same-named source directory copy is an explicit fallback; record
        # which file was actually used. Do not assume its content is equivalent.
        path = root / "sources/Native Advertising Data/combined_ads_12-4-25.csv"
    if not path.is_file():
        return
    source = path.relative_to(root).as_posix()
    headers, rows = _csv_rows(path, source, batch)
    rows = _validate_rows(headers, rows, (*DISPLAY_FIELDS, "article"), source, batch)
    seen = set(baseline_urls)
    counts = Counter(row["url"].strip() for _, row in rows)
    for number, row in rows:
        url = row["url"].strip()
        if url in seen:
            continue
        if counts[url] > 1:
            batch.rejected.append(
                Issue(
                    code="duplicate_candidate_url",
                    severity="error",
                    source=source,
                    row=number,
                    detail="Additional URL has ambiguous source rows; no row selected.",
                )
            )
            continue
        seen.add(url)
        issues, body_available = _body_issues(row["article"], source, number)
        reason = asset_url_reason(url)
        if reason:
            issues.append(
                Issue(code="scope_asset_url", source=source, row=number, detail=reason)
            )
        if "legal-disclaimer" in url.lower() or "legal_disclaimer" in url.lower():
            issues.append(
                Issue(
                    code="scope_legal_attachment",
                    source=source,
                    row=number,
                    detail="URL names a legal-disclaimer attachment; not an independent advertisement by default.",
                )
            )
        if "menopause" in (url + " " + row["title"]).lower():
            issues.append(
                Issue(
                    code="scope_requires_review",
                    source=source,
                    row=number,
                    detail="URL/title explicitly concerns menopause; current fossil-fuel scope needs human confirmation.",
                )
            )
        _, date_issues, precision = _date_value(row["date"], source=source, row=number)
        issues.extend(date_issues)
        for field in ("publisher", "title", "sponsor"):
            if missing(row[field]):
                issues.append(
                    Issue(
                        code=f"{field}_missing",
                        source=source,
                        row=number,
                        detail=f"Candidate {field} is unavailable.",
                    )
                )
        batch.candidates.append(
            {
                "record_id": _identify("native", url),
                "dataset": "native",
                "url": url,
                "title": row["title"],
                "sponsor": row["sponsor"],
                "keyword": row["keyword"],
                "reason": "URL absent from the approved baseline; explicit review required before admission.",
                "scope_status": "asset" if reason else "needs_review",
                "body_available": body_available,
                "retrieval_end": retrieval_boundary(row["article"]),
                "date_precision": precision,
                "raw": row,
                "provenance": [
                    _provenance(
                        source, batch.source_hashes[source], number, "candidate"
                    )
                ],
                "issues": [issue.model_dump() for issue in issues],
            }
        )


def _apply_admissions(
    root: Path, batch: ImportBatch, supplement: dict, *, required=False
) -> None:
    manifest = read_admissions(root, batch, required=required)
    if manifest is None:
        return
    decisions = {item.url: item for item in manifest.decisions}
    review_source = ADMISSIONS_PATH.as_posix()
    for candidate in batch.candidates:
        decision = decisions.get(candidate["url"])
        if decision is None:
            continue
        review = {
            "manifest_id": manifest.review_id,
            "reviewer_type": manifest.reviewer_type,
            "scope_note": manifest.scope_note,
            **decision.model_dump(),
        }
        candidate["review"] = review
        candidate["admission_status"] = {
            "include": "admitted",
            "exclude": "excluded",
            "pending": "pending",
        }[decision.decision]
        if decision.decision != "include":
            continue
        row = candidate["raw"]
        source = candidate["provenance"][0]["source"]
        number = decision.source_row
        metadata = supplement.get(candidate["url"])
        metadata_row = metadata[1] if metadata else {}
        metadata_source = NATIVE_METADATA.as_posix()
        provenance = list(candidate["provenance"])
        if metadata:
            provenance.append(
                _provenance(
                    metadata_source,
                    batch.source_hashes[metadata_source],
                    metadata[0],
                    "disclosure_and_notes",
                    metadata[2],
                )
            )
        issues = [Issue.model_validate(value) for value in candidate["issues"]]
        published, date_issues, precision = _date_value(
            row["date"],
            source=source,
            row=number,
            notes=_string(metadata_row.get("notes")),
        )
        issues.extend(
            issue for issue in date_issues if issue.code not in {i.code for i in issues}
        )
        disclosure = _string(metadata_row.get("disclosure language"))
        if not disclosure:
            issues.append(
                Issue(
                    code="disclosure_unknown",
                    source=source,
                    row=number,
                    detail="No URL-matched raw disclosure text; evidence references remain in the admission review.",
                )
            )
        if decision.date_policy == "unknown":
            published, precision = None, "conflict"
            issues.append(
                Issue(
                    code="date_source_conflict",
                    source=review_source,
                    row=number,
                    detail=decision.reason,
                )
            )
        fields = {
            field: _string(row.get(field))
            for field in ("publisher", "title", "sponsor", "keyword")
        }
        # The baseline uses lowercase sponsor keys. Match that convention so case
        # alone cannot split sponsor groups. Collection keywords retain source case.
        fields["sponsor"] = fields["sponsor"].casefold()
        if fields["publisher"] == "WSJ":
            fields["publisher"] = "The Wall Street Journal"
        if decision.sponsor_policy == "unknown":
            fields["sponsor"] = ""
            issues.append(
                Issue(
                    code="sponsor_unverified",
                    source=review_source,
                    row=number,
                    detail=decision.reason,
                )
            )
        if decision.body_mode == "metadata_only":
            issues.append(
                Issue(
                    code="review_metadata_only",
                    source=review_source,
                    row=number,
                    detail=decision.reason,
                )
            )
        issues.append(
            Issue(
                code="admission_review",
                severity="info",
                source=review_source,
                row=number,
                detail=f"{manifest.reviewer_type} review {decision.review_id}: {decision.reason}",
            )
        )
        # Keep source body intact even when its use for retrieval is disabled.
        batch.records.append(
            RecordInput(
                record_id=candidate["record_id"],
                dataset="native",
                url=candidate["url"],
                **fields,
                published_at=published,
                body=row["article"],
                disclosure=disclosure,
                countable=True,
                retrievable=decision.body_mode == "text"
                and candidate["body_available"],
                retrieval_end=candidate["retrieval_end"],
                raw={
                    "supplement": row,
                    "metadata": metadata_row,
                    "admission_review": review,
                    "date_precision": precision,
                    "date_source": source,
                    "body_sha256": body_hash(row["article"]),
                },
                provenance=[
                    *provenance,
                    {
                        "source": review_source,
                        "sha256": batch.source_hashes[review_source],
                        "source_asset_id": f"sha256:{batch.source_hashes[review_source]}",
                        "role": "admission_review",
                        "review_id": decision.review_id,
                        "reviewer_type": manifest.reviewer_type,
                    },
                ],
                issues=issues,
            )
        )


def load_native(
    root: Path, *, require_admissions=False, require_body_reviews=False,
    require_body_recoveries=False,
) -> ImportBatch:
    root = Path(root).resolve()
    batch = ImportBatch()
    source = NATIVE_CSV.as_posix()
    headers, rows = _csv_rows(root / NATIVE_CSV, source, batch)
    if not headers:
        return batch
    rows = _validate_rows(headers, rows, (*DISPLAY_FIELDS, "article"), source, batch)
    supplement = _metadata(root, batch)
    metadata_source = NATIVE_METADATA.as_posix()
    counts = Counter(row["url"].strip() for _, row in rows)
    for number, row in rows:
        url = row["url"].strip()
        if counts[url] > 1:
            batch.rejected.append(
                Issue(
                    code="duplicate_url",
                    severity="error",
                    source=source,
                    row=number,
                    detail="Duplicate baseline URL; every ambiguous row is isolated, not silently merged.",
                )
            )
            continue
        metadata = supplement.get(url)
        metadata_row = metadata[1] if metadata else {}
        issues, retrievable = _body_issues(row["article"], source, number)
        published, date_issues, precision = _date_value(
            row["date"],
            source=source,
            row=number,
            fmt="%d/%m/%Y",
            notes=_string(metadata_row.get("notes")),
        )
        issues.extend(date_issues)
        disclosure = _string(metadata_row.get("disclosure language"))
        if not metadata:
            issues.append(
                Issue(
                    code="metadata_unmatched",
                    source=source,
                    row=number,
                    detail="No unique URL-matched raw metadata supplement; disclosure remains unknown.",
                )
            )
        elif not disclosure:
            issues.append(
                Issue(
                    code="disclosure_unknown",
                    source=metadata_source,
                    row=metadata[0],
                    detail="Raw disclosure is blank/placeholder; does not prove absence on the original page.",
                )
            )
        if disclosure and row.get("Disclosure language", "").strip() == "0":
            issues.append(
                Issue(
                    code="disclosure_binary_mismatch",
                    severity="info",
                    source=source,
                    row=number,
                    detail="Cleaned binary is 0 despite raw disclosure text; binary not used as disclosure-presence evidence.",
                )
            )
        fields = {
            field: _string(row.get(field))
            for field in ("publisher", "title", "sponsor", "keyword")
        }
        for field, value in fields.items():
            if not value:
                issues.append(
                    Issue(
                        code=f"{field}_missing",
                        source=source,
                        row=number,
                        detail=f"{field} unavailable; unrelated features remain enabled.",
                    )
                )
        if _string(metadata_row.get("publisher")).startswith(("http://", "https://")):
            issues.append(
                Issue(
                    code="metadata_publisher_invalid",
                    severity="info",
                    source=metadata_source,
                    row=metadata[0],
                    detail="Raw publisher contains a URL; keep the baseline publisher, not the corrupted supplement.",
                )
            )
        reason = asset_url_reason(url)
        if reason:
            issues.append(
                Issue(code="scope_asset_url", source=source, row=number, detail=reason)
            )
        provenance = [
            _provenance(source, batch.source_hashes[source], number, "baseline")
        ]
        if metadata:
            provenance.append(
                _provenance(
                    metadata_source,
                    batch.source_hashes[metadata_source],
                    metadata[0],
                    "disclosure_and_notes",
                    metadata[2],
                )
            )
        batch.records.append(
            RecordInput(
                record_id=_identify("native", url),
                dataset="native",
                url=url,
                **fields,
                published_at=published,
                body=row["article"],
                disclosure=disclosure,
                countable=not bool(reason),
                retrievable=retrievable and not bool(reason),
                retrieval_end=retrieval_boundary(row["article"]),
                raw={
                    "baseline": row,
                    "metadata": metadata_row,
                    "date_precision": precision,
                    "date_source": source,
                    "date_review_notes_source": metadata_source
                    if date_issues and metadata
                    else "",
                    "body_sha256": body_hash(row["article"]),
                },
                provenance=provenance,
                issues=issues,
            )
        )
    # Compare against all baseline URLs, including rejected duplicates: a failed
    # baseline row must not quietly re-enter via the supplemental source.
    _add_candidates(root, {row["url"].strip() for _, row in rows}, batch)
    _apply_admissions(root, batch, supplement, required=require_admissions)
    mark_duplicate_bodies(batch.records)
    _attach_legacy(root, batch.records, batch)
    _attach_archive_candidates(root, batch.records, batch)
    apply_body_reviews(root, batch, required=require_body_reviews)
    apply_body_recoveries(root, batch, required=require_body_recoveries)
    return batch


def load_social(path: Path, mapping: dict) -> ImportBatch:
    """Import a supplied export only; never collect posts or infer column mappings."""
    batch = ImportBatch()
    path = Path(path)
    source = str(path.resolve())
    columns = mapping.get("columns") if isinstance(mapping, dict) else None
    constants = mapping.get("constants", {}) if isinstance(mapping, dict) else {}
    allowed = {
        "url",
        "body",
        "platform",
        "account",
        "publisher",
        "title",
        "published_at",
        "sponsor",
        "keyword",
        "archive_url",
        "disclosure",
    }
    if (
        not isinstance(columns, dict)
        or not isinstance(constants, dict)
        or not {"url", "body"}.issubset(columns)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in columns.items()
        )
        or not set(columns).issubset(allowed)
        or not set(constants).issubset(allowed)
        or set(columns) & set(constants)
        or "platform" not in (set(columns) | set(constants))
        or any(not isinstance(value, str) for value in constants.values())
    ):
        batch.rejected.append(
            Issue(
                code="social_mapping_invalid",
                severity="error",
                source=source,
                detail="Explicit columns mapping requires url/body and a mapped or constant platform; keys must be supported and unambiguous.",
            )
        )
        return batch
    date_format = mapping.get("date_format")
    if date_format is not None and not isinstance(date_format, str):
        batch.rejected.append(
            Issue(
                code="social_mapping_invalid",
                severity="error",
                source=source,
                detail="date_format must be a strptime format string or omitted for ISO dates.",
            )
        )
        return batch
    headers, rows = _csv_rows(path, source, batch)
    if not headers:
        return batch
    absent = sorted(set(columns.values()) - set(headers))
    if absent:
        batch.rejected.append(
            Issue(
                code="social_mapping_column_missing",
                severity="error",
                source=source,
                detail=f"Configured source columns not present: {absent}; no inferred substitutes.",
            )
        )
        return batch
    projected = [
        (number, {**{key: raw[value] for key, value in columns.items()}, **constants})
        for number, raw in rows
    ]
    canonical_headers = list(dict.fromkeys([*columns, *constants]))
    projected = _validate_rows(
        canonical_headers,
        projected,
        ("url", "body", "platform"),
        source,
        batch,
        ("platform",),
    )
    originals = dict(rows)
    counts = Counter(row["url"].strip() for _, row in projected)
    for number, row in projected:
        url = row["url"].strip()
        if counts[url] > 1:
            batch.rejected.append(
                Issue(
                    code="duplicate_url",
                    severity="error",
                    source=source,
                    row=number,
                    detail="Duplicate post URL; ambiguous rows isolated.",
                )
            )
            continue
        issues, retrievable = _body_issues(row["body"], source, number, social=True)
        published, date_issues, precision = _date_value(
            row.get("published_at"), source=source, row=number, fmt=date_format
        )
        issues.extend(date_issues)
        archive_url = _string(row.get("archive_url"))
        if archive_url and not valid_url(archive_url):
            issues.append(
                Issue(
                    code="archive_url_invalid",
                    source=source,
                    row=number,
                    detail="Archive link is not a valid HTTP(S) URL; raw value preserved.",
                )
            )
            archive_url = ""
        fields = {
            key: _string(row.get(key))
            for key in (
                "publisher",
                "title",
                "sponsor",
                "keyword",
                "platform",
                "account",
                "disclosure",
            )
        }
        for key in ("sponsor", "account"):
            if not fields[key]:
                issues.append(
                    Issue(
                        code=f"{key}_missing",
                        source=source,
                        row=number,
                        detail=f"{key} unknown; not inferred from account, keywords, or absence of disclosure.",
                    )
                )
        batch.records.append(
            RecordInput(
                record_id=_identify("social", url),
                dataset="social",
                url=url,
                **fields,
                body=row["body"],
                published_at=published,
                archive_url=archive_url,
                retrievable=retrievable,
                retrieval_end=retrieval_boundary(row["body"]),
                raw={
                    "source_row": originals[number],
                    "mapping": mapping,
                    "date_precision": precision,
                    "date_source": source,
                    "body_sha256": body_hash(row["body"]),
                },
                provenance=[
                    _provenance(
                        source, batch.source_hashes[source], number, "social_export"
                    )
                ],
                issues=issues,
            )
        )
    mark_duplicate_bodies(batch.records)
    return batch
