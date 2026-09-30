"""Apply reviewed original-text intervals without rewriting article text."""

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .chunking import retrieval_spans
from .models import ImportBatch, Issue, RecordInput
from .quality import body_hash, inspect_body

BODY_REVIEWS_PATH = Path("config/native_body_ranges.json")


class BodyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: str
    url: str
    source_row: StrictInt
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retained_ranges: list[tuple[StrictInt, StrictInt]]
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class BodyManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    review_id: str
    reviewer_type: Literal["ai", "human"]
    source_path: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decisions: list[BodyDecision]


def apply_body_reviews(root: Path, batch: ImportBatch, *, required=False) -> None:
    path = root / BODY_REVIEWS_PATH
    if not path.is_file():
        if required:
            raise ValueError(
                f"Required body review manifest is missing: {BODY_REVIEWS_PATH}"
            )
        return
    data = path.read_bytes()
    manifest = BodyManifest.model_validate_json(data)
    if batch.source_hashes.get(manifest.source_path) != manifest.source_sha256:
        raise ValueError(
            "Body review source is missing or changed; recheck intervals before import"
        )
    reviewed_ids = [item.record_id for item in manifest.decisions]
    if len(reviewed_ids) != len(set(reviewed_ids)):
        raise ValueError("Duplicate record in body review manifest")
    records = {record.record_id: record for record in batch.records}
    # Validate the whole manifest before changing the in-memory batch.
    for item in manifest.decisions:
        record = records.get(item.record_id)
        if (
            record is None
            or record.url != item.url
            or body_hash(record.body) != item.body_sha256
        ):
            raise ValueError(f"Reviewed body or identity changed: {item.record_id}")
        if not any(
            p.get("source") == manifest.source_path and p.get("row") == item.source_row
            for p in record.provenance
        ):
            raise ValueError(f"Reviewed source row changed: {item.record_id}")
        retrieval_spans(record.body, retrieval_ranges=item.retained_ranges)
    source = BODY_REVIEWS_PATH.as_posix()
    sha = hashlib.sha256(data).hexdigest()
    batch.source_hashes[source] = sha
    for item in manifest.decisions:
        record = records[item.record_id]
        # Concatenation here is only for quality gates. Indexing separately chunks
        # each interval and never creates a quotation across an excluded gap.
        selected_text = "\n\n".join(
            record.body[start:end] for start, end in item.retained_ranges
        )
        issues = inspect_body(selected_text)
        for issue in issues:
            issue.source, issue.row = source, item.source_row
        retained_issues = [
            issue
            for issue in record.issues
            if not issue.code.startswith("body_")
            or issue.code == "body_truncated_suspected"
        ]
        seen = {issue.code for issue in retained_issues}
        retained_issues.extend(issue for issue in issues if issue.code not in seen)
        retained_issues.append(
            Issue(
                code="body_navigation_intervals_reviewed",
                severity="info",
                source=source,
                row=item.source_row,
                detail=f"{manifest.reviewer_type} review {manifest.review_id}: {item.reason}",
            )
        )
        review = {
            "manifest_id": manifest.review_id,
            "reviewer_type": manifest.reviewer_type,
            "previous_retrieval_end": record.retrieval_end,
            **item.model_dump(mode="json"),
        }
        updated = RecordInput.model_validate(
            {
                **record.model_dump(),
                "retrieval_ranges": item.retained_ranges,
                "retrieval_end": None,
                "retrievable": record.countable
                and record.raw.get("admission_review", {}).get("body_mode")
                != "metadata_only"
                and not any(issue.severity != "info" for issue in issues),
                "issues": retained_issues,
                "raw": {**record.raw, "body_review": review},
                "provenance": [
                    *record.provenance,
                    {
                        "source": source,
                        "sha256": sha,
                        "source_asset_id": f"sha256:{sha}",
                        "role": "body_interval_review",
                        "review_id": manifest.review_id,
                        "reviewer_type": manifest.reviewer_type,
                    },
                ],
            }
        )
        records[item.record_id] = updated
    batch.records = [records[record.record_id] for record in batch.records]
