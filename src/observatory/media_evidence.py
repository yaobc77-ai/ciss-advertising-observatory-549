"""Offline, type-preserving evidence preparation for text, images and video.

This module performs no fetching, decoding, OCR, inference or database writes.
Caller-supplied extractions remain derived text, never original speech or pixels.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .chunking import chunk_body, chunk_retrieval_body

SCHEMA_VERSION = "separate-media-evidence-v1"
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identity = Annotated[str, Field(min_length=1, max_length=240)]
MediaType = Literal["text", "image", "video"]
Origin = Literal["original_text", "ocr", "vision", "human_description", "publisher_caption", "automatic_caption", "transcript"]


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def stable_id(prefix: str, *values: object) -> str:
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return prefix + "-" + sha256_text(payload)[:24]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class MediaAsset(Contract):
    asset_id: Identity
    record_id: Identity
    version_id: Sha256
    dataset: Literal["native", "social"]
    media_type: MediaType
    title: str
    source_url: str = ""
    local_path: str | None = None
    sha256: Sha256 | None = None
    duration_ms: int | None = Field(default=None, gt=0)
    identity_bound: bool = False
    active: bool = True
    retrievable: bool = False
    synthetic: bool = False

    @model_validator(mode="after")
    def known_source_for_retrieval(self):
        if self.retrievable and (not self.identity_bound or not self.sha256):
            raise ValueError("Retrieval requires a bound record/version and a saved artifact hash")
        if self.duration_ms is not None and self.media_type != "video":
            raise ValueError("Only video assets have duration_ms")
        return self


class TextArtifact(Contract):
    artifact_id: Identity
    asset_id: Identity
    origin: Origin
    body: str
    body_sha256: Sha256
    producer: str
    semantic_reviewed: bool = False

    @model_validator(mode="after")
    def exact_derived_body(self):
        if self.body_sha256 != sha256_text(self.body):
            raise ValueError("Text artifact body does not match its UTF-8 hash")
        if not self.producer.strip():
            raise ValueError("A producer is required; a description is not an anonymous transcript")
        return self


class CharacterLocation(Contract):
    kind: Literal["characters"] = "characters"
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    body_sha256: Sha256

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("Character interval must be nonempty")
        return self


class ImageLocation(Contract):
    kind: Literal["image_region"] = "image_region"
    screenshot_sha256: Sha256
    page_number: int | None = Field(default=None, ge=1)
    region: list[float] = Field(min_length=4, max_length=4)
    video_asset_id: str | None = None
    video_time_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def region_and_frame(self):
        x0, y0, x1, y1 = self.region
        if not all(math.isfinite(v) for v in self.region) or not 0 <= x0 < x1 <= 1 or not 0 <= y0 < y1 <= 1:
            raise ValueError("Image region must be a normalized, nonempty bounding box")
        if (self.video_asset_id is None) != (self.video_time_ms is None):
            raise ValueError("A video frame requires both parent asset and frame time")
        return self


class TimeLocation(Contract):
    kind: Literal["video_time"] = "video_time"
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    timing_scope: Literal["supplied_segment"] = "supplied_segment"

    @model_validator(mode="after")
    def ordered(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("Video interval must be nonempty")
        return self


Location = Annotated[CharacterLocation | ImageLocation | TimeLocation, Field(discriminator="kind")]


class MediaEvidence(Contract):
    evidence_id: Identity
    asset_id: Identity
    record_id: Identity
    version_id: Sha256
    media_type: MediaType
    text_artifact_id: Identity
    origin: Origin
    text: str
    derived_start: int = Field(ge=0)
    derived_end: int = Field(gt=0)
    locator: Location
    quality_label: str

    @model_validator(mode="after")
    def type_specific_position(self):
        expected = {"text": CharacterLocation, "image": ImageLocation, "video": TimeLocation}
        if not isinstance(self.locator, expected[self.media_type]):
            raise ValueError("Do not reuse text character positions as image or video grounding")
        allowed = {"text": {"original_text"}, "image": {"ocr", "vision", "human_description"}, "video": {"publisher_caption", "automatic_caption", "transcript", "human_description"}}
        if self.origin not in allowed[self.media_type]:
            raise ValueError("Evidence origin does not match its medium")
        if self.derived_end <= self.derived_start or not self.text.strip():
            raise ValueError("Evidence must contain an exact, nonempty derived text span")
        return self


class MediaBundle(Contract):
    schema_version: Literal["separate-media-evidence-v1"] = SCHEMA_VERSION
    assets: list[MediaAsset]
    text_artifacts: list[TextArtifact]
    evidence: list[MediaEvidence]
    source_mode: Literal["local_saved", "synthetic_demo"] = "local_saved"

    @model_validator(mode="after")
    def validate_grounding(self):
        assets = {asset.asset_id: asset for asset in self.assets}
        texts = {text.artifact_id: text for text in self.text_artifacts}
        if len(assets) != len(self.assets) or len(texts) != len(self.text_artifacts):
            raise ValueError("Duplicate asset or text artifact IDs")
        if len({unit.evidence_id for unit in self.evidence}) != len(self.evidence):
            raise ValueError("Duplicate evidence IDs")
        for artifact in self.text_artifacts:
            if artifact.asset_id not in assets:
                raise ValueError("Text artifact references an absent asset")
        for unit in self.evidence:
            asset = assets.get(unit.asset_id)
            artifact = texts.get(unit.text_artifact_id)
            if asset is None or artifact is None or artifact.asset_id != unit.asset_id:
                raise ValueError("Evidence asset/text binding is missing or mismatched")
            if (asset.record_id, asset.version_id, asset.media_type) != (unit.record_id, unit.version_id, unit.media_type):
                raise ValueError("Evidence belongs to a different record, version or medium")
            if artifact.origin != unit.origin or unit.derived_end > len(artifact.body) or artifact.body[unit.derived_start:unit.derived_end] != unit.text:
                raise ValueError("Evidence quote is not an exact span of its own derived text")
            if isinstance(unit.locator, CharacterLocation):
                if (unit.locator.start, unit.locator.end, unit.locator.body_sha256) != (unit.derived_start, unit.derived_end, artifact.body_sha256) or artifact.body_sha256 != asset.sha256:
                    raise ValueError("Original text position/hash must refer to the same saved body")
            elif isinstance(unit.locator, ImageLocation):
                if unit.locator.screenshot_sha256 != asset.sha256:
                    raise ValueError("Image evidence must match its screenshot hash")
                if unit.locator.video_asset_id is not None:
                    parent = assets.get(unit.locator.video_asset_id)
                    if parent is None or parent.media_type != "video" or (parent.record_id, parent.version_id) != (asset.record_id, asset.version_id):
                        raise ValueError("Frame must bind to a video from the same record/version")
                    if parent.duration_ms is None or unit.locator.video_time_ms >= parent.duration_ms:
                        raise ValueError("Frame time needs a known duration and must be inside the video")
            elif asset.duration_ms is None or unit.locator.end_ms > asset.duration_ms:
                raise ValueError("Timed evidence must be inside a known saved video duration")
        if self.source_mode == "local_saved" and any(asset.synthetic for asset in self.assets):
            raise ValueError("Synthetic assets must not appear as real saved source evidence")
        return self


def _validated_bundle(bundle: MediaBundle) -> MediaBundle:
    # Frozen models still contain mutable lists; never trust earlier validation.
    return MediaBundle.model_validate(bundle.model_dump())


def _artifact(asset: MediaAsset, text: str, origin: Origin, producer: str) -> TextArtifact:
    digest = sha256_text(text)
    return TextArtifact(artifact_id=stable_id("derived", asset.asset_id, origin, producer, digest), asset_id=asset.asset_id, origin=origin, body=text, body_sha256=digest, producer=producer)


def _unit(asset: MediaAsset, artifact: TextArtifact, start: int, end: int, location: Location, quality: str) -> MediaEvidence:
    return MediaEvidence(evidence_id=stable_id("media", asset.record_id, asset.version_id, asset.asset_id, artifact.artifact_id, start, end, location.model_dump()), asset_id=asset.asset_id, record_id=asset.record_id, version_id=asset.version_id, media_type=asset.media_type, text_artifact_id=artifact.artifact_id, origin=artifact.origin, text=artifact.body[start:end], derived_start=start, derived_end=end, locator=location, quality_label=quality)


def text_evidence(asset: MediaAsset, body: str, *, max_tokens: int = 600, overlap_tokens: int = 100, retrieval_ranges: list[tuple[int, int]] | None = None) -> tuple[TextArtifact, list[MediaEvidence]]:
    if asset.media_type != "text" or asset.sha256 != sha256_text(body):
        raise ValueError("Text extraction must match the original saved UTF-8 body hash")
    artifact = _artifact(asset, body, "original_text", "saved_utf8_body")
    chunks = chunk_retrieval_body(body, max_tokens=max_tokens, overlap_tokens=overlap_tokens, strategy="sentence", retrieval_ranges=retrieval_ranges)
    units = [_unit(asset, artifact, chunk["start"], chunk["end"], CharacterLocation(start=chunk["start"], end=chunk["end"], body_sha256=artifact.body_sha256), "保存正文；原字符位置") for chunk in chunks]
    return artifact, units


def image_evidence(asset: MediaAsset, derived_text: str, *, origin: Literal["ocr", "vision", "human_description"], producer: str, location: ImageLocation, max_tokens: int = 600) -> tuple[TextArtifact, list[MediaEvidence]]:
    if asset.media_type != "image" or not asset.sha256 or location.screenshot_sha256 != asset.sha256:
        raise ValueError("Image extraction requires its own saved screenshot hash and region")
    artifact = _artifact(asset, derived_text, origin, producer)
    labels = {"ocr": "图片OCR派生文字，未逐字核对；位置属于图片区域", "vision": "模型画面描述，未独立语义审核；不是原文或字幕", "human_description": "人工画面描述，待来源核对；不是字幕或原文引语"}
    chunks = chunk_body(derived_text, max_tokens=max_tokens, overlap_tokens=0, strategy="sentence")
    return artifact, [_unit(asset, artifact, c["start"], c["end"], location, labels[origin]) for c in chunks]


class TimedText(Contract):
    text: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


def video_evidence(asset: MediaAsset, segments: list[TimedText], *, origin: Literal["publisher_caption", "automatic_caption", "transcript", "human_description"], producer: str, max_tokens: int = 600) -> tuple[TextArtifact, list[MediaEvidence]]:
    if asset.media_type != "video" or not asset.sha256 or asset.duration_ms is None:
        raise ValueError("Video evidence needs a saved media hash and known duration")
    body = "\n".join(segment.text for segment in segments)
    artifact = _artifact(asset, body, origin, producer)
    labels = {"publisher_caption": "发布者字幕；未核对语音或画面完整覆盖", "automatic_caption": "自动字幕，未逐字核对；不包含画面理解", "transcript": "自动语音转写，未逐字核对；不包含画面理解", "human_description": "人工视频画面描述，待核对；不是语音转写或字幕"}
    units, cursor = [], 0
    for segment in segments:
        location = TimeLocation(start_ms=segment.start_ms, end_ms=segment.end_ms)
        if location.end_ms > asset.duration_ms:
            raise ValueError("Supplied subtitle/transcript time exceeds this video's duration")
        for chunk in chunk_body(segment.text, max_tokens=max_tokens, overlap_tokens=0, strategy="sentence"):
            units.append(_unit(asset, artifact, cursor + chunk["start"], cursor + chunk["end"], location, labels[origin]))
        cursor += len(segment.text) + 1
    return artifact, units


def verify_local_assets(bundle: MediaBundle, workspace_root: Path) -> list[dict]:
    """Hash-check explicitly listed local assets; do not fetch URLs or infer pixels."""
    bundle = _validated_bundle(bundle)
    root = workspace_root.resolve()
    checked = []
    for asset in bundle.assets:
        if not asset.local_path or not asset.sha256:
            raise ValueError("Local verification requires an explicit file and hash")
        path = (root / asset.local_path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Asset must be an existing file inside the permitted workspace")
        if any(part.lower().startswith(".env") or part.lower() in {".ssh", "secrets", "credentials"} for part in Path(asset.local_path).parts):
            raise ValueError("Credential files are not media")
        raw = path.read_bytes()
        if sha256_bytes(raw) != asset.sha256:
            raise ValueError("Saved media bytes no longer match the planned evidence hash")
        checked.append({"asset_id": asset.asset_id, "sha256": asset.sha256, "bytes": len(raw), "content_semantics_verified": False})
    return checked


class RetrievalHit(Contract):
    evidence: MediaEvidence
    score: float = Field(ge=0, allow_inf_nan=False)
    rank: int = Field(ge=1)
    matched_terms: list[str]


def retrieve_separately(bundle: MediaBundle, query: str, *, current_versions: dict[str, str], per_media_limit: int = 5) -> dict[str, list[RetrievalHit]]:
    """Deterministic lexical baseline, explicitly separate from production pgvector."""
    bundle = _validated_bundle(bundle)
    if per_media_limit < 1 or not query.strip():
        raise ValueError("Use a nonempty query and positive per-medium candidate limit")
    terms = sorted(set(re.findall(r"\w+", query.casefold())))
    if not terms:
        raise ValueError("The query has no lexical terms")
    assets = {asset.asset_id: asset for asset in bundle.assets}
    grouped: dict[str, list[tuple[float, MediaEvidence, list[str]]]] = {kind: [] for kind in ("text", "image", "video")}
    for unit in bundle.evidence:
        asset = assets[unit.asset_id]
        if not asset.retrievable or not asset.active or current_versions.get(unit.record_id) != unit.version_id:
            continue
        words = set(re.findall(r"\w+", unit.text.casefold()))
        matches = sorted(words.intersection(terms))
        if matches:
            grouped[unit.media_type].append((len(matches) / len(terms), unit, matches))
    return {kind: [RetrievalHit(evidence=unit, score=score, rank=index + 1, matched_terms=matches) for index, (score, unit, matches) in enumerate(sorted(candidates, key=lambda c: (-c[0], c[1].evidence_id))[:per_media_limit])] for kind, candidates in grouped.items()}


def merge_candidates(routes: dict[str, list[RetrievalHit]], *, bundle: MediaBundle, current_versions: dict[str, str], rrf_k: int = 60) -> list[dict]:
    """Group a record/version once; preserve every medium and locator within it."""
    bundle = _validated_bundle(bundle)
    if rrf_k < 1 or set(routes) - {"text", "image", "video"}:
        raise ValueError("Invalid RRF constant or media route")
    groups: dict[tuple[str, str], dict] = {}
    known_units = {unit.evidence_id: unit for unit in bundle.evidence}
    assets = {asset.asset_id: asset for asset in bundle.assets}
    for medium, hits in routes.items():
        best_rank: dict[tuple[str, str], int] = {}
        for hit in hits:
            unit = hit.evidence
            if unit.media_type != medium or current_versions.get(unit.record_id) != unit.version_id:
                raise ValueError("Route type/version mismatch; do not merge stale evidence")
            if unit.evidence_id not in known_units or known_units[unit.evidence_id] != unit:
                raise ValueError("Merge candidate does not match its validated evidence bundle")
            if not assets[unit.asset_id].retrievable or not assets[unit.asset_id].active:
                raise ValueError("Cannot merge pending or withdrawn source evidence")
            key = (unit.record_id, unit.version_id)
            group = groups.setdefault(key, {"record_id": unit.record_id, "version_id": unit.version_id, "score": 0.0, "media_types": [], "evidence": [], "matched_terms": []})
            if medium not in group["media_types"]:
                group["media_types"].append(medium)
            if unit.evidence_id not in {entry["evidence_id"] for entry in group["evidence"]}:
                group["evidence"].append(unit.model_dump(mode="json"))
            group["matched_terms"] = sorted(set(group["matched_terms"] + hit.matched_terms))
            best_rank[key] = min(best_rank.get(key, hit.rank), hit.rank)
        for key, rank in best_rank.items():
            groups[key]["score"] += 1 / (rrf_k + rank)
    return sorted(groups.values(), key=lambda row: (-row["score"], row["record_id"], row["version_id"]))


def citation_payload(unit: MediaEvidence, bundle: MediaBundle, *, current_versions: dict[str, str]) -> dict:
    bundle = _validated_bundle(bundle)
    known = {item.evidence_id: item for item in bundle.evidence}
    assets = {asset.asset_id: asset for asset in bundle.assets}
    if unit.evidence_id not in known or known[unit.evidence_id] != unit:
        raise ValueError("Citation must come from the validated evidence bundle")
    if current_versions.get(unit.record_id) != unit.version_id:
        raise ValueError("Cannot cite stale or out-of-scope source evidence")
    asset = assets[unit.asset_id]
    if not asset.retrievable or not asset.active:
        raise ValueError("Pending or withdrawn sources cannot become answer citations")
    literal_source_quote = unit.origin == "original_text"
    return {"evidence_id": unit.evidence_id, "record_id": unit.record_id, "version_id": unit.version_id, "title": asset.title, "source_url": asset.source_url, "media_type": unit.media_type, "origin": unit.origin, "locator": unit.locator.model_dump(), "evidence_text": unit.text, "quote_from_original_body": literal_source_quote, "image_or_speech_semantics_verified": False, "quality_label": unit.quality_label}


def media_route(media_type: MediaType, *, captions_available: bool | None = None, voice_status: Literal["speech", "no_speech", "unknown"] = "unknown", source_accessible: bool = True) -> dict:
    if media_type not in {"text", "image", "video"} or voice_status not in {"speech", "no_speech", "unknown"}:
        raise ValueError("Unknown media type or voice status")
    if (captions_available is not None and type(captions_available) is not bool) or type(source_accessible) is not bool:
        raise ValueError("Caption/access observations must be explicit booleans or unknown captions")
    if not source_accessible:
        return {"route": "manual_material", "reason": "缺可访问素材；请补原页面、字幕、截图或描述，不推定视频无声。", "model_calls": 0}
    if media_type == "text":
        return {"route": "text_index", "reason": "保存正文按原字符位置单独切分。", "model_calls": 0}
    if media_type == "image":
        return {"route": "manual_visual", "reason": "保存图片或截图及页／区域；描述先登记，图像识别另行授权。", "model_calls": 0}
    if captions_available is True:
        return {"route": "caption_index", "reason": "优先复用已有字幕，保留字幕类型和时间。", "model_calls": 0}
    if captions_available is None:
        return {"route": "check_captions", "reason": "外部字幕尚未检查；容器没有字幕流不等于没有字幕。", "model_calls": 0}
    if voice_status == "speech":
        return {"route": "transcription_pending", "reason": "确认有人声且无字幕；可计划一次轻量转写，尚未运行模型。", "model_calls": 0}
    if voice_status == "no_speech":
        return {"route": "manual_visual", "reason": "已确认无人声且无字幕；请补截图、时间和画面描述，后续可计划图像识别。", "model_calls": 0}
    return {"route": "check_voice_or_manual", "reason": "是否有人声未知；有音轨或页面静音都不足以判定。可补截图或描述，不能编造字幕。", "model_calls": 0}


class ScreenshotReference(Contract):
    local_path: str
    sha256: Sha256
    region: list[float] = Field(min_length=4, max_length=4)
    page_number: int | None = Field(default=None, ge=1)
    video_time_ms: int | None = Field(default=None, ge=0)


class ManualMediaSubmission(Contract):
    task_id: Identity
    record_id: Identity
    version_id: Sha256
    asset_id: str | None = None
    reviewer: str = ""
    description: str = ""
    description_kind: Literal["visual_description"] = "visual_description"
    screenshot_refs: list[ScreenshotReference] = Field(default_factory=list)
    time_start_ms: int | None = Field(default=None, ge=0)
    time_end_ms: int | None = Field(default=None, gt=0)
    request_advanced_vision: bool = False

    @model_validator(mode="after")
    def pending_material_only(self):
        if not self.description.strip() and not self.screenshot_refs:
            raise ValueError("Provide at least a visual description or screenshot")
        if (self.time_start_ms is None) != (self.time_end_ms is None) or (self.time_start_ms is not None and self.time_end_ms <= self.time_start_ms):
            raise ValueError("Optional video time range must be complete and nonempty")
        for image in self.screenshot_refs:
            ImageLocation(screenshot_sha256=image.sha256, page_number=image.page_number, region=image.region)
        return self


def record_manual_material(submission: ManualMediaSubmission, *, current_versions: dict[str, str]) -> dict:
    submission = ManualMediaSubmission.model_validate(submission.model_dump())
    if current_versions.get(submission.record_id) != submission.version_id:
        raise ValueError("Manual material is for a stale or out-of-scope record/version")
    return {"schema_version": "manual-media-material-v1", **submission.model_dump(mode="json"), "state": "pending_source_and_location_review", "content_kind": "human_visual_description", "is_subtitle": False, "is_transcript": False, "retrieval_admitted": False, "image_content_recognized": False, "advanced_vision_status": "awaiting_explicit_model_budget_and_authorization" if submission.request_advanced_vision else "not_requested", "model_calls": 0}
