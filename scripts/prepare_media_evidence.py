"""Prepare local multimodal evidence and human material queues without inference."""

from __future__ import annotations

import argparse
import base64
import importlib.metadata
import json
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from observatory.chunking import retrieval_spans
from observatory.evaluate import canonical_digest
from observatory.media_evidence import (
    ImageLocation,
    ManualMediaSubmission,
    MediaAsset,
    MediaBundle,
    TimedText,
    citation_payload,
    image_evidence,
    merge_candidates,
    record_manual_material,
    retrieve_separately,
    sha256_bytes,
    sha256_text,
    stable_id,
    text_evidence,
    verify_local_assets,
    video_evidence,
)
from observatory.models import RecordInput

DEFAULT_TRIAGE = ".runtime/video_recovery_20261006/triage/validated/triage.json"
DEFAULT_SNAPSHOT = ".runtime/native_truncated_review_20261006/current_materials/content_review/source_snapshot.json"
DEFAULT_PROBE = ".runtime/video_caption_probe_20261006/metadata_probe.json"
DEFAULT_PAGE_CHECK = ".runtime/video_caption_probe_20261006/page_source_checks.json"
DEFAULT_OUT = ".runtime/decision_followup_20261006/media"


def inside(root: Path, relative: str, *, exists=False) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or (exists and not path.is_file()):
        raise ValueError("All input/output files must be explicitly inside the workspace")
    if any(part.lower().startswith(".env") or part.lower() in {"secrets", "credentials", ".ssh"} for part in Path(relative).parts):
        raise ValueError("Credentials are not media inputs")
    return path


