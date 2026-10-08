"""Evidence-position, retrieval-scope and manual-material behavior checks."""

import importlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from observatory.evaluate import canonical_digest
from observatory.media_evidence import (
    CharacterLocation,
    ImageLocation,
    ManualMediaSubmission,
    MediaAsset,
    MediaBundle,
    MediaEvidence,
    RetrievalHit,
    TimedText,
    TimeLocation,
    citation_payload,
    image_evidence,
    media_route,
    merge_candidates,
    record_manual_material,
    retrieve_separately,
    sha256_bytes,
    sha256_text,
    text_evidence,
    verify_local_assets,
    video_evidence,
)

VERSION = sha256_text("record-version-1")
OTHER_VERSION = sha256_text("record-version-2")


def asset(kind, *, identifier=None, record="ad-1", **changes):
    data = dict(asset_id=identifier or kind, record_id=record, version_id=VERSION, dataset="native", media_type=kind, title="Synthetic test advertisement", source_url="https://example.test/ad", identity_bound=True, retrievable=True, synthetic=True, sha256=sha256_text("synthetic " + kind))
    if kind == "video":
        data["duration_ms"] = 10000
    data.update(changes)
    return MediaAsset(**data)


def mixed_bundle():
    body = "The company promotes carbon capture. A separate paragraph describes jobs."
    text = asset("text", sha256=sha256_text(body))
    image = asset("image")
    video = asset("video")
    ta, tu = text_evidence(text, body)
    ia, iu = image_evidence(image, "The screenshot shows carbon capture equipment.", origin="human_description", producer="synthetic reviewer", location=ImageLocation(screenshot_sha256=image.sha256, region=[0.0, 0.0, 1.0, 1.0]))
    va, vu = video_evidence(video, [TimedText(text="Our carbon capture investment creates jobs.", start_ms=1000, end_ms=3000)], origin="automatic_caption", producer="synthetic saved caption")
    return MediaBundle(assets=[text, image, video], text_artifacts=[ta, ia, va], evidence=tu + iu + vu, source_mode="synthetic_demo")


def test_three_routes_merge_one_ad_preserve_type_and_location():
    bundle = mixed_bundle()
    routes = retrieve_separately(bundle, "carbon capture", current_versions={"ad-1": VERSION})
    assert all(routes[medium] for medium in ["text", "image", "video"])
    merged = merge_candidates(routes, bundle=bundle, current_versions={"ad-1": VERSION})
    assert len(merged) == 1
    assert set(merged[0]["media_types"]) == {"text", "image", "video"}
    assert {entry["locator"]["kind"] for entry in merged[0]["evidence"]} == {"characters", "image_region", "video_time"}
    assert merged[0]["score"] == pytest.approx(3 / 61)


def test_crlf_unicode_spans_and_excluded_ranges_are_preserved():
    body = "碳捕集。\r\n\r\nDRAFT carbon capture.\r\nFinal disclosure."
    text = asset("text", sha256=sha256_text(body))
    start = body.index("Final")
    artifact, units = text_evidence(text, body, retrieval_ranges=[(0, 5), (start, len(body))])
    bundle = MediaBundle(assets=[text], text_artifacts=[artifact], evidence=units, source_mode="synthetic_demo")
    assert all(unit.text == body[unit.locator.start:unit.locator.end] for unit in bundle.evidence)
    assert "DRAFT" not in " ".join(unit.text for unit in units)
    assert artifact.body == body


def test_original_body_update_cannot_reuse_old_character_positions():
    old = "This claim is old."
    with pytest.raises(ValueError, match="original saved"):
        text_evidence(asset("text", sha256=sha256_text(old)), "This claim is updated.")


def test_ocr_cannot_use_original_text_character_location():
    bundle = mixed_bundle()
    image = next(unit for unit in bundle.evidence if unit.media_type == "image")
    raw = image.model_dump()
    raw["locator"] = CharacterLocation(start=0, end=len(image.text), body_sha256=sha256_text(image.text)).model_dump()
    with pytest.raises(ValidationError, match="reuse text character"):
        MediaEvidence.model_validate(raw)


def test_modified_quote_is_rejected_even_with_same_image_hash():
    bundle = mixed_bundle()
    raw = bundle.model_dump()
    raw["evidence"][1]["text"] = "This is a fabricated image statement."
    with pytest.raises(ValidationError, match="exact span"):
        MediaBundle.model_validate(raw)


def test_wrong_screenshot_hash_is_rejected():
    image = asset("image")
    with pytest.raises(ValueError, match="screenshot hash"):
        image_evidence(image, "A turbine.", origin="vision", producer="unexecuted synthetic adapter", location=ImageLocation(screenshot_sha256=sha256_text("different screenshot"), region=[0.0, 0.0, 1.0, 1.0]))


