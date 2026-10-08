"""Exact media selection bindings; every saved asset here is a fictional fixture."""

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
from observatory.media_reader import MediaEvidenceReader
from observatory.models import Answer, Citation, Filters, MediaAnswerEvidence

BODY = "The fictional sponsor discusses carbon capture and jobs."
VERSION = sha256_text("fictional-current-source")


def mount(state, bundle=None):
    if bundle is not None:
        state["bundle"] = bundle
    raw = state["bundle"].model_dump_json(indent=2).encode()
    state["path"].write_bytes(raw)
    state["sha"] = sha256_bytes(raw)


def make_reader(state, **changes):
    config = {"bundle_path": state["path"], "bundle_sha256": state["sha"],
              "asset_root": state["root"], "current_record": state["current"]}
    config.update(changes)
    return MediaEvidenceReader(**config)


@pytest.fixture
def source(tmp_path):
    assets, artifacts, units = [], [], []
    for kind, raw in (("text", BODY.encode()), ("image", b"fictional image"),
                      ("video", b"fictional video")):
        path = tmp_path / f"{kind}.fixture"
        path.write_bytes(raw)
        asset = MediaAsset(asset_id=kind, record_id="fixture-record", version_id=VERSION,
                           dataset="native", media_type=kind, title="Saved fixture title",
                           source_url="https://private.example.test/unused-asset-link",
                           local_path=path.name, sha256=sha256_bytes(raw),
                           identity_bound=True, retrievable=True,
                           duration_ms=10000 if kind == "video" else None)
        assets.append(asset)
        if kind == "text":
            artifact, evidence = text_evidence(asset, BODY)
        elif kind == "image":
            artifact, evidence = image_evidence(
                asset, "Carbon capture equipment is visible.", origin="ocr",
                producer="private/operator/fixture",
                location=ImageLocation(screenshot_sha256=asset.sha256, page_number=2,
                                       region=[0.1, 0.2, 0.8, 0.9]))
        else:
            artifact, evidence = video_evidence(
                asset, [TimedText(text="Carbon capture supports jobs.", start_ms=1000, end_ms=4000)],
                origin="publisher_caption", producer="private/operator/caption")
        artifacts.append(artifact)
        units.extend(evidence)
    state = {"bundle": MediaBundle(assets=assets, text_artifacts=artifacts, evidence=units),
             "row": {"record_id": "fixture-record", "version_id": VERSION, "dataset": "native",
                     "retrievable": True, "active": True, "body": BODY,
                     "body_hash": sha256_text(BODY), "title": "Current fixture title",
                     "url": "https://current.example.test/fixture", "retrieval_ranges": None,
                     "retrieval_end": None},
             "root": tmp_path, "path": tmp_path / "bundle.json", "calls": []}

    def current(filters, record_id):
        state["calls"].append((deepcopy(filters.model_dump()), record_id))
        return deepcopy(state["row"])

    state["current"] = current
    mount(state)
    return state


def selected(source, reader=None):
    result = (reader or make_reader(source)).read("carbon", filters=Filters())
    assert result["status"] == "ok"
    return result["source_refs"]


def test_reader_supplies_exact_asset_and_derived_artifact_bindings(source):
    refs = selected(source)
    assets = {asset.asset_id: asset for asset in source["bundle"].assets}
    artifacts = {artifact.artifact_id: artifact for artifact in source["bundle"].text_artifacts}
    for ref in refs:
        typed = MediaAnswerEvidence.from_source_ref(ref)
        artifact = artifacts[typed.text_artifact_id]
        assert typed.asset_sha256 == assets[typed.asset_id].sha256
        assert typed.artifact_sha256 == artifact.body_sha256
        assert artifact.body[typed.derived_start:typed.derived_end] == typed.evidence_text
        assert typed.quote_from_original_body is False
        assert typed.image_or_speech_semantics_verified is False
        assert not hasattr(typed, "start") and not hasattr(typed, "end")
    dumped = json.dumps(refs)
    assert all(private not in dumped for private in ("local_path", "producer", "raw",
                                                      "private/operator", str(source["root"])))


