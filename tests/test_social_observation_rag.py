"""Social observation quotes retain exact source identity without merging variants."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from pydantic import ValidationError

from observatory.config import Settings
from observatory.models import Citation, Evidence
from observatory.rag import (
    GroundedClaim,
    ModelAnswer,
    Rag,
    _evidence_views,
    _observation_context,
    materialize_selections,
    quote_catalog,
    selection_schema,
    validate_answer,
)


def quotes(catalog):
    """Source identity and text only; sentence context is checked in its own tests."""
    return {pid: {"evidence_id": p["evidence_id"], "quote": p["quote"]} for pid, p in catalog.items()}


def observation(body, *, source_id="junkipedia:1", version="a" * 64, evidence_id="e1", start=0, end=None, **updates):
    end = len(body) if end is None else end
    text = body[start:end] if type(start) is int and type(end) is int else body
    values = {
        "evidence_id": evidence_id, "record_id": "social-post:example", "version_id": "b" * 64,
        "dataset": "social", "title": "Supplied post observation", "text": text,
        "start": start, "end": end, "source_observation_id": source_id,
        "source_version_id": version, "source_body_hash": hashlib.sha256(body.encode()).hexdigest(),
        "source_observation_count": 2, "source_conflicts": ["body"],
        "source_quality_codes": ["body_footer_only"],
    }
    values.update(updates)
    return Evidence(**values)


def native(body="The article describes a proposed pilot.", **updates):
    values = {
        "evidence_id": "native", "record_id": "record", "version_id": "version", "dataset": "native",
        "title": "Article", "text": body, "start": 0, "end": len(body),
    }
    values.update(updates)
    return Evidence(**values)


def test_opposing_observations_with_same_post_and_offsets_never_merge():
    yes, no = "The pilot is planned.", "The pilot is rejected."
    items = [observation(yes), observation(no, source_id="junkipedia:2", version="c" * 64, evidence_id="e2")]
    assert [(view.start, view.text) for view in _evidence_views(items)] == [(0, yes), (0, no)]
    assert quotes(quote_catalog(items)) == {
        "Q1": {"evidence_id": "e1", "quote": yes},
        "Q2": {"evidence_id": "e2", "quote": no},
    }
    assert len({item.record_id for item in items}) == 1


def test_identical_variant_text_at_same_offsets_retains_both_observation_ids():
    body = "The supplied observation describes a pilot."
    items = [observation(body), observation(body, source_id="junkipedia:2", evidence_id="e2")]
    assert len(quote_catalog(items)) == 2
    assert {passage["evidence_id"] for passage in quote_catalog(items).values()} == {"e1", "e2"}


def test_same_observation_chunks_join_only_with_original_source_coverage():
    body = "The speaker proposes a pilot. The result remains uncertain."
    start = body.index("The result")
    items = [observation(body, end=start), observation(body, start=start, evidence_id="e2")]
    assert len(_evidence_views(items)) == 1
    by_id = {item.evidence_id: item for item in items}
    for passage in quote_catalog(items).values():
        assert passage["quote"] in by_id[passage["evidence_id"]].text


@pytest.mark.parametrize("change", [
    {"source_observation_id": None}, {"source_version_id": None}, {"source_body_hash": None},
    {"source_observation_id": "C:/private/path"}, {"source_observation_id": 123},
    {"source_version_id": "A" * 64}, {"source_body_hash": "not-a-hash"},
    {"source_observation_count": True}, {"source_observation_count": 0},
    {"source_conflicts": ["private/path"]}, {"source_conflicts": ["body", "body"]},
    {"source_quality_codes": ["private/path"]}, {"source_quality_codes": [1]},
    {"dataset": "native"}, {"start": True}, {"start": "0"},
    {"start": -1}, {"end": 999}, {"end": 0}, {"text": " "},
])
def test_observation_metadata_and_offsets_are_strict(change):
    with pytest.raises(ValidationError):
        observation("An exact original source.", **change)


@pytest.mark.parametrize("change", [
    {"source_version_id": "a" * 64}, {"source_observation_count": 2},
    {"source_conflicts": ["body"]}, {"source_quality_codes": ["body_short"]},
])
def test_observation_context_cannot_be_added_without_identity(change):
    with pytest.raises(ValidationError):
        native(**change)


@pytest.mark.parametrize("change", [
    {"source_version_id": None}, {"source_body_hash": "invalid"},
    {"source_observation_count": True}, {"dataset": "native"},
])
def test_post_creation_forgery_cannot_enter_quote_catalog(change):
    forged = observation("A supplied original observation.").model_copy(update=change)
    with pytest.raises(ValidationError):
        quote_catalog([forged])


@pytest.mark.parametrize("change", [
    {"start": False}, {"start": True}, {"end": False}, {"start": -1}, {"end": -1},
    {"start": "0"}, {"start": 0.0}, {"end": "31"}, {"end": 31.0},
])
def test_modified_source_offsets_are_rejected_before_serialization(monkeypatch, change):
    forged = observation("A supplied original observation.").model_copy(update=change)
    serializer = Mock(side_effect=AssertionError("Raw source offsets must be checked before serialization"))
    monkeypatch.setattr(Evidence, "model_dump", serializer)
    with pytest.raises(ValueError, match="exact Unicode character offsets"):
        quote_catalog([forged])
    serializer.assert_not_called()


def test_unicode_observation_keeps_full_exact_offsets_with_coverage_fallback():
    body = "Prefix not retrieved. ♨️Where are new district heating networks being explored?"
    start = body.index("♨")
    item = observation(body, start=start)
    quotes = [passage["quote"] for passage in quote_catalog([item]).values()]
    assert "".join(char for char in "".join(quotes) if not char.isspace()) == "".join(
        char for char in body[start:] if not char.isspace()
    )
    assert all(quote in body[item.start:item.end] for quote in quotes)
    assert item.text == body[item.start:item.end]


def test_native_and_legacy_social_segmentation_stays_strict(monkeypatch):
    monkeypatch.setattr("observatory.segmentation.pysbd.Segmenter", lambda **kwargs: SimpleNamespace(segment=lambda text: []))
    body = "Uncovered original characters."
    with pytest.raises(ValueError, match="omitted"):
        quote_catalog([native(body)])
    with pytest.raises(ValueError, match="omitted"):
        quote_catalog([native(body, dataset="social")])
    assert quotes(quote_catalog([observation(body)])) == {"Q1": {"evidence_id": "e1", "quote": body}}


@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_native_and_observation_views_keep_their_own_segmentation_policy(reverse):
    items = [native(), observation("The supplied source describes a pilot.")]
    if reverse:
        items.reverse()
    views = _evidence_views(items)
    assert {view.sources[0].evidence.dataset: view.coverage_fallback for view in views} == {
        "native": False, "social": True,
    }


def test_native_catalog_and_citation_context_keep_default_behavior():
    item = native()
    assert quotes(quote_catalog([item])) == {"Q1": {"evidence_id": "native", "quote": item.text}}
    assert _observation_context(item) == {}
    answer = validate_answer(ModelAnswer(status="answered", claims=[GroundedClaim(
        text="The article proposes a pilot.", evidence_id=item.evidence_id, quote=item.text,
    )]), [item])
    assert answer.citations[0].source_observation_id is None
    assert answer.citations[0].source_conflicts == []


def test_program_selected_quote_propagates_trusted_observation_metadata():
    item = observation("The supplied observation describes planned work.")
    catalog = quote_catalog([item])
    parsed = selection_schema(catalog).model_validate({
        "status": "answered", "claims": [{"passage_id": "Q1", "text": "This observation describes planned work."}],
        "summary": [{"text": "Planned work is described.", "citation_indices": [1]}],
        "sections": [{"title": "Planned work", "citation_indices": [1]}],
    })
    answer = validate_answer(materialize_selections(parsed, catalog), [item])
    citation = answer.citations[0]
    assert citation.quote == item.text
    for field in ("source_observation_id", "source_version_id", "source_body_hash", "source_observation_count", "source_conflicts", "source_quality_codes"):
        assert getattr(citation, field) == getattr(item, field)
    restored = Citation.model_validate_json(citation.model_dump_json())
    assert restored == citation
    item.source_conflicts.append("sponsor")
    assert citation.source_conflicts == ["body"]


@pytest.mark.parametrize("change", [{"evidence_type": "image"}, {"origin": "vision"}, {"source_version_id": None}])
def test_observation_citation_cannot_claim_media_or_partial_identity(change):
    item = observation("Original text.")
    values = {key: value for key, value in item.model_dump().items() if key.startswith("source_")}
    values.update(change)
    with pytest.raises(ValidationError):
        Citation(evidence_id=item.evidence_id, quote=item.text, **values)


def test_offline_generation_supplies_source_labels_quality_and_unresolved_conflict():
    item = observation("The supplied source describes a planned pilot.")
    connection = MagicMock()

    def respond(**request):
        payload = json.loads(request["input"][1]["content"])
        supplied = payload["evidence"][0]
        assert supplied["source_observation_id"] == item.source_observation_id
        assert supplied["source_version_id"] == item.source_version_id
        assert supplied["source_body_hash"] == item.source_body_hash
        assert supplied["source_observation_count"] == 2
        assert supplied["source_conflicts"] == ["body"]
        assert supplied["source_quality_codes"] == ["body_footer_only"]
        assert supplied["source_status"] == "preserved_observation_not_adjudicated_post_text"
        assert supplied["paid_ad_status"] == "unknown"
        assert supplied["quote_catalog"] == {"Q1": item.text}
        assert "Do not resolve differing observations" in request["input"][0]["content"]
        return SimpleNamespace(
            status="completed", model="offline-fixture",
            output_parsed=request["text_format"].model_validate({
                "status": "insufficient_evidence", "claims": [], "summary": [], "sections": [],
            }),
            usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                  input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
                                  model_dump=lambda: {"input_tokens": 10, "output_tokens": 5}),
        )

    client = SimpleNamespace(responses=SimpleNamespace(parse=Mock(side_effect=respond)))
    rag = Rag(SimpleNamespace(validate_evidence=lambda value: True, connect=lambda: connection), Settings(), client=client)
    rag.budget = SimpleNamespace(settle=Mock(), uncertain=Mock())
    answer = rag.generate("What does the supplied source say?", [item], "test", "reserved")
    assert answer.evidence == [item]
    client.responses.parse.assert_called_once()
    rag.budget.uncertain.assert_not_called()
