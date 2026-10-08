"""Offline media citation and dispatch boundaries; no real inference or materials."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from openai.lib._pydantic import to_strict_json_schema

from observatory.config import Settings
from observatory.db import digest
from observatory.media_evidence import ImageLocation, TimeLocation
from observatory.models import Evidence, MediaAnswerEvidence
from observatory.rag import (
    MAX_QUOTE_WORDS,
    GroundedClaim,
    ModelAnswer,
    Rag,
    answer_quote_catalog,
    materialize_selections,
    selection_schema,
    validate_answer,
)


def body_evidence(*, evidence_id="body-evidence", title="Synthetic article"):
    text = "The article says the company plans a pilot, with its outcome still uncertain."
    return Evidence(
        evidence_id=evidence_id, record_id="synthetic-record", version_id="a" * 64,
        dataset="native", title=title, text=text, start=20, end=20 + len(text),
    )


def media_evidence(*, evidence_id="image-evidence", media_type="image", origin=None,
                   text=None, title="Synthetic media source"):
    text = text or (
        "The image's extracted text describes a planned carbon-capture pilot."
        if media_type == "image" else
        "The supplied caption says the pilot remains planned and its result is uncertain."
    )
    asset_hash = digest("synthetic-asset:" + evidence_id)
    locator = (
        ImageLocation(screenshot_sha256=asset_hash, page_number=2,
                      region=[0.1, 0.2, 0.8, 0.9])
        if media_type == "image" else
        TimeLocation(start_ms=10_000, end_ms=18_000)
    )
    return MediaAnswerEvidence(
        evidence_id=evidence_id, asset_id="asset:" + evidence_id,
        record_id="synthetic-record", version_id="a" * 64, dataset="native", title=title,
        source_url="https://example.org/synthetic-ad", media_type=media_type,
        origin=origin or ("ocr" if media_type == "image" else "publisher_caption"),
        locator=locator, evidence_text=text,
        quality_label="Synthetic derived text; no semantic accuracy claim.",
        asset_sha256=asset_hash, text_artifact_id="artifact:" + evidence_id,
        artifact_sha256=digest("prefix:" + text + ":suffix"),
        derived_start=7, derived_end=7 + len(text),
    )


def selected_output(passage_ids, *, status="answered"):
    if status == "insufficient_evidence":
        return {"status": status, "claims": [], "summary": [], "sections": []}
    numbers = list(range(1, len(passage_ids) + 1))
    return {
        "status": status,
        "claims": [{"passage_id": pid,
                    "text": "The supplied advertisement evidence describes a planned pilot."}
                   for pid in passage_ids],
        "summary": [{"text": "These supplied materials describe a planned pilot.",
                     "citation_indices": numbers}],
        "sections": [{"title": "Supplied evidence", "citation_indices": numbers}],
    }


def adapter(output, *, before_response=None):
    connection = MagicMock()
    validator = Mock(return_value=True)

    def respond(**request):
        if before_response:
            before_response(request)
        return SimpleNamespace(
            status="completed", model="offline-fixture",
            output_parsed=request["text_format"].model_validate(output),
            usage=SimpleNamespace(
                input_tokens=20, output_tokens=10,
                input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
                model_dump=lambda: {"input_tokens": 20, "output_tokens": 10},
            ),
        )

    provider = Mock(side_effect=respond)
    rag = Rag(SimpleNamespace(validate_evidence=validator, connect=lambda: connection),
              Settings(), client=SimpleNamespace(responses=SimpleNamespace(parse=provider)))
    rag.budget = SimpleNamespace(reserve=Mock(return_value="reserved"), settle=Mock(),
                                 uncertain=Mock(), cancel_unsent=Mock())
    return rag, provider, validator, connection


def test_joint_catalog_keeps_original_ids_short_derivatives_and_no_body_coordinates():
    body = body_evidence()
    words = "CO₂ emissions — " + " ".join(f"word{i}" for i in range(140))
    image = media_evidence(text=words)
    video = media_evidence(evidence_id="video-evidence", media_type="video")
    snapshots = [item.model_dump(mode="json") for item in [body, image, video]]
    catalog = answer_quote_catalog([body], [image, video])

    assert list(catalog) == [f"Q{i}" for i in range(1, len(catalog) + 1)]
    assert {k: catalog["Q1"][k] for k in ("evidence_id", "quote")} == {"evidence_id": body.evidence_id, "quote": body.text}
    originals = {image.evidence_id: image.evidence_text, video.evidence_id: video.evidence_text}
    for item in list(catalog.values())[1:]:
        assert {key for key in item if not key.startswith("_")} == {"evidence_id", "quote", "context"}
        assert item["quote"] in originals[item["evidence_id"]]
        assert len(item["quote"].split()) <= MAX_QUOTE_WORDS
    image_words = {word for item in catalog.values() if item["evidence_id"] == image.evidence_id
                   for word in item["quote"].split()}
    assert image_words == set(words.split())
    assert [item.model_dump(mode="json") for item in [body, image, video]] == snapshots


def test_selection_schema_and_materializer_only_select_real_media_passage_ids():
    image = media_evidence()
    catalog = answer_quote_catalog([], [image])
    schema = selection_schema(catalog)
    strict = to_strict_json_schema(schema)
    claim_schema = strict["$defs"]["SelectedClaim"]
    assert set(claim_schema["properties"]) == {"passage_id", "support_passage_ids", "text"}
    assert claim_schema["additionalProperties"] is False
    with pytest.raises(ValueError):
        schema.model_validate(selected_output(["Q999"]))
    with pytest.raises(ValueError, match="Unverifiable citation"):
        materialize_selections(SimpleNamespace(
            status="answered", claims=[SimpleNamespace(passage_id="Q999", text="Fake")],
        ), catalog)
    selected = schema.model_validate(selected_output(["Q1"]))
    materialized = materialize_selections(selected, catalog)
    assert materialized.claims[0].evidence_id == image.evidence_id
    assert materialized.claims[0].quote == image.evidence_text


@pytest.mark.parametrize("media_type,origin", [
    ("image", "ocr"), ("image", "vision"), ("image", "human_description"),
    ("video", "publisher_caption"), ("video", "automatic_caption"),
    ("video", "transcript"), ("video", "human_description"),
])
def test_media_only_generation_preserves_derived_origin_and_coarse_locator(media_type, origin):
    media = media_evidence(media_type=media_type, origin=origin)
    original = media.model_dump(mode="json")
    verify = Mock(return_value=True)
    rag, provider, body_validator, _ = adapter(selected_output(["Q1"]))
    result = rag.generate("What do these supplied materials describe?", [], "offline-test",
                          "reserved", media_evidence=[media], validate_media=verify)

    assert result.status == "answered" and result.evidence == []
    assert result.media_evidence == [media]
    assert [(c.evidence_id, c.quote, c.evidence_type, c.origin) for c in result.citations] == [
        (media.evidence_id, media.evidence_text, media_type, origin),
    ]
    assert result.summary[0].citation_indices == [1]
    assert result.cited_claims[0].citation_indices == [1]
    assert result.media_evidence[0].model_dump(mode="json") == original
    assert result.media_evidence[0].quote_from_original_body is False
    assert result.media_evidence[0].image_or_speech_semantics_verified is False
    assert verify.call_count == 2 and all(c.args == ([media],) for c in verify.call_args_list)
    body_validator.assert_not_called()
    provider.assert_called_once()
    rag.budget.settle.assert_called_once()
    rag.budget.uncertain.assert_not_called()


def test_mixed_generation_uses_one_based_references_and_server_assigned_media_types():
    body, image = body_evidence(), media_evidence()
    video = media_evidence(evidence_id="video-evidence", media_type="video", origin="transcript")
    rag, provider, body_validator, connection = adapter(selected_output(["Q1", "Q2", "Q3"]))
    result = rag.generate("What do these supplied materials describe?", [body], "offline-test",
                          "reserved", media_evidence=[image, video], validate_media=lambda _: True)

    assert [c.evidence_id for c in result.citations] == [body.evidence_id, image.evidence_id, video.evidence_id]
    assert [c.evidence_type for c in result.citations] == ["article_text", "image", "video"]
    assert [c.origin for c in result.citations] == ["original_text", "ocr", "transcript"]
    assert result.summary[0].citation_indices == [1, 2, 3]
    assert result.sections[0].citation_indices == [1, 2, 3]
    assert [c.citation_indices for c in result.cited_claims] == [[1], [2], [3]]
    body_validator.assert_called_once_with(body)
    context = json.loads(provider.call_args.kwargs["input"][1]["content"])
    assert context["evidence"][0]["quote_catalog"] == {"Q1": body.text}
    assert context["media_evidence"][0]["quote_catalog"] == {"Q2": image.evidence_text}
    assert context["media_evidence"][1]["locator"] == video.locator.model_dump(mode="json")
    saved_ids = connection.__enter__.return_value.execute.call_args.args[1][1].obj
    assert saved_ids == [body.evidence_id, image.evidence_id, video.evidence_id]


@pytest.mark.parametrize("media_type,origin", [("image", "ocr"), ("video", "automatic_caption")])
def test_matching_body_words_never_promote_a_derived_citation_to_original_text(media_type, origin):
    body = body_evidence()
    media = media_evidence(media_type=media_type, origin=origin, text=body.text)
    rag, _, _, _ = adapter(selected_output(["Q2"]))
    result = rag.generate("What does this material describe?", [body], "offline-test", "reserved",
                          media_evidence=[media], validate_media=lambda _: True)
    assert result.citations[0].quote == body.text
    assert result.citations[0].evidence_id == media.evidence_id
    assert result.citations[0].evidence_type == media_type
    assert result.citations[0].origin == origin
    assert result.media_evidence[0].quote_from_original_body is False
    assert result.media_evidence[0].locator.model_dump() == media.locator.model_dump()


@pytest.mark.parametrize("bad_id,bad_quote", [
    ("missing", "planned"),
    ("image-evidence", "A fabricated statement absent from the extraction."),
    ("body-evidence", "The image's extracted text describes a planned carbon-capture pilot."),
    ("image-evidence", "The article says the company plans a pilot, with its outcome still uncertain."),
])
def test_claims_cannot_borrow_another_source_id_or_invent_derived_quotes(bad_id, bad_quote):
    parsed = ModelAnswer(status="answered", claims=[GroundedClaim(
        text="The supplied material describes a pilot.", evidence_id=bad_id, quote=bad_quote,
    )])
    with pytest.raises(ValueError, match="Unverifiable citation"):
        validate_answer(parsed, [body_evidence()], media_evidence=[media_evidence()])


def test_untrusted_materialized_quote_cannot_bypass_the_short_quote_limit():
    long_text = " ".join(f"word{i}" for i in range(MAX_QUOTE_WORDS + 1))
    media = media_evidence(text=long_text)
    parsed = ModelAnswer(status="answered", claims=[GroundedClaim(
        evidence_id=media.evidence_id, quote=long_text, text="The supplied material describes a pilot.",
    )])
    with pytest.raises(ValueError, match="Citation exceeds short-quote limit"):
        validate_answer(parsed, [], media_evidence=[media])


@pytest.mark.parametrize("kind", ["body_and_media", "two_media"])
def test_duplicate_ids_stop_before_dispatch_including_unsent_reservation(kind):
    image = media_evidence(evidence_id="same-id")
    body = [body_evidence(evidence_id="same-id")] if kind == "body_and_media" else []
    media = [image] if body else [image, media_evidence(evidence_id="same-id", media_type="video")]
    rag, provider, _, _ = adapter(selected_output(["Q1"]))
    with pytest.raises(ValueError, match="Duplicate answer evidence identity"):
        rag.generate("What do these materials describe?", body, "offline-test", "reserved",
                     media_evidence=media, validate_media=lambda _: True)
    provider.assert_not_called()
    rag.budget.reserve.assert_not_called()
    rag.budget.cancel_unsent.assert_called_once_with("reserved")


@pytest.mark.parametrize("verify", [None, lambda _: False, lambda _: 1])
def test_media_generation_requires_explicit_successful_pre_validation(verify):
    rag, provider, body_validator, connection = adapter(selected_output(["Q1"]))
    with pytest.raises(ValueError, match="Media evidence failed source validation"):
        rag.generate("What does this material describe?", [], "offline-test", "reserved",
                     media_evidence=[media_evidence()], validate_media=verify)
    provider.assert_not_called()
    body_validator.assert_not_called()
    connection.__enter__.assert_not_called()
    rag.budget.cancel_unsent.assert_called_once_with("reserved")
    rag.budget.reserve.assert_not_called()
    rag.budget.settle.assert_not_called()


def test_raised_pre_verification_error_also_cancels_without_model_dispatch():
    rag, provider, _, _ = adapter(selected_output(["Q1"]))
    verify = Mock(side_effect=OSError("Synthetic source unavailable"))
    with pytest.raises(ValueError, match="Media evidence failed source validation"):
        rag.generate("What does this material describe?", [], "offline-test", "reserved",
                     media_evidence=[media_evidence()], validate_media=verify)
    provider.assert_not_called()
    rag.budget.cancel_unsent.assert_called_once_with("reserved")


def test_source_change_after_provider_is_refused_but_usage_and_ids_are_preserved():
    image = media_evidence()
    current = {"valid": True}
    verify = Mock(side_effect=lambda _: current["valid"])
    rag, provider, _, connection = adapter(
        selected_output(["Q1"]), before_response=lambda _: current.update(valid=False),
    )
    with pytest.raises(ValueError, match="Media evidence failed source validation"):
        rag.generate("What does this material describe?", [], "offline-test", "reserved",
                     media_evidence=[image], validate_media=verify)
    assert verify.call_count == 2
    provider.assert_called_once()
    rag.budget.settle.assert_called_once()
    rag.budget.cancel_unsent.assert_not_called()
    saved_ids = connection.__enter__.return_value.execute.call_args.args[1][1].obj
    assert saved_ids == [image.evidence_id]


def test_usage_audit_binds_media_versions_hashes_and_derived_ranges_without_private_paths():
    image = media_evidence()
    public = MediaAnswerEvidence.from_source_ref({
        **image.model_dump(mode="json"), "local_path": "D:/private/customer-image.png",
        "producer": {"reviewer": "private-reviewer"}, "secret": "private-material-key",
    })
    rag, provider, _, _ = adapter(selected_output(["Q1"]))
    rag.generate("What does this material describe?", [], "offline-test", "reserved",
                 media_evidence=[public], validate_media=lambda _: True)
    audit = rag.budget.settle.call_args.args[2]["observatory_request"]
    binding = audit["media_source_bindings"][0]
    for field in ("evidence_id", "record_id", "version_id", "asset_sha256", "text_artifact_id",
                  "artifact_sha256", "derived_start", "derived_end", "origin", "locator"):
        assert binding[field] == image.model_dump(mode="json")[field]
    audit_and_request = json.dumps([audit, provider.call_args.kwargs["input"]])
    assert all(value not in audit_and_request for value in (
        "local_path", "D:/private/", "private-reviewer", "private-material-key",
    ))
    assert audit["retrieval_scope"]["complete_matching_list"] is False


def test_media_context_is_included_in_utf8_prompt_cap_before_provider_dispatch():
    # A valid body-only context fits. Adding bounded derived media crosses the
    # same input cap; it cannot be excluded from reservation sizing.
    body = body_evidence(title="B" * 78_000)  # sentence context now adds prompt overhead
    baseline, baseline_provider, _, _ = adapter(selected_output([], status="insufficient_evidence"))
    baseline.generate("What do these materials describe?", [body], "offline-test", "reserved")
    baseline_provider.assert_called_once()
    long_text = "CO₂ " + " ".join(f"word{i}" for i in range(690))
    image = media_evidence(text=long_text)
    rag, provider, _, _ = adapter(selected_output(["Q1"]))
    with pytest.raises(ValueError, match="Evidence prompt exceeds app budget"):
        rag.generate("What do these materials describe?", [body], "offline-test", "reserved",
                     media_evidence=[image], validate_media=lambda _: True)
    provider.assert_not_called()
    rag.budget.reserve.assert_not_called()
    rag.budget.cancel_unsent.assert_called_once_with("reserved")


def test_original_body_validation_remains_required_for_mixed_media_generation():
    body = body_evidence()
    rag, provider, body_validator, _ = adapter(selected_output(["Q1"]))
    body_validator.return_value = False
    with pytest.raises(ValueError, match="Evidence failed original-version validation"):
        rag.generate("What do these materials describe?", [body], "offline-test", "reserved",
                     media_evidence=[media_evidence()], validate_media=lambda _: True)
    body_validator.assert_called_once_with(body)
    provider.assert_not_called()
    rag.budget.cancel_unsent.assert_called_once_with("reserved")


def test_original_body_validation_exception_cancels_reservation_before_dispatch():
    body = body_evidence()
    rag, provider, body_validator, connection = adapter(selected_output(["Q1"]))
    body_validator.side_effect = OSError("Synthetic source validation unavailable")
    verify_media = Mock(return_value=True)

    with pytest.raises(ValueError, match="Evidence failed original-version validation"):
        rag.generate("What do these materials describe?", [body], "offline-test", "reserved",
                     media_evidence=[media_evidence()], validate_media=verify_media)

    body_validator.assert_called_once_with(body)
    verify_media.assert_not_called()
    provider.assert_not_called()
    connection.__enter__.assert_not_called()
    rag.budget.cancel_unsent.assert_called_once_with("reserved")
    rag.budget.reserve.assert_not_called()
    rag.budget.settle.assert_not_called()
    rag.budget.uncertain.assert_not_called()


def test_insufficient_answer_keeps_media_as_unclassified_evidence_without_published_claims():
    image = media_evidence()
    contradictory = ModelAnswer(status="insufficient_evidence", claims=[GroundedClaim(
        evidence_id=image.evidence_id, quote=image.evidence_text,
        text="An unneeded statement that must not be published.",
    )])
    with pytest.raises(ValueError, match="Contradictory answer status"):
        validate_answer(contradictory, [], 0.004, media_evidence=[image])
    parsed = ModelAnswer(status="insufficient_evidence", claims=[])
    answer = validate_answer(parsed, [], 0.004, media_evidence=[image])
    assert answer.status == "insufficient_evidence" and answer.media_evidence == [image]
    assert answer.citations == answer.summary == answer.sections == answer.cited_claims == []
    assert "unneeded statement" not in answer.answer
    assert answer.cost_usd == 0.004


@pytest.mark.parametrize("status", ["answered", "insufficient_evidence"])
def test_media_instance_cannot_bypass_derived_interval_validation_by_model_copy(status):
    valid = media_evidence()
    changed = valid.model_copy(update={"evidence_text": valid.evidence_text + " Changed."})
    parsed = ModelAnswer(status=status, claims=[GroundedClaim(
        evidence_id=valid.evidence_id, quote=valid.evidence_text,
        text="The supplied material describes a planned pilot.",
    )])
    with pytest.raises(ValueError):
        validate_answer(parsed, [], media_evidence=[changed])
