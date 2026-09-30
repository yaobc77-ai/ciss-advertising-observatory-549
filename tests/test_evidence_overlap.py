"""Original-coordinate guarantees when retrieved chunks overlap or touch."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from observatory.config import Settings
from observatory.models import Evidence
from observatory.rag import (
    MAX_QUOTE_WORDS,
    GroundedClaim,
    ModelAnswer,
    Rag,
    _evidence_views,
    quote_catalog,
    validate_answer,
)


def source(body, start=0, end=None, *, evidence_id="e1", **metadata):
    end = len(body) if end is None else end
    values = dict(
        evidence_id=evidence_id, record_id="record", version_id="version",
        dataset="native", title="Article", text=body[start:end], start=start, end=end,
    )
    values.update(metadata)
    return Evidence(**values)


def assert_original_quotes(catalog, evidence):
    by_id = {e.evidence_id: e for e in evidence}
    assert catalog
    for passage in catalog.values():
        assert set(passage) == {"evidence_id", "quote"}
        original = by_id[passage["evidence_id"]]
        assert passage["quote"] in original.text
        assert len(passage["quote"].split()) <= MAX_QUOTE_WORDS


def test_overlap_is_segmented_once_with_complete_speaker_sentence():
    first = "The advertisement introduces a pilot. "
    shared = "Dr. Doe says the U.S. pilot remains uncertain. "
    last = "The team will need independent tests."
    body = first + shared + last
    evidence = [
        source(body, end=len(first + shared)),
        source(body, start=len(first), evidence_id="e2"),
    ]
    before = [e.model_dump() for e in evidence]
    catalog = quote_catalog(evidence)
    quotes = [p["quote"] for p in catalog.values()]
    assert quotes == [(first + shared).strip(), last]
    assert sum("Dr. Doe" in q for q in quotes) == 1
    assert_original_quotes(catalog, evidence)
    assert [e.model_dump() for e in evidence] == before


def test_source_choice_is_deterministic_and_uses_existing_ids():
    body = "The speaker discusses a planned pilot. Further testing remains necessary."
    evidence = [
        source(body, evidence_id="z"),
        source(body, evidence_id="a"),
        source(body, start=4, evidence_id="inside"),
    ]
    forward = quote_catalog(evidence)
    assert forward == quote_catalog(list(reversed(evidence)))
    assert forward == {"Q1": {"evidence_id": "a", "quote": body}}
    assert_original_quotes(forward, evidence)


def test_adjacent_sources_never_create_a_new_spanning_citation():
    body = "The company proposed a pilot whose eventual outcome remains unknown."
    cut = body.index("whose")
    evidence = [source(body, end=cut), source(body, start=cut, evidence_id="e2")]
    catalog = quote_catalog(evidence)
    assert [p["quote"] for p in catalog.values()] == [body[:cut].strip(), body[cut:]]
    assert all(p["quote"] != body for p in catalog.values())
    assert_original_quotes(catalog, evidence)


@pytest.mark.parametrize("boundary", ["\n\n", "\r\n \t\r\n", "\f", "\n\f\n"])
def test_hard_boundaries_survive_a_join_inside_the_separator(boundary):
    body = "First paragraph" + boundary + "Second paragraph"
    cut = len("First paragraph") + 1
    evidence = [source(body, end=cut), source(body, start=cut, evidence_id="e2")]
    assert [p["quote"] for p in quote_catalog(evidence).values()] == [
        "First paragraph", "Second paragraph",
    ]


def test_a_missing_interval_is_never_filled():
    body = "First passage. Omitted qualification. Final passage."
    first_end = len("First passage.")
    last_start = body.index("Final")
    evidence = [
        source(body, end=first_end), source(body, start=last_start, evidence_id="e2"),
    ]
    views = _evidence_views(evidence)
    assert [(v.start, v.text) for v in views] == [
        (0, "First passage."), (last_start, "Final passage."),
    ]
    assert all("Omitted" not in p["quote"] for p in quote_catalog(evidence).values())


@pytest.mark.parametrize("changed", [
    {"record_id": "other-record"}, {"version_id": "other-version"},
    {"dataset": "social"}, {"title": "Other title"}, {"publisher": "Other publisher"},
    {"sponsor": "Other sponsor"}, {"url": "https://example.org/other"},
    {"archive_url": "https://example.org/archived"},
])
def test_different_source_identity_or_metadata_is_not_merged(changed):
    body = "A continuous sentence across this exact boundary."
    cut = body.index("across")
    evidence = [
        source(body, end=cut), source(body, start=cut, evidence_id="e2", **changed),
    ]
    assert len(_evidence_views(evidence)) == 2
    assert_original_quotes(quote_catalog(evidence), evidence)


def test_conflicting_overlap_blocks_before_dispatch_and_releases_reservation():
    body = "The same source text is required in both chunks."
    first = source(body)
    second = source(body, start=4, evidence_id="e2")
    second = second.model_copy(update={"text": "X" + second.text[1:]})
    checked, cancelled = [], []
    client = SimpleNamespace(responses=SimpleNamespace(parse=Mock()))
    rag = Rag(SimpleNamespace(validate_evidence=lambda e: checked.append(e) or True),
              Settings(), client=client)
    rag.budget = SimpleNamespace(cancel_unsent=cancelled.append)
    with pytest.raises(ValueError, match="overlap"):
        rag.generate("What does this article say?", [first, second], "test", "reserved")
    assert checked == [first, second]
    assert cancelled == ["reserved"]
    client.responses.parse.assert_not_called()


@pytest.mark.parametrize("changed", [
    {"start": -1}, {"end": 1}, {"start": True}, {"end": 999},
])
def test_invalid_original_coordinates_are_rejected(changed):
    item = source("A complete original sentence.").model_copy(update=changed)
    with pytest.raises(ValueError, match="offsets"):
        quote_catalog([item])


def test_identical_text_at_different_locations_is_not_deduplicated():
    body = "Repeated sentence.\n\nRepeated sentence."
    catalog = quote_catalog([source(body)])
    assert [p["quote"] for p in catalog.values()] == ["Repeated sentence."] * 2


def test_unlocated_inputs_cannot_be_merged_by_text_similarity():
    evidence = [
        SimpleNamespace(evidence_id="one", text="Identical sentence."),
        SimpleNamespace(evidence_id="two", text="Identical sentence."),
    ]
    assert quote_catalog(evidence) == {
        "Q1": {"evidence_id": "one", "quote": "Identical sentence."},
        "Q2": {"evidence_id": "two", "quote": "Identical sentence."},
    }


def test_long_sentence_windows_remain_bounded_and_cover_the_source():
    body = " ".join(f"word{i}" for i in range(140))
    cut = body.index("word80")
    second_start = body.index("word50")
    evidence = [source(body, end=cut), source(body, start=second_start, evidence_id="e2")]
    catalog = quote_catalog(evidence)
    assert_original_quotes(catalog, evidence)
    assert {word for p in catalog.values() for word in p["quote"].split()} == set(body.split())


def test_answer_keeps_every_original_evidence_even_when_catalog_uses_only_one():
    body = "The advertisement describes a proposed pilot."
    evidence = [source(body, evidence_id="one"), source(body, evidence_id="two")]
    passage = next(iter(quote_catalog(evidence).values()))
    result = validate_answer(ModelAnswer(
        status="answered",
        claims=[GroundedClaim(text="The advertisement proposes a pilot.", **passage)],
    ), evidence)
    assert result.evidence == evidence
    assert result.citations[0].evidence_id == "one"


def test_generation_omits_empty_catalog_entries_but_retains_original_evidence():
    body = "The advertisement describes a proposed pilot."
    evidence = [source(body, evidence_id="one"), source(body, evidence_id="two")]
    checked = []
    connection = MagicMock()

    def respond(**request):
        payload = json.loads(request["input"][1]["content"])
        assert [item["id"] for item in payload["evidence"]] == ["one"]
        assert payload["evidence"][0]["quote_catalog"] == {"Q1": body}
        return SimpleNamespace(
            status="completed", model="offline-fixture",
            output_parsed=request["text_format"].model_validate({
                "status": "insufficient_evidence", "claims": [],
            }),
            usage=SimpleNamespace(
                input_tokens=10, output_tokens=5,
                input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
                model_dump=lambda: {"input_tokens": 10, "output_tokens": 5},
            ),
        )

    client = SimpleNamespace(responses=SimpleNamespace(parse=Mock(side_effect=respond)))
    rag = Rag(SimpleNamespace(
        validate_evidence=lambda e: checked.append(e) or True,
        connect=lambda: connection,
    ), Settings(), client=client)
    rag.budget = SimpleNamespace(settle=Mock(), uncertain=Mock())
    result = rag.generate("What does this article say?", evidence, "test", "reserved")
    assert checked == evidence
    assert result.evidence == evidence
    client.responses.parse.assert_called_once()
    rag.budget.settle.assert_called_once()
    rag.budget.uncertain.assert_not_called()
    saved = connection.__enter__.return_value.execute.call_args.args[1][1]
    assert saved.obj == ["one", "two"]
