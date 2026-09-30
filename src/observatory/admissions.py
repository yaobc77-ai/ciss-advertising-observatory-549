"""Versioned local admission decisions, pinned to the reviewed source bytes."""

import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import ImportBatch
from .quality import body_hash

ADMISSIONS_PATH = Path("config/native_admissions.json")


class AdmissionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    review_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    source_row: int = Field(ge=2)
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["include", "exclude", "pending"]
    body_mode: Literal["text", "metadata_only"]
    sponsor_policy: Literal["source", "unknown"]
    date_policy: Literal["source", "unknown"]
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)


class AdmissionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    review_id: str = Field(min_length=1)
    reviewer_type: Literal["ai", "human"]
    scope_note: str = Field(min_length=1)
    source_path: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decisions: list[AdmissionDecision]

    @model_validator(mode="after")
    def unique_decisions(self):
        for name in ("url", "review_id", "source_row"):
            values = [getattr(item, name) for item in self.decisions]
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate admission {name}")
        return self


def read_admissions(root: Path, batch: ImportBatch, *, required=False):
    """Validate every decision before modifying a batch or publishing its snapshot."""
    path = root / ADMISSIONS_PATH
    if not path.is_file():
        if required:
            raise ValueError(
                f"Required admission manifest is missing: {ADMISSIONS_PATH}"
            )
        return None
    data = path.read_bytes()
    manifest = AdmissionManifest.model_validate_json(data)
    if batch.source_hashes.get(manifest.source_path) != manifest.source_sha256:
        raise ValueError(
            "Admission source is missing or changed; review the new source before import"
        )
    candidates = {candidate["url"]: candidate for candidate in batch.candidates}
    for decision in manifest.decisions:
        candidate = candidates.get(decision.url)
        if candidate is None:
            raise ValueError(
                f"Reviewed URL is no longer a unique additional candidate: {decision.review_id}"
            )
        source = candidate["provenance"][0]
        if (
            source["source"] != manifest.source_path
            or source["row"] != decision.source_row
            or body_hash(candidate["raw"]["article"]) != decision.body_sha256
        ):
            raise ValueError(f"Admission row or body changed: {decision.review_id}")
    batch.source_hashes[ADMISSIONS_PATH.as_posix()] = hashlib.sha256(data).hexdigest()
    return manifest