def test_body_citations_and_answer_defaults_remain_compatible():
    citation = Citation.model_validate({"evidence_id": "old-body", "quote": "A body quote."})
    assert citation.evidence_type == "article_text" and citation.origin == "original_text"
    answer = Answer.model_validate({"status": "answered", "answer": "Old answer.",
                                   "citations": [{"evidence_id": "old-body", "quote": "A body quote."}]})
    assert answer.media_evidence == []
    assert Answer.model_validate_json(answer.model_dump_json()) == answer


def test_narrow_mapping_does_not_publish_private_extra_fields(source):
    ref = selected(source)[0]
    typed = MediaAnswerEvidence.from_source_ref({**ref, "local_path": "C:/private",
                                               "producer": "private/operator", "raw": {"secret": True}})
    assert "private" not in typed.model_dump_json()
    with pytest.raises(ValidationError):
        MediaAnswerEvidence.model_validate({**ref, "local_path": "C:/private"})
    with pytest.raises(ValidationError):
        typed.asset_sha256 = "a" * 64


@pytest.mark.parametrize("change", [
    {"media_type": "text"}, {"origin": "original_text"}, {"origin": "transcript"},
    {"quote_from_original_body": True}, {"quote_from_original_body": 0},
    {"image_or_speech_semantics_verified": True}, {"image_or_speech_semantics_verified": 0},
    {"derived_start": True}, {"derived_start": "0"}, {"derived_end": 1},
    {"asset_sha256": "bad"}, {"artifact_sha256": "BAD"},
    {"text_artifact_id": "C:/private/extraction"},
    {"source_url": "file:///C:/private/image"},
    {"source_url": "https://name:password@example.test/ad"},
    {"source_url": "javascript:alert(1)"},
])
def test_media_contract_rejects_type_promotion_bad_bindings_and_private_links(source, change):
    ref = next(ref for ref in selected(source) if ref["media_type"] == "image")
    with pytest.raises(ValidationError):
        MediaAnswerEvidence.from_source_ref({**ref, **change})


def test_wrong_medium_locator_or_image_hash_is_rejected(source):
    refs = selected(source)
    image = next(ref for ref in refs if ref["media_type"] == "image")
    video = next(ref for ref in refs if ref["media_type"] == "video")
    with pytest.raises(ValidationError):
        MediaAnswerEvidence.from_source_ref({**image, "locator": video["locator"]})
    with pytest.raises(ValidationError):
        MediaAnswerEvidence.from_source_ref({**image, "asset_sha256": "b" * 64})


def test_media_text_cannot_defer_nul_failure_until_database_audit(source):
    ref = next(ref for ref in selected(source) if ref["media_type"] == "image")
    with pytest.raises(ValidationError, match="U\\+0000"):
        MediaAnswerEvidence.from_source_ref({**ref, "evidence_text": "Carbon\x00",
                                            "derived_start": 0, "derived_end": 7})


def test_selection_rechecks_without_retrieval_and_accepts_safe_models(source, monkeypatch):
    reader = make_reader(source)
    refs = selected(source, reader)
    source["calls"].clear()

    def no_retrieval(*args, **kwargs):
        raise AssertionError("Selection checks must not rerun topK")

    monkeypatch.setattr("observatory.media_reader.retrieve_separately", no_retrieval)
    monkeypatch.setattr("observatory.media_reader.merge_candidates", no_retrieval)
    assert reader.validate_selection(refs, filters=Filters()) is True
    assert len(source["calls"]) == 2
    assert reader.validate_selection([MediaAnswerEvidence.from_source_ref(ref) for ref in refs],
                                     filters=Filters()) is True


def test_empty_selection_needs_no_configuration_but_nonempty_does(source):
    def forbidden(*args):
        raise AssertionError("Unconfigured selection must never read a record")

    reader = MediaEvidenceReader(current_record=forbidden)
    assert reader.validate_selection([], filters=Filters()) is True
    assert reader.validate_selection(selected(source), filters=Filters()) is False
    assert reader.validate_selection("not-a-selection", filters=Filters()) is False