@pytest.mark.parametrize("region", [[-0.1, 0.0, 1.0, 1.0], [0.0, 0.0, 0.0, 1.0], [0.0, 1.0, 1.0, 0.0], [0.0, 0.0, float("nan"), 1.0]])
def test_invalid_image_region_is_rejected(region):
    with pytest.raises(ValidationError, match="bounding box"):
        ImageLocation(screenshot_sha256=sha256_text("image"), region=region)


def test_frame_uses_image_index_and_binds_video_time_separately():
    video = asset("video")
    image = asset("image", identifier="frame")
    artifact, units = image_evidence(image, "A turbine in frame.", origin="vision", producer="synthetic adapter", location=ImageLocation(screenshot_sha256=image.sha256, region=[0.0, 0.0, 1.0, 1.0], video_asset_id=video.asset_id, video_time_ms=2000))
    bundle = MediaBundle(assets=[video, image], text_artifacts=[artifact], evidence=units, source_mode="synthetic_demo")
    routes = retrieve_separately(bundle, "turbine", current_versions={"ad-1": VERSION})
    assert routes["image"] and not routes["video"]
    assert routes["image"][0].evidence.locator.video_time_ms == 2000


@pytest.mark.parametrize("time_ms", [10000, 11000])
def test_frame_time_outside_parent_video_is_rejected(time_ms):
    video = asset("video")
    image = asset("image", identifier="frame")
    artifact, units = image_evidence(image, "Frame", origin="human_description", producer="reviewer", location=ImageLocation(screenshot_sha256=image.sha256, region=[0.0, 0.0, 1.0, 1.0], video_asset_id=video.asset_id, video_time_ms=time_ms))
    with pytest.raises(ValidationError, match="inside the video"):
        MediaBundle(assets=[video, image], text_artifacts=[artifact], evidence=units, source_mode="synthetic_demo")


def test_multiple_video_segments_keep_exact_derived_offsets_and_coarse_times():
    video = asset("video")
    segments = [TimedText(text="Carbon capture is proposed.", start_ms=1000, end_ms=2000), TimedText(text="The promised results are not verified.", start_ms=4000, end_ms=5000)]
    artifact, units = video_evidence(video, segments, origin="transcript", producer="synthetic transcript")
    bundle = MediaBundle(assets=[video], text_artifacts=[artifact], evidence=units, source_mode="synthetic_demo")
    assert units[1].text == artifact.body[units[1].derived_start:units[1].derived_end]
    assert units[1].locator == TimeLocation(start_ms=4000, end_ms=5000)
    assert all(not citation_payload(unit, bundle, current_versions={"ad-1": VERSION})["quote_from_original_body"] for unit in units)


@pytest.mark.parametrize("start,end", [(2000, 1000), (0, 11000)])
def test_bad_transcript_time_does_not_create_grounded_evidence(start, end):
    with pytest.raises(ValueError):
        video_evidence(asset("video"), [TimedText(text="Claim", start_ms=start, end_ms=end)], origin="transcript", producer="synthetic")


def test_pending_withdrawn_and_old_sources_are_not_retrieved():
    bundle = mixed_bundle()
    raw = bundle.model_dump()
    raw["assets"][0]["active"] = False
    raw["assets"][1]["retrievable"] = False
    guarded = MediaBundle.model_validate(raw)
    routes = retrieve_separately(guarded, "carbon capture", current_versions={"ad-1": OTHER_VERSION})
    assert routes == {"text": [], "image": [], "video": []}
    routes = retrieve_separately(guarded, "carbon capture", current_versions={"ad-1": VERSION})
    assert not routes["text"] and not routes["image"] and routes["video"]


def test_stale_hit_cannot_be_manually_injected_into_merge():
    bundle = mixed_bundle()
    routes = retrieve_separately(bundle, "carbon capture", current_versions={"ad-1": VERSION})
    with pytest.raises(ValueError, match="version mismatch"):
        merge_candidates(routes, bundle=bundle, current_versions={"ad-1": OTHER_VERSION})


def test_pending_hit_cannot_bypass_retrieval_in_merge_or_citation():
    bundle = mixed_bundle()
    raw = bundle.model_dump()
    raw["assets"][1]["retrievable"] = False
    guarded = MediaBundle.model_validate(raw)
    image = next(unit for unit in guarded.evidence if unit.media_type == "image")
    hit = RetrievalHit(evidence=image, score=1.0, rank=1, matched_terms=["carbon"])
    with pytest.raises(ValueError, match="pending or withdrawn"):
        merge_candidates({"image": [hit]}, bundle=guarded, current_versions={"ad-1": VERSION})
    with pytest.raises(ValueError, match="Pending or withdrawn"):
        citation_payload(image, guarded, current_versions={"ad-1": VERSION})


