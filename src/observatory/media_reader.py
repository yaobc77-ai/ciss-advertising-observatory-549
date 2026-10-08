"""Read operator-mounted media evidence within the current trusted record scope.

No user paths, URLs, source versions, network access, inference or source writes.
The callback must perform a current, filtered read such as Database.versioned_record.
"""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from .chunking import retrieval_spans
from .media_evidence import (
    SCHEMA_VERSION,
    CharacterLocation,
    ImageLocation,
    MediaBundle,
    citation_payload,
    merge_candidates,
    retrieve_separately,
    verify_local_assets,
)
from .models import Filters, MediaAnswerEvidence

MediaType = Literal["text", "image", "video"]
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_MAX_BUNDLE_BYTES = 16 * 1024 * 1024
_MAX_ASSET_BYTES = 128 * 1024 * 1024
_MAX_SELECTION_UNITS = 15  # At most three existing comparison groups, five each.
_QUALITY = {
    "original_text": "Saved article text; exact current character positions. Claims are not fact-checked.",
    "ocr": "OCR-derived text from an image region; transcription and visual meaning are not independently verified.",
    "vision": "Model description of an image region; not an original-text quotation or a transcript.",
    "human_description": "Human visual description pending semantic review; not a subtitle, transcript or original-text quotation.",
    "publisher_caption": "Publisher-provided captions; speech and visual coverage are not independently verified.",
    "automatic_caption": "Automatic captions; wording and speech alignment are not independently verified.",
    "transcript": "Speech transcription; wording and time alignment are not independently verified.",
}


class MediaReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: Annotated[str, Field(min_length=1, max_length=2000)]
    media_types: Annotated[list[MediaType], Field(min_length=1, max_length=3)] = Field(default_factory=lambda: ["image", "video"])
    limit: Annotated[StrictInt, Field(ge=1, le=5)] = 5

    @field_validator("query")
    @classmethod
    def lexical_query(cls, value):
        if not re.search(r"\w", value):
            raise ValueError("A query must contain searchable text")
        return value

    @field_validator("media_types")
    @classmethod
    def unique_types(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Choose each media type once")
        return value


class MediaEvidenceReader:
    """Fixed configuration, separate modality recall, safe bounded source results.

    ``current_record(filters, record_id)`` must enforce every supplied filter.
    It returns current Database.versioned_record fields, or None outside scope.
    ``limit`` caps returned evidence units, not a statistical advertising count.
    """

    def __init__(self, *, bundle_path: str | Path = "", bundle_sha256: str = "", asset_root: str | Path = "", current_record: Callable[[Filters, str], dict | None], show_source_links: bool = True):
        self._bundle_path = str(bundle_path)
        self._bundle_sha256 = bundle_sha256
        self._asset_root = str(asset_root)
        self._current_record = current_record
        self._show_source_links = show_source_links

    @property
    def source_binding(self):
        """Safe immutable request provenance, without operator filesystem paths."""
        return {"contract": SCHEMA_VERSION, "bundle_sha256": self._bundle_sha256,
                "configuration_complete": all((self._bundle_path, self._bundle_sha256, self._asset_root))}

    @staticmethod
    def _empty(status, message, **extra):
        return {"status": status, "message": message, "records": [], "source_refs": [], "model_calls": 0, "retrieval_method": "separate_lexical_media_rrf", **extra}

    def _load(self):
        path, root = Path(self._bundle_path).resolve(), Path(self._asset_root).resolve()
        if not _SHA.fullmatch(self._bundle_sha256) or not root.is_dir() or path.stat().st_size > _MAX_BUNDLE_BYTES:
            raise ValueError("Invalid fixed media configuration")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != self._bundle_sha256:
            raise ValueError("Fixed bundle hash mismatch")
        bundle = MediaBundle.model_validate_json(raw)
        if bundle.source_mode != "local_saved" or any(asset.synthetic for asset in bundle.assets):
            raise ValueError("Synthetic evidence cannot enter the production reader")
        if len(bundle.assets) > 1000 or len(bundle.evidence) > 20000:
            raise ValueError("Fixed evidence collection exceeds this bounded reader")
        for asset in bundle.assets:
            if not _ID.fullmatch(asset.asset_id) or not _ID.fullmatch(asset.record_id) or not asset.local_path:
                raise ValueError("Invalid asset/source identity")
            source = (root / asset.local_path).resolve()
            if not source.is_relative_to(root) or source.stat().st_size > _MAX_ASSET_BYTES:
                raise ValueError("Asset is outside the permitted root or too large")
        if any(not _ID.fullmatch(unit.evidence_id) or len(unit.text) > 6000 for unit in bundle.evidence):
            raise ValueError("Invalid or unbounded evidence unit")
        verify_local_assets(bundle, root)
        return bundle, root

    def _row(self, filters, record_id):
        row = self._current_record(filters.model_copy(deep=True), record_id)
        if row is None:
            return None
        row = deepcopy(row)
        if (not isinstance(row, dict) or row.get("record_id") != record_id
                or row.get("dataset") not in {"native", "social"}
                or filters.dataset not in {"all", row.get("dataset")}
                or filters.record_ids and record_id not in filters.record_ids
                or not isinstance(row.get("version_id"), str) or not _SHA.fullmatch(row["version_id"])):
            raise ValueError("Current record is not bound to the trusted scope")
        if row.get("retrievable") is not True or row.get("active") is False:
            return None
        if not isinstance(row.get("body"), str) or hashlib.sha256(row["body"].encode()).hexdigest() != row.get("body_hash"):
            raise ValueError("Current saved source body/hash mismatch")
        return row

    @staticmethod
    def _bind(bundle, rows):
        versions = {}
        assets = {asset.asset_id: asset for asset in bundle.assets}
        for asset in bundle.assets:
            row = rows.get(asset.record_id)
            if row is None:
                continue
            if (asset.version_id, asset.dataset) != (row["version_id"], row["dataset"]):
                raise ValueError("Mounted evidence is not the current source version")
            if asset.media_type == "text" and asset.sha256 != row["body_hash"]:
                raise ValueError("Mounted text differs from current source text")
            versions[asset.record_id] = row["version_id"]
        admitted = []
        for unit in bundle.evidence:
            asset, row = assets[unit.asset_id], rows.get(unit.record_id)
            if row is None or not asset.active or not asset.retrievable:
                continue
            if isinstance(unit.locator, CharacterLocation):
                ranges = retrieval_spans(row["body"], retrieval_ranges=row.get("retrieval_ranges"), retrieval_end=row.get("retrieval_end"))
                if not any(a <= unit.locator.start < unit.locator.end <= b for a, b in ranges):
                    raise ValueError("Mounted text extends outside current searchable source intervals")
            elif isinstance(unit.locator, ImageLocation) and unit.locator.video_asset_id is not None:
                parent = assets[unit.locator.video_asset_id]
                if not parent.active or not parent.retrievable:
                    raise ValueError("A video frame cannot inherit a paused parent asset")
            admitted.append(unit)
        return versions, admitted

    def _source_ref(self, unit, bundle, row, versions):
        """One safe projection shared by retrieval and later selection checks."""
        from .service import safe_url

        ref = citation_payload(unit, bundle, current_versions=versions)
        ref.update(dataset=row["dataset"], asset_id=unit.asset_id,
                   title=str(row.get("title") or "")[:300],
                   source_url=safe_url(row.get("url")) if self._show_source_links else "",
                   quality_label=_QUALITY[unit.origin])
        if isinstance(unit.locator, CharacterLocation):
            # Keep the existing original-text MCP result unchanged.
            ref.update(body_hash=unit.locator.body_sha256,
                       start=unit.locator.start, end=unit.locator.end)
        else:
            asset = next(item for item in bundle.assets if item.asset_id == unit.asset_id)
            artifact = next(item for item in bundle.text_artifacts if item.artifact_id == unit.text_artifact_id)
            ref.update(asset_sha256=asset.sha256,
                       text_artifact_id=unit.text_artifact_id,
                       artifact_sha256=artifact.body_sha256,
                       derived_start=unit.derived_start, derived_end=unit.derived_end)
            # Reconstruct nested locations; a frozen model still has mutable lists.
            ref = MediaAnswerEvidence.from_source_ref(ref).model_dump()
        return ref

    def validate_selection(self, refs, *, filters: Filters) -> bool:
        """Recheck exact selected image/video units without running retrieval.

        Reuse this reader instance before and after generation: its configured
        bundle hash is fixed for the request. Empty selection means no media to
        check, not that a material collection is configured or available.
        """
        if not isinstance(refs, (list, tuple)) or len(refs) > _MAX_SELECTION_UNITS:
            return False
        if not refs:
            return True
        if not all((self._bundle_path, self._bundle_sha256, self._asset_root)):
            return False
        try:
            # Do not trust prior model validation or mutable nested locators.
            selected = [MediaAnswerEvidence.model_validate(
                ref.model_dump() if isinstance(ref, MediaAnswerEvidence) else ref
            ) for ref in refs]
            if len({ref.evidence_id for ref in selected}) != len(selected):
                return False
            filters = Filters.model_validate(filters.model_dump())
            bundle, root = self._load()
            record_ids = sorted({ref.record_id for ref in selected})
            rows = {rid: self._row(filters, rid) for rid in record_ids}
            versions, admitted = self._bind(bundle, rows)
            units = {unit.evidence_id: unit for unit in admitted if unit.media_type in {"image", "video"}}

            def matches(current_rows, current_versions):
                for ref in selected:
                    unit = units.get(ref.evidence_id)
                    row = current_rows.get(ref.record_id)
                    if unit is None or row is None or unit.record_id != ref.record_id:
                        return False
                    expected = self._source_ref(unit, bundle, row, current_versions)
                    if expected != ref.model_dump():
                        return False
                return True

            if not matches(rows, versions):
                return False
            # A scope, pause, current title/link or version change during this
            # validation cannot inherit the earlier source read.
            fresh = {rid: self._row(filters, rid) for rid in record_ids}
            final_versions, _ = self._bind(bundle, fresh)
            if not matches(fresh, final_versions):
                return False
            verify_local_assets(bundle, root)  # Also checks a frame's parent video.
            return hashlib.sha256(Path(self._bundle_path).read_bytes()).hexdigest() == self._bundle_sha256
        except Exception:
            # Callers get a stable refusal, never local paths or producer details.
            return False

    def read(self, query: str, *, filters: Filters, media_types: list[MediaType] | None = None, limit: int = 5) -> dict:
        request = MediaReadRequest(query=query, media_types=["image", "video"] if media_types is None else media_types, limit=limit)
        filters = Filters.model_validate(filters.model_dump())
        configured = (bool(self._bundle_path), bool(self._bundle_sha256), bool(self._asset_root))
        if not any(configured):
            return self._empty("not_configured", "No operator-reviewed media collection is configured.")
        if not all(configured):
            return self._empty("unavailable", "The fixed media configuration is incomplete; no source excerpt is published.")
        try:
            bundle, root = self._load()
            identifiers = {asset.record_id for asset in bundle.assets}
            if filters.record_ids:
                identifiers &= set(filters.record_ids)
            rows = {rid: row for rid in sorted(identifiers) if (row := self._row(filters, rid)) is not None}
            versions, admitted = self._bind(bundle, rows)
            available = {kind: sum(unit.media_type == kind for unit in admitted) for kind in ("text", "image", "video")}
            selected = [unit for unit in admitted if unit.media_type in request.media_types]
            if not selected:
                return self._empty("missing_material", "No verified searchable material of the requested types is available within the current selection.", available_media_units=available, requested_media_types=request.media_types)
            scoped = MediaBundle(assets=bundle.assets, text_artifacts=bundle.text_artifacts, evidence=selected)
            routes = retrieve_separately(scoped, request.query, current_versions=versions, per_media_limit=request.limit)
            merged = merge_candidates(routes, bundle=scoped, current_versions=versions)
            if not merged:
                return self._empty("no_match", "The available saved material did not match this query; this does not establish absence from an advertisement.", available_media_units=available, requested_media_types=request.media_types)
            units = {unit.evidence_id: unit for unit in selected}
            chosen, public_records = [], []
            for group in merged:
                if len(chosen) >= request.limit:
                    break
                # Prefer one unit per present modality before further units.
                candidates = group["evidence"]
                present, diverse, remainder = set(), [], []
                for item in candidates:
                    if item["media_type"] not in present:
                        diverse.append(item)
                        present.add(item["media_type"])
                    else:
                        remainder.append(item)
                picks = (diverse + remainder)[:request.limit - len(chosen)]
                chosen.extend(units[item["evidence_id"]] for item in picks)
                row = rows[group["record_id"]]
                public_records.append({"record_id": group["record_id"], "version_id": group["version_id"], "dataset": row["dataset"], "title": str(row.get("title") or "")[:300], "media_types": sorted({item["media_type"] for item in picks}), "source_ref_ids": [item["evidence_id"] for item in picks], "ranking_score_rrf": group["score"]})
            # A changing source cannot inherit earlier validation during this read.
            fresh = {rid: self._row(filters, rid) for rid in {unit.record_id for unit in chosen}}
            final_versions, _ = self._bind(scoped, fresh)
            for rid, row in fresh.items():
                if row is None or row["version_id"] != versions[rid]:
                    raise ValueError("Source changed or was paused during this read")
            verify_local_assets(bundle, root)
            if hashlib.sha256(Path(self._bundle_path).read_bytes()).hexdigest() != self._bundle_sha256:
                raise ValueError("Mounted collection changed during this read")
            refs = []
            for unit in chosen:
                row = fresh[unit.record_id]
                refs.append(self._source_ref(unit, scoped, row, final_versions))
            return {"status": "ok", "records": public_records, "source_refs": refs, "evidence_units": len(refs), "available_media_units": available, "requested_media_types": request.media_types, "retrieval_method": "separate_lexical_media_rrf", "semantic_accuracy_measured": False, "model_calls": 0, "limits": "Saved derived descriptions and captions are not fact checks, complete article coverage, or verified paid-advertising identities."}
        except Exception:
            return self._empty("unavailable", "Saved media evidence could not be verified against current sources; no source excerpt is published.")