def test_duplicate_over_limit_and_original_text_selections_are_rejected(source):
    reader = make_reader(source)
    refs = selected(source, reader)
    assert reader.validate_selection([refs[0], refs[0]], filters=Filters()) is False
    assert reader.validate_selection([refs[0]] * 16, filters=Filters()) is False
    text_refs = reader.read("carbon", filters=Filters(), media_types=["text"])["source_refs"]
    assert reader.validate_selection(text_refs, filters=Filters()) is False
    # Its pre-existing original-text contract remains unchanged.
    assert text_refs[0]["evidence_text"] == BODY[text_refs[0]["start"]:text_refs[0]["end"]]
    assert text_refs[0]["quote_from_original_body"] is True
    assert "artifact_sha256" not in text_refs[0]


@pytest.mark.parametrize("field,value", [
    ("title", "Changed current title"), ("url", "https://current.example.test/replaced"),
    ("version_id", sha256_text("changed-version")), ("record_id", "different-record"),
    ("dataset", "social"), ("body", "Changed body"), ("body_hash", "c" * 64),
    ("retrievable", False), ("active", False),
])
def test_current_source_change_invalidates_the_prior_selection(source, field, value):
    reader = make_reader(source)
    refs = selected(source, reader)
    source["row"][field] = value
    assert reader.validate_selection(refs, filters=Filters()) is False


def test_trusted_scope_is_forwarded_and_cannot_be_mutated(source):
    reader = make_reader(source)
    refs = selected(source, reader)
    selection = Filters(publishers=["Outlet"], sponsors=["Company"], keywords=["Collection term"],
                        record_ids=["fixture-record"], date_from=date(2020, 1, 1),
                        date_to=date(2024, 1, 1), include_unknown_dates=False,
                        include_inferred_dates=True)
    expected = selection.model_dump()

    def current(filters, record_id):
        assert filters.model_dump() == expected
        filters.sponsors.append("tamper")
        return source["row"]

    assert make_reader(source, current_record=current).validate_selection(refs, filters=selection)
    assert selection.model_dump() == expected
    assert reader.validate_selection(refs, filters=Filters(record_ids=["outside-record"])) is False
    assert make_reader(source, current_record=lambda *_: None).validate_selection(refs, filters=selection) is False


@pytest.mark.parametrize("change", ["bundle", "image", "video"])
def test_saved_bytes_changed_after_read_are_rejected(source, change):
    reader = make_reader(source)
    refs = selected(source, reader)
    if change == "bundle":
        source["path"].write_bytes(source["path"].read_bytes() + b" ")
    else:
        (source["root"] / f"{change}.fixture").write_bytes(b"replaced fixture bytes")
    assert reader.validate_selection(refs, filters=Filters()) is False


def test_changed_derived_artifact_is_rejected_even_when_excerpt_stays_the_same(source):
    old_reader = make_reader(source)
    refs = selected(source, old_reader)
    raw = source["bundle"].model_dump()
    artifact = next(item for item in raw["text_artifacts"] if item["asset_id"] == "image")
    artifact["body"] += " A new unrelated ending."
    artifact["body_sha256"] = sha256_text(artifact["body"])
    mount(source, MediaBundle.model_validate(raw))
    assert old_reader.validate_selection(refs, filters=Filters()) is False
    # A newly configured manifest does not make the old extraction binding valid.
    assert make_reader(source).validate_selection(refs, filters=Filters()) is False


@pytest.mark.parametrize("change", ["pause", "title", "bundle", "image"])
def test_change_during_second_source_read_does_not_inherit_first_validation(source, change):
    refs = selected(source)
    calls = 0

    def changing(filters, record_id):
        nonlocal calls
        calls += 1
        row = deepcopy(source["row"])
        if calls == 2:
            if change == "pause":
                row["retrievable"] = False
            elif change == "title":
                row["title"] = "Changed while validating"
            elif change == "bundle":
                source["path"].write_bytes(source["path"].read_bytes() + b" ")
            else:
                (source["root"] / "image.fixture").write_bytes(b"changed image bytes")
        return row

    assert make_reader(source, current_record=changing).validate_selection(refs, filters=Filters()) is False
    assert calls == 2


