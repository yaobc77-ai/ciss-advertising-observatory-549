"""Production reader boundary checks; all test media below are fictional fixtures."""

import json
from copy import deepcopy
from datetime import date

import pytest
from pydantic import ValidationError

from observatory.media_evidence import (
    ImageLocation,
    MediaAsset,
    MediaBundle,
    TimedText,
    image_evidence,
    sha256_bytes,
    sha256_text,
    text_evidence,
    video_evidence,
)
from observatory.media_reader import MediaEvidenceReader, MediaReadRequest
from observatory.models import Filters

VERSION = sha256_text("fictional-current-version")
BODY = "The company discusses carbon capture. It also describes community jobs."


@pytest.fixture
def mounted(tmp_path):
    assets, artifacts, evidence = [], [], []
    for kind, raw in (("text", BODY.encode()), ("image", b"fictional image bytes"), ("video", b"fictional video bytes")):
        path = tmp_path / (kind + ".fixture")
        path.write_bytes(raw)
        asset = MediaAsset(asset_id=kind, record_id="fixture-ad-1", version_id=VERSION, dataset="native", media_type=kind, title="Fictional fixture title", source_url="https://asset.example.test/untrusted-title", local_path=path.name, sha256=sha256_bytes(raw), identity_bound=True, retrievable=True, duration_ms=10000 if kind == "video" else None)
        assets.append(asset)
        if kind == "text":
            artifact, units = text_evidence(asset, BODY)
        elif kind == "image":
            artifact, units = image_evidence(asset, "Carbon capture equipment is visible.", origin="ocr", producer="private/internal/operator", location=ImageLocation(screenshot_sha256=asset.sha256, page_number=2, region=[0.1, 0.2, 0.8, 0.9]))
        else:
            artifact, units = video_evidence(asset, [TimedText(text="Carbon capture creates jobs.", start_ms=1000, end_ms=4000)], origin="publisher_caption", producer="private/internal/captions")
        artifacts.append(artifact)
        evidence.extend(units)
    bundle = MediaBundle(assets=assets, text_artifacts=artifacts, evidence=evidence)
    row = {"record_id": "fixture-ad-1", "version_id": VERSION, "dataset": "native", "retrievable": True, "active": True, "body": BODY, "body_hash": sha256_text(BODY), "retrieval_ranges": None, "retrieval_end": None, "title": "Current database title", "url": "https://current.example.test/ad"}
    calls = []

    def current(filters, record_id):
        calls.append((filters.model_dump(), record_id))
        return deepcopy(row)

    state = {"bundle": bundle, "row": row, "calls": calls, "current": current, "root": tmp_path, "path": tmp_path / "bundle.json"}
    save_bundle(state)
    return state


def save_bundle(state, bundle=None):
    if bundle is not None:
        state["bundle"] = bundle
    raw = state["bundle"].model_dump_json(indent=2).encode()
    state["path"].write_bytes(raw)
    state["sha"] = sha256_bytes(raw)


def reader(state, **changes):
    config = {"bundle_path": state["path"], "bundle_sha256": state["sha"], "asset_root": state["root"], "current_record": state["current"]}
    config.update(changes)
    return MediaEvidenceReader(**config)


def test_default_unconfigured_never_reads_a_source():
    def forbidden(*args):
        raise AssertionError("Unconfigured reads must stop before DB access")

    result = MediaEvidenceReader(current_record=forbidden).read("carbon", filters=Filters())
    assert result["status"] == "not_configured" and result["source_refs"] == []
    assert result["model_calls"] == 0


def test_partial_config_is_unavailable_and_contains_no_private_path(mounted):
    result = reader(mounted, asset_root="").read("carbon", filters=Filters())
    assert result["status"] == "unavailable" and not mounted["calls"]
    assert str(mounted["path"]) not in json.dumps(result)


def test_original_text_has_exact_current_offsets_and_bounded_public_fields(mounted):
    result = reader(mounted).read("carbon capture", filters=Filters(), media_types=["text"])
    assert result["status"] == "ok"
    ref = result["source_refs"][0]
    assert ref["evidence_text"] == BODY[ref["start"]:ref["end"]]
    assert ref["quote_from_original_body"] is True
    assert ref["body_hash"] == sha256_text(BODY)
    assert ref["title"] == "Current database title"
    assert ref["source_url"] == "https://current.example.test/ad"
    serialized = json.dumps(result)
    assert all(key not in serialized for key in ("local_path", "producer", "raw", "private/internal", str(mounted["root"])))
    assert len(mounted["calls"]) == 2 and result["model_calls"] == 0