@pytest.mark.parametrize("scope", [{}, {"ad-1": OTHER_VERSION}])
def test_stale_or_out_of_scope_bundle_cannot_bypass_retrieval_in_citation(scope):
    bundle = mixed_bundle()
    with pytest.raises(ValueError, match="stale or out-of-scope"):
        citation_payload(bundle.evidence[0], bundle, current_versions=scope)


def test_duplicate_hit_does_not_duplicate_evidence_or_inflate_record_rank():
    bundle = mixed_bundle()
    routes = retrieve_separately(bundle, "carbon", current_versions={"ad-1": VERSION})
    routes["image"].append(routes["image"][0])
    merged = merge_candidates(routes, bundle=bundle, current_versions={"ad-1": VERSION})
    assert len(merged[0]["evidence"]) == 3
    assert merged[0]["score"] == pytest.approx(3 / 61)


def test_irrelevant_query_returns_no_evidence_not_a_negative_ad_classification():
    bundle = mixed_bundle()
    routes = retrieve_separately(bundle, "unicorn", current_versions={"ad-1": VERSION})
    assert merge_candidates(routes, bundle=bundle, current_versions={"ad-1": VERSION}) == []


@pytest.mark.parametrize("available,voice,expected", [(True, "unknown", "caption_index"), (None, "no_speech", "check_captions"), (False, "speech", "transcription_pending"), (False, "no_speech", "manual_visual"), (False, "unknown", "check_voice_or_manual")])
def test_video_routes_do_not_infer_speech_from_container_or_page_mute(available, voice, expected):
    route = media_route("video", captions_available=available, voice_status=voice)
    assert route["route"] == expected and route["model_calls"] == 0


def test_no_access_routes_to_material_request_not_assumed_silence():
    route = media_route("video", source_accessible=False)
    assert route["route"] == "manual_material" and "不推定视频无声" in route["reason"]


@pytest.mark.parametrize("observations", [{"captions_available": "false"}, {"source_accessible": "false"}])
def test_string_observations_do_not_trigger_an_incorrect_media_route(observations):
    with pytest.raises(ValueError, match="explicit booleans"):
        media_route("video", **observations)


def test_manual_description_is_pending_and_never_turned_into_subtitles_or_model_call():
    note = ManualMediaSubmission(task_id="manual-1", record_id="ad-1", version_id=VERSION, description="The video shows a wind farm.", request_advanced_vision=True)
    saved = record_manual_material(note, current_versions={"ad-1": VERSION})
    assert saved["state"] == "pending_source_and_location_review"
    assert saved["is_subtitle"] is saved["is_transcript"] is saved["retrieval_admitted"] is False
    assert saved["image_content_recognized"] is False and saved["model_calls"] == 0
    assert saved["advanced_vision_status"] == "awaiting_explicit_model_budget_and_authorization"


def test_stale_manual_material_is_rejected():
    note = ManualMediaSubmission(task_id="manual-1", record_id="ad-1", version_id=VERSION, description="A turbine")
    with pytest.raises(ValueError, match="stale"):
        record_manual_material(note, current_versions={"ad-1": OTHER_VERSION})


def test_screenshot_and_description_need_not_claim_any_spoken_words():
    note = ManualMediaSubmission(task_id="manual-1", record_id="ad-1", version_id=VERSION, screenshot_refs=[{"local_path": "saved/frame.png", "sha256": sha256_text("frame bytes"), "region": [0.0, 0.0, 1.0, 1.0]}])
    saved = record_manual_material(note, current_versions={"ad-1": VERSION})
    assert saved["description"] == "" and saved["is_transcript"] is False


def test_hash_verification_detects_source_changes(tmp_path):
    body = "A claim."
    path = tmp_path / "body.txt"
    path.write_bytes(body.encode())
    text = asset("text", sha256=sha256_text(body), local_path="body.txt")
    artifact, units = text_evidence(text, body)
    bundle = MediaBundle(assets=[text], text_artifacts=[artifact], evidence=units, source_mode="synthetic_demo")
    assert verify_local_assets(bundle, tmp_path)[0]["bytes"] == len(body)
    path.write_bytes(b"A changed claim.")
    with pytest.raises(ValueError, match="no longer match"):
        verify_local_assets(bundle, tmp_path)


def test_paths_cannot_escape_permitted_media_root(tmp_path):
    body = "A claim."
    text = asset("text", sha256=sha256_bytes(body.encode()), local_path="../outside.txt")
    artifact, units = text_evidence(text, body)
    bundle = MediaBundle(assets=[text], text_artifacts=[artifact], evidence=units, source_mode="synthetic_demo")
    with pytest.raises(ValueError, match="permitted workspace"):
        verify_local_assets(bundle, tmp_path)


