"""Apply hash-bound, partial PDF text recoveries without editing source characters."""

import hashlib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

from .chunking import retrieval_spans
from .models import ImportBatch, Issue, RecordInput
from .quality import body_hash, inspect_body

BODY_RECOVERIES_PATH = Path("config/native_body_recoveries.json")
NonemptyText = Annotated[str, StringConstraints(strict=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", strict=True)]


class RecoveryPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: StrictInt = Field(ge=1)
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(gt=0)


class RecoveryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: NonemptyText
    url: NonemptyText
    source_row: StrictInt = Field(ge=2)
    previous_body_sha256: Sha256
    source_pdf_path: NonemptyText
    source_pdf_sha256: Sha256
    extracted_text_path: NonemptyText
    extracted_text_sha256: Sha256
    extraction_method: NonemptyText
    pages: list[RecoveryPage] = Field(min_length=1)
    retained_ranges: list[tuple[StrictInt, StrictInt]] = Field(min_length=1)
    identity_basis: NonemptyText
    evidence_refs: list[NonemptyText] = Field(min_length=1)
    limitations: list[NonemptyText] = Field(min_length=1)


class RecoveryManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    review_id: NonemptyText
    reviewer_type: Literal["ai", "human"]
    source_path: NonemptyText
    source_sha256: Sha256
    decisions: list[RecoveryDecision]


def _project_file(root: Path, source: str) -> Path:
    """Reject absolute, traversing and resolved-outside paths on every platform."""
    relative = PurePosixPath(source.replace("\\", "/"))
    windows = PureWindowsPath(source)
    if relative.is_absolute() or windows.drive or windows.root or ".." in relative.parts:
        raise ValueError(f"Recovery source must be project-relative: {source}")
    try:
        path = (root / Path(*relative.parts)).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Recovery source is missing: {source}") from exc
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"Recovery source escapes the project or is not a file: {source}")
    return path


def _validate_pages(pages: list[RecoveryPage], text: str) -> None:
    """Each extractor page is its exact text followed by one U+000C delimiter.

    Embedded form-feeds are not supported by this source contract. A contiguous
    but shifted page boundary must not silently relabel characters as another page.
    """
    end = 0
    for number, page in enumerate(pages, 1):
        if page.page != number or page.start != end or not page.start < page.end <= len(text):
            raise ValueError("Recovery pages must be ordered, contiguous and cover the text")
        if text[page.end - 1] != "\f" or text.count("\f", page.start, page.end) != 1:
            raise ValueError("Recovery pages must each end with exactly one form-feed delimiter")
        end = page.end
    if end != len(text):
        raise ValueError("Recovery pages must cover the complete extracted text")


def apply_body_recoveries(root: Path, batch: ImportBatch, *, required=False) -> None:
    """Validate all assets and decisions before replacing any in-memory record."""
    root = Path(root).resolve()
    manifest_path = root / BODY_RECOVERIES_PATH
    if not manifest_path.is_file():
        if required:
            raise ValueError(f"Required body recovery manifest is missing: {BODY_RECOVERIES_PATH}")
        return
    manifest_data = _project_file(root, BODY_RECOVERIES_PATH.as_posix()).read_bytes()
    manifest = RecoveryManifest.model_validate_json(manifest_data)
    if batch.source_hashes.get(manifest.source_path) != manifest.source_sha256:
        raise ValueError("Recovery baseline source is missing or changed")

    # Cache immutable bytes for this application, and do not publish any hashes
    # until every file, identity, range and replacement RecordInput is valid.
    assets: dict[str, bytes] = {}
    asset_hashes: dict[str, str] = {}

    def checked_bytes(source: str, expected: str) -> bytes:
        if source not in assets:
            assets[source] = _project_file(root, source).read_bytes()
            asset_hashes[source] = hashlib.sha256(assets[source]).hexdigest()
        if asset_hashes[source] != expected:
            raise ValueError(f"Recovery source bytes changed: {source}")
        return assets[source]

    checked_bytes(manifest.source_path, manifest.source_sha256)
    for item in manifest.decisions:
        checked_bytes(item.source_pdf_path, item.source_pdf_sha256)
        checked_bytes(item.extracted_text_path, item.extracted_text_sha256)
    for name in ("record_id", "url", "source_row"):
        values = [getattr(item, name) for item in manifest.decisions]
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate recovery {name}")
    records = {record.record_id: record for record in batch.records}
    if len(records) != len(batch.records):
        raise ValueError("Duplicate record in recovery input batch")
    recovered_text: dict[str, str] = {}
    for item in manifest.decisions:
        record = records.get(item.record_id)
        if (
            record is None
            or record.url != item.url
            or body_hash(record.body) != item.previous_body_sha256
        ):
            raise ValueError(f"Recovery previous body or identity changed: {item.record_id}")
        if not any(
            p.get("source") == manifest.source_path
            and p.get("row") == item.source_row
            and p.get("sha256") == manifest.source_sha256
            for p in record.provenance
        ):
            raise ValueError(f"Recovery baseline row changed: {item.record_id}")
        try:
            # read_text() uses universal newline conversion; decoding the pinned
            # bytes preserves CR, LF, page form-feeds and Unicode exactly.
            body = assets[item.extracted_text_path].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Recovery text is not strict UTF-8: {item.extracted_text_path}") from exc
        _validate_pages(item.pages, body)
        retrieval_spans(body, retrieval_ranges=item.retained_ranges)
        recovered_text[item.record_id] = body

    review_source = BODY_RECOVERIES_PATH.as_posix()
    review_sha = hashlib.sha256(manifest_data).hexdigest()
    replacements = {}
    for item in manifest.decisions:
        record = records[item.record_id]
        body = recovered_text[item.record_id]
        selected = "\n\n".join(body[start:end] for start, end in item.retained_ranges)
        quality_issues = inspect_body(selected)
        for issue in quality_issues:
            issue.source = item.extracted_text_path
        issues = [
            issue.model_copy(update={"detail": "Previous CSV body: " + issue.detail})
            if issue.code == "body_truncated_suspected" else issue
            for issue in record.issues
            if not issue.code.startswith("body_") or issue.code == "body_truncated_suspected"
        ]
        issues.extend(quality_issues)
        issues.append(Issue(
            code="body_partial_recovery", severity="info", source=review_source,
            row=item.source_row,
            detail=f"{manifest.reviewer_type} review {manifest.review_id}: partial extracted-text recovery. "
            + " ".join(item.limitations),
        ))
        if record.annotations:
            issues.append(Issue(
                code="historical_annotations_prior_body", severity="info",
                source=review_source, row=item.source_row,
                detail="Historical automatic labels describe the prior body hash. "
                "They remain in previous_body_annotations and are not labels of the recovered body.",
            ))
        payload = record.model_dump(mode="json")
        raw = payload["raw"]
        raw["previous_body"] = record.body
        raw["previous_body_annotations"] = payload["annotations"]
        if "body_review" in raw:
            raw["previous_body_review"] = raw.pop("body_review")
        raw["body_sha256"] = body_hash(body)
        raw["body_recovery"] = {
            "schema_version": manifest.schema_version,
            "manifest_id": manifest.review_id,
            "reviewer_type": manifest.reviewer_type,
            "source_path": manifest.source_path,
            "source_sha256": manifest.source_sha256,
            "coverage": "partial",
            "previous_retrieval_end": record.retrieval_end,
            "previous_retrieval_ranges": payload["retrieval_ranges"],
            **item.model_dump(mode="json"),
        }
        provenance = [*payload["provenance"], {
            "source": item.source_pdf_path,
            "sha256": item.source_pdf_sha256,
            "source_asset_id": f"sha256:{item.source_pdf_sha256}",
            "role": "content_match_reviewed",
            "basis": item.identity_basis,
            "review_id": manifest.review_id,
            "reviewer_type": manifest.reviewer_type,
            "review_source": review_source,
            "review_sha256": review_sha,
            "coverage": "partial",
            "extracted_text_path": item.extracted_text_path,
            "extracted_text_sha256": item.extracted_text_sha256,
            "evidence_refs": item.evidence_refs,
        }]
        replacements[item.record_id] = RecordInput.model_validate({
            **payload,
            "body": body,
            "retrieval_end": None,
            "retrieval_ranges": item.retained_ranges,
            "retrievable": record.countable
            and record.raw.get("admission_review", {}).get("body_mode") != "metadata_only"
            and not any(issue.severity != "info" for issue in quality_issues),
            "raw": raw,
            "annotations": [],
            "issues": issues,
            "provenance": provenance,
        })
    # Commit only after validation and construction of every replacement.
    batch.records = [replacements.get(record.record_id, record) for record in batch.records]
    batch.source_hashes.update(asset_hashes)
    batch.source_hashes[review_source] = review_sha