def save(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != raw:
        raise ValueError(f"Preserve the existing artifact; choose a fresh output directory: {path}")
    if not path.exists():
        path.write_bytes(raw)


def save_json(path: Path, value: dict) -> None:
    save(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def validate_snapshot_records(rows: list[dict]) -> dict[str, dict]:
    """Check frozen media records using runtime identity and range contracts.

    Preserve the original payload and its hash. This helper reads no question
    definitions and performs no database or model operations.
    """
    if not isinstance(rows, list) or not rows or len(rows) > 20_000:
        raise ValueError("frozen_source_records_required")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            raise ValueError("frozen_source_payload_required")
        payload = row["payload"]
        record_id, body = row.get("record_id"), row.get("body")
        if (not isinstance(record_id, str) or not record_id.strip() or record_id in indexed
                or not isinstance(body, str) or type(row.get("active")) is not bool
                or type(payload.get("countable")) is not bool
                or type(payload.get("retrievable")) is not bool):
            raise ValueError("frozen_source_identity_or_admission_invalid")
        RecordInput.model_validate(payload)
        payload_hash = canonical_digest(payload)
        if (payload.get("record_id") != record_id or payload.get("body") != body
                or row.get("dataset") != payload.get("dataset")
                or row.get("version_id") != payload_hash):
            raise ValueError("database_payload_version_or_identity_mismatch")
        body_hash = sha256_text(body)
        if row.get("body_sha256") != body_hash or row.get("body_hash", body_hash) != body_hash:
            raise ValueError("frozen_body_hash_mismatch")
        if row.get("payload_sha256") != payload_hash:
            raise ValueError("frozen_payload_hash_mismatch")
        ranges = [list(pair) for pair in retrieval_spans(
            body, retrieval_ranges=payload.get("retrieval_ranges"), retrieval_end=payload.get("retrieval_end"))]
        if row.get("retrieval_ranges") != ranges:
            raise ValueError("frozen_retrieval_ranges_mismatch")
        indexed[record_id] = deepcopy(row)
    return indexed


def prepare_inventory(root: Path, out: Path, source_paths: dict[str, str]) -> dict:
    sources, original_bytes = {}, {}
    for name, relative in source_paths.items():
        raw = inside(root, relative, exists=True).read_bytes()
        sources[name] = json.loads(raw)
        original_bytes[relative] = raw
    triage = sources["triage"]
    records = validate_snapshot_records(sources["snapshot"]["records"])
    probe = sources["probe"]
    page_records = {record["record_id"]: record for record in sources["page_check"]["records"]}
    tasks, assets, artifacts, evidence_units, inventory = [], [], [], [], []
    for old in triage["records"]:
        current = records[old["record_id"]]
        payload = current["payload"]
        if payload["url"] != old["original_url"]:
            raise ValueError("Triage and frozen later record URLs do not identify the same record")
        body = current["body"]
        if sha256_text(body) != current["body_sha256"]:
            raise ValueError("Frozen body hash differs from its saved text")
        aid = str(old["upstream_article_id"])
        raw_video_field = old.get("supplied_video_url") or ""
        candidate_urls = [line.strip().rstrip(",") for line in raw_video_field.splitlines() if line.strip().rstrip(",").startswith(("https://", "http://"))]
        embedded = old.get("supplied_embedded_url")
        if embedded and embedded.startswith(("https://", "http://")):
            candidate_urls.append(embedded)
        candidate_urls = list(dict.fromkeys(candidate_urls))
        saved_body = bool(body.strip()) and body.strip().casefold() != "video"
        if old["triage_route"] == "saved_text_candidate_review":
            reason = "已有保存页面文字可单独检索，但它不是视频字幕；素材身份、画面和人声仍待核对。"
        elif "image" in old["triage_route"]:
            reason = "原资料给图片线索，尚无对应图像证据；请补截图、图片来源或画面描述，不安排语音转写。"
        elif "directory" in old["triage_route"]:
            reason = "原资料可能是目录或混合内容，尚未找到对应广告素材；请补具体页面、截图或说明。"
        else:
            reason = "尚无可定位字幕或已确认视频素材；请补原页面、字幕、截图或描述，不把缺材料当成无人声。"
        if not payload.get("countable"):
            reason += " 该记录原统计资格仍关闭，提交素材不自动增加广告数。"
        task = {"task_id": stable_id("media-task", current["record_id"], current["version_id"]), "record_id": current["record_id"], "version_id": current["version_id"], "title": payload["title"], "url": payload["url"], "reason": reason, "voice_status": "unknown", "duration_ms": None, "asset_id": None, "original_article_id": aid, "countable": bool(payload.get("countable")), "status": "pending_material_or_source_review", "candidate_media_urls": candidate_urls, "triage_route": old["triage_route"], "captions_status": "not_obtained", "version_basis": "later_saved_fulltext_snapshot_not_fresh_live_database", "already_saved_page_text": saved_body, "description_is_subtitle": False}
        tasks.append(task)
        item = {**task, "original_input_version_id": old["input_version_id"], "current_saved_body_sha256": current["body_sha256"], "source_identity_approved": False, "visual_semantics_reviewed": False}
        if current["record_id"] == probe["record_id"]:
            item["saved_component_probes"] = [{"ordinal": result["asset_ordinal"], "url": result["url"], "container_audio_presence": result["audio_presence"], "container_subtitle_presence": result["subtitle_presence"], "external_captions": result["external_captions"], "voice_status": "unknown", "scope": "earlier_component_metadata_not_whole_ad_or_speech_check"} for result in probe["results"]]
        if current["record_id"] in page_records:
            item["saved_access_result"] = {key: page_records[current["record_id"]][key] for key in ["status", "raw_tool_result", "http_status"]}
        if saved_body:
            relative = (out / "saved_page_text" / f"{aid}.txt").relative_to(root).as_posix()
            save(root / relative, body.encode("utf-8"))
            asset = MediaAsset(asset_id=stable_id("page-text", current["record_id"], current["version_id"]), record_id=current["record_id"], version_id=current["version_id"], dataset=current["dataset"], media_type="text", title=payload["title"], source_url=payload["url"], local_path=relative, sha256=current["body_sha256"], identity_bound=True, active=bool(current["active"]), retrievable=bool(payload.get("retrievable")))
            ranges = current.get("retrieval_ranges")
            artifact, units = text_evidence(asset, body, retrieval_ranges=[tuple(region) for region in ranges] if ranges is not None else None)
            assets.append(asset)
            artifacts.append(artifact)
            evidence_units.extend(units)
            item["saved_page_asset_id"] = asset.asset_id
            item["saved_page_units"] = len(units)
        inventory.append(item)
    bundle = MediaBundle(assets=assets, text_artifacts=artifacts, evidence=evidence_units)
    checks = verify_local_assets(bundle, root)
    for relative, raw in original_bytes.items():
        if inside(root, relative, exists=True).read_bytes() != raw:
            raise ValueError("An original frozen source changed while preparing media inputs")
    common = {"prepared_at_utc": datetime.now(timezone.utc).isoformat(), "scope": "saved_files_only_not_current_database_or_video_coverage", "source_files": [{"path": path, "sha256": sha256_bytes(raw), "bytes": len(raw)} for path, raw in original_bytes.items()], "model_calls": 0, "network_calls": 0, "database_operations": 0, "image_recognition_runs": 0, "video_transcriptions": 0}
    save_json(out / "media_tasks.json", {"schema_version": "human-media-review-queue-v1", **common, "tasks": tasks})
    save_json(out / "media_inventory.json", {"schema_version": "saved-media-inventory-v1", **common, "records": inventory, "counts": {"placeholder_records": len(tasks), "countable": sum(task["countable"] for task in tasks), "saved_page_text_assets": len(assets), "real_saved_image_assets": 0, "real_saved_video_assets": 0, "real_subtitle_or_transcript_units": 0}})
    save_json(out / "saved_text_bundle.json", bundle.model_dump(mode="json"))
    installed = {}
    for package in ["pydantic", "tiktoken", "pysbd", "pypdf", "Pillow", "yt-dlp", "faster-whisper", "torch"]:
        try:
            installed[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            installed[package] = None
    receipt = {**common, "counts": {"tasks": len(tasks), "saved_page_text_assets": len(assets), "saved_page_units": len(evidence_units)}, "local_hash_checks": checks, "all_queue_voice_status_unknown": all(task["voice_status"] == "unknown" for task in tasks), "real_video_or_image_coverage_measured": False, "model_quality_measured": False, "package_metadata_observed": installed, "existing_commands": {name: shutil.which(name) for name in ["ffmpeg", "ffprobe", "yt-dlp"]}, "user_statement_interpretation": {"original": "无人生则进入人工标注区", "assumption": "理解为无人声；是否有人声仍需要来源证据，不因缺字幕或页面静音而推定。"}}
    save_json(out / "preparation_receipt.json", receipt)
    return receipt


def synthetic_demo(root: Path, out: Path) -> dict:
    version = sha256_text("synthetic demo record version")
    body = "The company proposes carbon capture. An independent reviewer questions its claims."
    contents = {"text": body.encode(), "image": base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2ioAAAAASUVORK5CYII="), "video": b"SYNTHETIC_TIMED_METADATA_FIXTURE_NOT_A_DECODED_VIDEO"}
    assets = []
    for kind, raw in contents.items():
        filename = {"text": "source.txt", "image": "source.png", "video": "synthetic_video_fixture.bin"}[kind]
        path = out / filename
        save(path, raw)
        assets.append(MediaAsset(asset_id="synthetic-" + kind, record_id="synthetic-ad", version_id=version, dataset="native", media_type=kind, title="Synthetic multimodal demo; not a project advertisement", local_path=path.relative_to(root).as_posix(), sha256=sha256_bytes(raw), duration_ms=10000 if kind == "video" else None, identity_bound=True, retrievable=True, synthetic=True))
    text_artifact, text_units = text_evidence(assets[0], body)
    image_artifact, image_units = image_evidence(assets[1], "Synthetic description: a carbon capture diagram.", origin="human_description", producer="synthetic replay description, not actual pixel recognition", location=ImageLocation(screenshot_sha256=assets[1].sha256, region=[0.0, 0.0, 1.0, 1.0]))
    video_artifact, video_units = video_evidence(assets[2], [TimedText(text="Synthetic caption: carbon capture may reduce emissions.", start_ms=1000, end_ms=3000)], origin="automatic_caption", producer="synthetic replay, not actual speech decoding")
    bundle = MediaBundle(assets=assets, text_artifacts=[text_artifact, image_artifact, video_artifact], evidence=text_units + image_units + video_units, source_mode="synthetic_demo")
    checked = verify_local_assets(bundle, root)
    current_versions = {"synthetic-ad": version}
    routes = retrieve_separately(bundle, "carbon capture", current_versions=current_versions)
    merged = merge_candidates(routes, bundle=bundle, current_versions=current_versions)
    manual = record_manual_material(ManualMediaSubmission(task_id="synthetic-review", record_id="synthetic-ad", version_id=version, description="Please review the video visuals.", request_advanced_vision=True), current_versions=current_versions)
    result = {"scope": "synthetic_engineering_demo_not_real_image_or_speech_analysis", "independent_routes": {kind: [hit.model_dump(mode="json") for hit in hits] for kind, hits in routes.items()}, "merged_records": merged, "citations": [citation_payload(unit, bundle, current_versions=current_versions) for unit in bundle.evidence], "pending_manual_note": manual, "local_hash_checks": checked, "image_recognition_runs": 0, "video_decoding_runs": 0, "model_calls": 0, "network_calls": 0, "database_operations": 0}
    save_json(out / "bundle.json", bundle.model_dump(mode="json"))
    save_json(out / "result.json", result)
    return {"synthetic": True, "route_counts": {kind: len(hits) for kind, hits in routes.items()}, "merged_records": len(merged), "model_calls": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="Read existing frozen files, prepare26 pending tasks and saved page-text candidates")
    prepare.add_argument("--triage", default=DEFAULT_TRIAGE)
    prepare.add_argument("--source-snapshot", default=DEFAULT_SNAPSHOT)
    prepare.add_argument("--probe", default=DEFAULT_PROBE)
    prepare.add_argument("--page-check", default=DEFAULT_PAGE_CHECK)
    prepare.add_argument("--out", default=DEFAULT_OUT)
    demo = sub.add_parser("demo", help="Run a clearly synthetic, offline three-medium retrieval example")
    demo.add_argument("--out", default=DEFAULT_OUT + "/synthetic_demo")
    validate = sub.add_parser("check-material", help="Validate a pending manual screenshot/description; do not index or call a model")
    validate.add_argument("--submission", required=True)
    validate.add_argument("--source-snapshot", default=DEFAULT_SNAPSHOT)
    validate.add_argument("--out", required=True)
    args = parser.parse_args()
    root = Path(args.workspace_root).resolve()
    out = inside(root, args.out)
    if args.command == "prepare":
        result = prepare_inventory(root, out, {"triage": args.triage, "snapshot": args.source_snapshot, "probe": args.probe, "page_check": args.page_check})
        print(json.dumps({"status": "prepared_saved_inputs", "counts": result["counts"], "queue": (out / "media_tasks.json").as_posix(), "model_calls": 0}, ensure_ascii=False))
    elif args.command == "demo":
        print(json.dumps(synthetic_demo(root, out), ensure_ascii=False))
    else:
        submission = ManualMediaSubmission.model_validate_json(inside(root, args.submission, exists=True).read_bytes())
        snapshot = json.loads(inside(root, args.source_snapshot, exists=True).read_bytes())
        result = record_manual_material(submission, current_versions={row["record_id"]: row["version_id"] for row in snapshot["records"]})
        save_json(out, result)
        print(json.dumps({"status": result["state"], "retrieval_admitted": False, "model_calls": 0}))


if __name__ == "__main__":
    main()