def test_synthetic_bundle_cannot_be_relabelled_as_real_evidence():
    raw = mixed_bundle().model_dump()
    raw["source_mode"] = "local_saved"
    with pytest.raises(ValidationError, match="Synthetic assets"):
        MediaBundle.model_validate(raw)


@pytest.mark.parametrize("operation", ["retrieve", "merge", "citation", "verify"])
def test_mutating_a_frozen_bundle_list_cannot_create_an_original_body_quote(operation, tmp_path):
    bundle = mixed_bundle()
    raw = bundle.evidence[0].model_dump()
    raw.update(evidence_id="fabricated-after-construction", text="FABRICATED", derived_start=0, derived_end=10)
    fabricated = MediaEvidence.model_validate(raw)
    bundle.evidence.append(fabricated)
    with pytest.raises(ValidationError, match="exact span"):
        if operation == "retrieve":
            retrieve_separately(bundle, "FABRICATED", current_versions={"ad-1": VERSION})
        elif operation == "merge":
            hit = RetrievalHit(evidence=fabricated, score=1.0, rank=1, matched_terms=["fabricated"])
            merge_candidates({"text": [hit]}, bundle=bundle, current_versions={"ad-1": VERSION})
        elif operation == "citation":
            citation_payload(fabricated, bundle, current_versions={"ad-1": VERSION})
        else:
            verify_local_assets(bundle, tmp_path)


def test_mutating_a_manual_screenshot_region_remains_invalid_at_save_boundary():
    note = ManualMediaSubmission(task_id="manual-1", record_id="ad-1", version_id=VERSION, screenshot_refs=[{"local_path": "saved/frame.png", "sha256": sha256_text("frame bytes"), "region": [0.0, 0.0, 1.0, 1.0]}])
    note.screenshot_refs[0].region[2] = -0.5
    with pytest.raises(ValidationError, match="bounding box"):
        record_manual_material(note, current_versions={"ad-1": VERSION})


def media_preparer(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "scripts"))
    return importlib.import_module("prepare_media_evidence")


def frozen_record():
    body = "Saved original body. 保存原文。"
    payload = {"record_id": "ad-1", "dataset": "native", "body": body, "title": "A saved advertisement", "url": "https://example.test/ad", "countable": True, "retrievable": True, "retrieval_ranges": [[0, len(body)]]}
    return {"record_id": "ad-1", "dataset": "native", "active": True, "body": body, "body_sha256": sha256_text(body), "version_id": canonical_digest(payload), "payload_sha256": canonical_digest(payload), "payload": payload, "retrieval_ranges": [[0, len(body)]]}


def test_saved_record_identity_and_ranges_reuse_the_existing_preparer(monkeypatch):
    row = frozen_record()
    checked = media_preparer(monkeypatch).validate_snapshot_records([row])["ad-1"]
    assert checked["body"] == row["body"]
    assert checked["version_id"] == canonical_digest(row["payload"])
    assert checked["retrieval_ranges"] == row["retrieval_ranges"]


@pytest.mark.parametrize("mutation", ["body", "version", "payload_hash", "dataset", "ranges", "active", "admission", "duplicate"])
def test_changed_frozen_record_cannot_borrow_old_identity(monkeypatch, mutation):
    row = frozen_record()
    rows = [row]
    if mutation == "body":
        row["body"] = "FABRICATED frozen-body quote."
        row["body_sha256"] = sha256_text(row["body"])
    elif mutation == "version":
        row["version_id"] = OTHER_VERSION
    elif mutation == "payload_hash":
        row["payload_sha256"] = OTHER_VERSION
    elif mutation == "dataset":
        row["dataset"] = "social"
    elif mutation == "ranges":
        row["retrieval_ranges"] = [[0, 5]]
    elif mutation == "active":
        row["active"] = "false"
    elif mutation == "admission":
        row["payload"]["retrievable"] = "false"
        row["version_id"] = row["payload_sha256"] = canonical_digest(row["payload"])
    else:
        rows.append(deepcopy(row))
    with pytest.raises(ValueError):
        media_preparer(monkeypatch).validate_snapshot_records(rows)


def test_frozen_body_substitution_is_rejected_before_writing_artifacts(monkeypatch, tmp_path):
    row = frozen_record()
    row["body"] = "FABRICATED frozen-body quote."
    row["body_sha256"] = sha256_text(row["body"])
    sources = {"triage": {}, "snapshot": {"records": [row]}, "probe": {}, "page_check": {"records": []}}
    paths = {}
    for key, value in sources.items():
        filename = key + ".json"
        (tmp_path / filename).write_text(json.dumps(value), encoding="utf-8")
        paths[key] = filename
    out = tmp_path / "should-not-be-written"
    with pytest.raises(ValueError, match="database_payload_version_or_identity_mismatch"):
        media_preparer(monkeypatch).prepare_inventory(tmp_path, out, paths)
    assert not out.exists()