def test_default_is_image_and_video_with_type_specific_locations(mounted):
    result = reader(mounted).read("carbon", filters=Filters())
    assert result["status"] == "ok"
    refs = result["source_refs"]
    assert {ref["media_type"] for ref in refs} == {"image", "video"}
    assert all(ref["quote_from_original_body"] is False for ref in refs)
    assert all("start" not in ref and "end" not in ref for ref in refs)
    assert {ref["locator"]["kind"] for ref in refs} == {"image_region", "video_time"}
    assert next(ref for ref in refs if ref["media_type"] == "video")["locator"]["start_ms"] == 1000
    assert all(ref["image_or_speech_semantics_verified"] is False for ref in refs)


def test_three_routes_group_one_record_without_losing_locations(mounted):
    result = reader(mounted).read("carbon", filters=Filters(), media_types=["text", "image", "video"], limit=3)
    assert result["status"] == "ok" and len(result["records"]) == 1
    assert len(result["source_refs"]) == 3
    assert result["records"][0]["media_types"] == ["image", "text", "video"]
    assert len(set(result["records"][0]["source_ref_ids"])) == 3
    one = reader(mounted).read("carbon", filters=Filters(), media_types=["text", "image", "video"], limit=1)
    assert len(one["source_refs"]) == 1


def test_filters_are_preserved_and_callback_cannot_mutate_caller(mounted):
    selection = Filters(dataset="native", publishers=["Washington Post"], sponsors=["exxonmobil"], keywords=["climate"], record_ids=["fixture-ad-1"], date_from=date(2020, 1, 1), date_to=date(2024, 12, 31), include_unknown_dates=False, include_inferred_dates=False)
    expected = selection.model_dump()

    def mutate(filters, record_id):
        assert filters.model_dump() == expected
        filters.sponsors.append("tamper")
        return mounted["row"]

    assert reader(mounted, current_record=mutate).read("carbon", filters=selection)["status"] == "ok"
    assert selection.model_dump() == expected


def test_record_id_filter_excludes_before_callback(mounted):
    result = reader(mounted).read("carbon", filters=Filters(record_ids=["other-record"]))
    assert result["status"] == "missing_material" and not mounted["calls"]


def test_callback_scope_exclusion_is_missing_material(mounted):
    selection = Filters(sponsors=["not-the-current-sponsor"])

    def scoped(filters, record_id):
        assert filters.sponsors == selection.sponsors
        return None

    result = reader(mounted, current_record=scoped).read("carbon", filters=selection)
    assert result["status"] == "missing_material" and result["source_refs"] == []


@pytest.mark.parametrize("field,value", [("retrievable", False), ("retrievable", "true"), ("active", False)])
def test_paused_current_record_cannot_inherit_bundle_admission(mounted, field, value):
    mounted["row"][field] = value
    result = reader(mounted).read("carbon", filters=Filters())
    assert result["status"] == "missing_material" and result["source_refs"] == []


@pytest.mark.parametrize("field,value", [("version_id", sha256_text("changed-version")), ("dataset", "social"), ("record_id", "unrelated"), ("body_hash", sha256_text("different body")), ("body", "Changed body")])
def test_wrong_current_identity_fails_closed(mounted, field, value):
    mounted["row"][field] = value
    result = reader(mounted).read("carbon", filters=Filters())
    assert result["status"] == "unavailable" and result["source_refs"] == []


def test_current_dataset_mismatch_even_with_all_selection(mounted):
    mounted["row"]["dataset"] = "social"
    assert reader(mounted).read("carbon", filters=Filters(dataset="all"))["status"] == "unavailable"


def test_source_change_between_reads_publishes_no_excerpt(mounted):
    count = 0

    def changed(filters, record_id):
        nonlocal count
        count += 1
        row = deepcopy(mounted["row"])
        if count > 1:
            row["retrievable"] = False
        return row

    result = reader(mounted, current_record=changed).read("carbon", filters=Filters())
    assert count == 2 and result["status"] == "unavailable" and result["source_refs"] == []


def test_current_search_intervals_cannot_be_widened_by_mounted_text(mounted):
    mounted["row"]["retrieval_ranges"] = [(0, 10)]
    result = reader(mounted).read("carbon", filters=Filters(), media_types=["text"])
    assert result["status"] == "unavailable" and result["source_refs"] == []


