"""Source binding permits layout changes, never lexical repair or guessed offsets."""

from dataclasses import replace
from itertools import product

import pytest

from observatory.claims_spans import (
    ClaimSpan,
    ClaimSpanLocator,
    locate_claim_spans,
    normalize_claim_text,
    validate_claim_span,
)


def test_exact_source_slice_uses_half_open_unicode_character_offsets():
    source = "🛢️中文 prefix: energy transition. suffix"
    matches = locate_claim_spans(source, "energy transition.")
    start = source.index("energy")
    assert matches == [ClaimSpan(start, start + len("energy transition."), "energy transition.", "exact")]
    assert source[matches[0].start : matches[0].end] == matches[0].quote
    assert matches[0].start != len(source[:start].encode("utf-8"))


def test_whitespace_collapse_restores_all_source_layout_characters():
    source = "  Carbon\t \r\ncapture\u00a0\u2003and\fstorage.  "
    matches = locate_claim_spans(source, "Carbon capture and storage.")
    assert matches == [
        ClaimSpan(2, len(source) - 2, source[2:-2], "whitespace_normalized")
    ]
    assert normalize_claim_text(matches[0].quote) == "Carbon capture and storage."


def test_exact_and_normalized_duplicates_remain_separate_candidates():
    source = "Energy transition.\nEnergy\ttransition.\nEnergy transition."
    matches = locate_claim_spans(source, "Energy transition.")
    assert [span.quote for span in matches] == [
        "Energy transition.", "Energy\ttransition.", "Energy transition."
    ]
    assert [span.match_method for span in matches] == ["exact", "whitespace_normalized", "exact"]
    assert len({(span.start, span.end) for span in matches}) == 3


def test_upstream_raw_whitespace_can_itself_be_an_exact_match():
    source = "a\tb; a b; a\nb"
    matches = locate_claim_spans(source, " \ra\tb\n ")
    assert [span.quote for span in matches] == ["a\tb", "a b", "a\nb"]
    assert [span.match_method for span in matches] == ["exact", "whitespace_normalized", "whitespace_normalized"]


def test_overlapping_occurrences_are_not_silently_removed():
    assert locate_claim_spans("banana", "ana") == [
        ClaimSpan(1, 4, "ana", "exact"), ClaimSpan(3, 6, "ana", "exact")
    ]


@pytest.mark.parametrize(
    ("source", "upstream"),
    [
        ("CARBON capture", "carbon capture"),
        ("lower-carbon", "lower carbon"),
        ("A &amp; B", "A & B"),
        ("café", "cafe\u0301"),
        ("ＡＰＩ", "API"),
        ("A\u200bB", "AB"),
        ("capture—storage", "capture-storage"),
    ],
)
def test_non_whitespace_differences_are_not_repaired(source, upstream):
    assert locate_claim_spans(source, upstream) == []


@pytest.mark.parametrize("source", ["", " \t\r\n\u00a0"])
def test_empty_source_has_no_candidates(source):
    assert locate_claim_spans(source, "Carbon") == []


@pytest.mark.parametrize("upstream", ["", " \t\r\n\u00a0"])
def test_blank_upstream_is_rejected_instead_of_matching_every_position(upstream):
    with pytest.raises(ValueError, match="non-whitespace"):
        locate_claim_spans("Carbon capture", upstream)


@pytest.mark.parametrize("value", [None, 3, b"Carbon"])
def test_non_string_inputs_are_rejected(value):
    with pytest.raises(TypeError):
        ClaimSpanLocator(value)
    with pytest.raises(TypeError):
        locate_claim_spans("Carbon", value)
    with pytest.raises(TypeError):
        normalize_claim_text(value)


def test_cached_article_locator_handles_independent_queries():
    locator = ClaimSpanLocator("🛢 capture\ncarbon; capture carbon")
    assert len(locator.locate("capture carbon")) == 2
    assert locator.locate("not present") == []
    assert locator.locate("carbon")[0].quote == "carbon"
    assert len(locator.locate("capture carbon")) == 2


def test_layout_variants_round_trip_to_the_actual_original_slice():
    # Exercise each whitespace run independently, including astral Unicode and
    # decomposed characters. The expected boundaries come from source offsets,
    # not from the locator's own normalized offset view.
    words = ["🛢️", "cafe\u0301", "中文"]
    layouts = [" ", "\t", "\r\n", "\u00a0\u2003", " \n\t "]
    for first_gap, second_gap in product(layouts, repeat=2):
        paragraph = words[0] + first_gap + words[1] + second_gap + words[2]
        source = "\r\nBEFORE: " + paragraph + " :AFTER\t"
        matches = locate_claim_spans(source, " ".join(words))
        assert len(matches) == 1
        assert matches[0].start == len("\r\nBEFORE: ")
        assert matches[0].end == matches[0].start + len(paragraph)
        assert matches[0].quote == paragraph
        validate_claim_span(source, " ".join(words), matches[0])


@pytest.mark.parametrize(
    "changes",
    [
        {"start": -1},
        {"start": True},
        {"end": False},
        {"end": 50},
        {"end": 2},
        {"quote": "invented"},
        {"match_method": "fuzzy"},
        {"match_method": "whitespace_normalized"},
    ],
)
def test_manual_or_corrupted_exact_spans_fail_original_validation(changes):
    valid = ClaimSpan(2, 5, "ABC", "exact")
    with pytest.raises(ValueError):
        validate_claim_span("x ABC y", "ABC", replace(valid, **changes))


def test_wrong_declared_exact_match_is_rejected():
    with pytest.raises(ValueError, match="Exact claim span"):
        validate_claim_span("a\tb", "a b", ClaimSpan(0, 3, "a\tb", "exact"))


def test_normalized_span_must_preserve_the_upstream_non_whitespace_text():
    with pytest.raises(ValueError, match="non-whitespace difference"):
        validate_claim_span("a\tb", "a c", ClaimSpan(0, 3, "a\tb", "whitespace_normalized"))


def test_manual_span_cannot_add_surrounding_source_whitespace():
    with pytest.raises(ValueError, match="surrounding whitespace"):
        validate_claim_span(" a b ", "a b", ClaimSpan(0, 5, " a b ", "whitespace_normalized"))


def test_validator_rejects_untyped_span():
    with pytest.raises(TypeError, match="ClaimSpan"):
        validate_claim_span("ABC", "ABC", (0, 3, "ABC", "exact"))