def test_mutated_nested_locator_and_forged_quality_or_hash_do_not_pass(source):
    reader = make_reader(source)
    refs = selected(source, reader)
    image = MediaAnswerEvidence.from_source_ref(next(ref for ref in refs if ref["media_type"] == "image"))
    image.locator.region[0] = 0.15  # Frozen Pydantic models still contain mutable lists.
    assert reader.validate_selection([image], filters=Filters()) is False
    for field, value in (("quality_label", "Independently verified speech"),
                         ("artifact_sha256", "f" * 64), ("text_artifact_id", "made-up"),
                         ("evidence_id", "made-up")):
        assert reader.validate_selection([{**refs[0], field: value}], filters=Filters()) is False


def test_existing_media_instances_and_nested_locations_are_revalidated(source):
    ref = next(ref for ref in selected(source) if ref["media_type"] == "image")
    typed = MediaAnswerEvidence.from_source_ref(ref)
    with pytest.raises(ValidationError):
        MediaAnswerEvidence.model_validate(typed.model_copy(update={"derived_end": 1}))
    typed.locator.region[0] = 0.99
    with pytest.raises(ValidationError):
        MediaAnswerEvidence.model_validate(typed)


def test_hidden_links_remain_hidden_through_read_and_revalidation(source):
    reader = make_reader(source, show_source_links=False)
    refs = selected(source, reader)
    assert all(ref["source_url"] == "" for ref in refs)
    assert reader.validate_selection(refs, filters=Filters())
    assert "example.test" not in json.dumps(refs)
    assert make_reader(source, show_source_links=True).validate_selection(refs, filters=Filters()) is False


def frame_bundle(source):
    bundle = source["bundle"]
    image = next(asset for asset in bundle.assets if asset.media_type == "image")
    artifact, units = image_evidence(
        image, "Carbon capture equipment is visible.", origin="human_description",
        producer="private/frame/operator",
        location=ImageLocation(screenshot_sha256=image.sha256, region=[0.0, 0.0, 1.0, 1.0],
                               video_asset_id="video", video_time_ms=2500))
    return MediaBundle(assets=bundle.assets,
                       text_artifacts=[item for item in bundle.text_artifacts if item.asset_id != "image"] + [artifact],
                       evidence=[unit for unit in bundle.evidence if unit.media_type != "image"] + units)


def test_frame_keeps_supplied_time_and_rechecks_parent_video_bytes(source):
    mount(source, frame_bundle(source))
    reader = make_reader(source)
    refs = reader.read("carbon", filters=Filters(), media_types=["image"])["source_refs"]
    assert len(refs) == 1 and refs[0]["locator"]["video_time_ms"] == 2500
    assert reader.validate_selection(refs, filters=Filters())
    (source["root"] / "video.fixture").write_bytes(b"parent replaced")
    assert reader.validate_selection(refs, filters=Filters()) is False


@pytest.mark.parametrize("field", ["active", "retrievable"])
def test_paused_parent_video_blocks_its_frame(source, field):
    mount(source, frame_bundle(source))
    reader = make_reader(source)
    refs = reader.read("carbon", filters=Filters(), media_types=["image"])["source_refs"]
    raw = source["bundle"].model_dump()
    next(asset for asset in raw["assets"] if asset["asset_id"] == "video")[field] = False
    mount(source, MediaBundle.model_validate(raw))
    assert reader.validate_selection(refs, filters=Filters()) is False
    assert make_reader(source).read("carbon", filters=Filters(), media_types=["image"])["status"] == "unavailable"


def test_private_exception_and_synthetic_bundle_fail_closed(source):
    refs = selected(source)

    def error(*args):
        raise RuntimeError("C:/private/secret and private/operator")

    assert make_reader(source, current_record=error).validate_selection(refs, filters=Filters()) is False
    raw = source["bundle"].model_dump()
    raw["source_mode"] = "synthetic_demo"
    for asset in raw["assets"]:
        asset["synthetic"] = True
    mount(source, MediaBundle.model_validate(raw))
    assert make_reader(source).validate_selection(refs, filters=Filters()) is False