def test_text_only_bundle_reports_missing_images_instead_of_no_match(mounted):
    bundle = mounted["bundle"]
    save_bundle(mounted, MediaBundle(assets=[bundle.assets[0]], text_artifacts=[bundle.text_artifacts[0]], evidence=[unit for unit in bundle.evidence if unit.media_type == "text"]))
    result = reader(mounted).read("carbon", filters=Filters())
    assert result["status"] == "missing_material"
    assert result["available_media_units"] == {"text": 1, "image": 0, "video": 0}
    assert reader(mounted).read("carbon", filters=Filters(), media_types=["text"])["status"] == "ok"


def test_available_material_without_lexical_match_is_not_classified_absent(mounted):
    result = reader(mounted).read("unicorn", filters=Filters())
    assert result["status"] == "no_match" and "does not establish absence" in result["message"]


def test_synthetic_bundle_is_rejected_even_when_files_exist(mounted):
    raw = mounted["bundle"].model_dump()
    raw["source_mode"] = "synthetic_demo"
    for asset in raw["assets"]:
        asset["synthetic"] = True
    save_bundle(mounted, MediaBundle.model_validate(raw))
    assert reader(mounted).read("carbon", filters=Filters())["status"] == "unavailable"
    assert not mounted["calls"]


@pytest.mark.parametrize("bad_asset", ["missing", "changed", "outside", "credential"])
def test_material_missing_changed_outside_or_secret_is_not_published(mounted, bad_asset):
    raw = mounted["bundle"].model_dump()
    if bad_asset == "missing":
        (mounted["root"] / "image.fixture").unlink()
    elif bad_asset == "changed":
        (mounted["root"] / "image.fixture").write_bytes(b"replaced bytes")
    elif bad_asset == "outside":
        raw["assets"][1]["local_path"] = "../outside.fixture"
    else:
        raw["assets"][1]["local_path"] = ".env"
        (mounted["root"] / ".env").write_bytes(b"fictional image bytes")
    if bad_asset in {"outside", "credential"}:
        save_bundle(mounted, MediaBundle.model_validate(raw))
    result = reader(mounted).read("carbon", filters=Filters())
    assert result["status"] == "unavailable" and result["source_refs"] == []
    assert str(mounted["root"]) not in json.dumps(result)


def test_bundle_hash_mismatch_stops_before_source_reads(mounted):
    result = reader(mounted, bundle_sha256=sha256_text("wrong bundle")).read("carbon", filters=Filters())
    assert result["status"] == "unavailable" and not mounted["calls"]


def test_files_or_bundle_changed_during_current_read_do_not_leak_partial_result(mounted):
    count = 0

    def changed(filters, record_id):
        nonlocal count
        count += 1
        if count > 1:
            mounted["path"].write_bytes(mounted["path"].read_bytes() + b" ")
        return mounted["row"]

    result = reader(mounted, current_record=changed).read("carbon", filters=Filters())
    assert result["status"] == "unavailable" and result["records"] == []


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://user:password@example.test/ad", "file:///private/source"])
def test_unsafe_current_links_are_blank(mounted, url):
    mounted["row"]["url"] = url
    result = reader(mounted).read("carbon", filters=Filters())
    assert result["status"] == "ok" and all(ref["source_url"] == "" for ref in result["source_refs"])


def test_show_source_links_false_hides_bundle_and_current_links(mounted):
    result = reader(mounted, show_source_links=False).read("carbon", filters=Filters())
    assert result["status"] == "ok" and all(ref["source_url"] == "" for ref in result["source_refs"])
    assert "example.test" not in json.dumps(result)


@pytest.mark.parametrize("field,value", [("local_path", "C:/secret"), ("url", "https://example.test"), ("current_versions", {"ad": VERSION}), ("asset_root", "/tmp")])
def test_user_request_cannot_supply_paths_urls_or_versions(field, value):
    with pytest.raises(ValidationError):
        MediaReadRequest.model_validate({"query": "carbon", field: value})


@pytest.mark.parametrize("params", [{"query": " "}, {"query": "?"}, {"query": "carbon", "limit": True}, {"query": "carbon", "limit": 6}, {"query": "carbon", "media_types": []}, {"query": "carbon", "media_types": ["image", "image"]}, {"query": "carbon", "media_types": ["audio"]}])
def test_invalid_or_unbounded_user_request_is_rejected(params):
    with pytest.raises(ValidationError):
        MediaReadRequest.model_validate(params)
