"""Independent source-projection fixtures; upstream notebook cells are never run."""

import hashlib
import json
from dataclasses import replace

import pytest

from observatory.claims_projection import (
    NORMALIZATION_CONTRACT_SHA256,
    PROJECTION_CONTRACT_JSON,
    PROJECTION_METHOD,
    UNICODE_DATABASE_VERSION,
    UPSTREAM_NOTEBOOK_SHA256,
    ProjectedClaimSpan,
    UpstreamAsciiProjectionLocator,
    locate_projected_claim_spans,
    project_upstream_ascii,
    projection_contract,
    validate_projected_claim_span,
)
from observatory.claims_spans import locate_claim_spans


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("“café” – ‘naïve’ — …", '"cafe" - \'naive\' -- ...'),
        ("‐ ‑ ‒ ― ′ ″ • · →", '- - - -- \' " - - ->'),
        ("\u00a0\u200b\u200e\u200f\ufeff\u00ad", ""),
        ("25°C × © ® ™ ½ ¼ ¾", "25 degreesC x (c) (R) (TM) 1/2 1/4 3/4"),
        (" cafe\u0301\t\r\n 中文 🛢️ ﬃ  ", "cafe ffi"),
        ("ＡＰＩ ﬁancée K", "API fiancee K"),
        ("Case &amp; <p>Keep</p>", "Case &amp; <p>Keep</p>"),
        ("a\r\nb\rc\n\td", "a b c d"),
    ],
)
def test_independent_notebook_contract_fixtures(source, expected):
    assert project_upstream_ascii(source) == expected


def test_contract_pins_source_and_recipe_without_external_imports():
    assert UPSTREAM_NOTEBOOK_SHA256 == "8c4b6bf88a38dff90bb2446e9293577056b3d9b7a78924d23ca377ebbcba768e"
    contract = json.loads(PROJECTION_CONTRACT_JSON)
    assert contract["source_notebook_sha256"] == UPSTREAM_NOTEBOOK_SHA256
    assert contract["unicode_database_version"] == UNICODE_DATABASE_VERSION
    assert len(contract["replacements"]) == 30
    assert contract["publication_policy"] == "review_only"
    assert NORMALIZATION_CONTRACT_SHA256 == hashlib.sha256(PROJECTION_CONTRACT_JSON.encode("ascii")).hexdigest()
    first = projection_contract()
    first["replacements"].clear()
    assert len(projection_contract()["replacements"]) == 30


def test_ascii_exact_locations_agree_with_strict_locator():
    source = "Lead Energy transition.\nEnergy\ttransition. Tail"
    strict = locate_claim_spans(source, "Energy transition.")
    projected = locate_projected_claim_spans(source, "Energy transition.")
    assert [(span.start, span.end, span.quote) for span in projected] == [
        (span.start, span.end, span.quote) for span in strict
    ]
    assert all(span.match_method == PROJECTION_METHOD and not span.lossy for span in projected)


def test_punctuation_and_diacritics_retain_literal_original_unicode_quote():
    source = "🛢️ Prefix: “cafe\u0301” — ½. Suffix"
    needle = '"cafe" -- 1/2.'
    matches = locate_projected_claim_spans(source, needle)
    assert len(matches) == 1
    span = matches[0]
    assert span.quote == "“cafe\u0301” — ½."
    assert span.start == source.index("“")
    assert span.end == source.index(". Suffix") + 1
    assert span.lossy
    assert source[span.start : span.end] == span.quote
    validate_projected_claim_span(source, needle, span)


@pytest.mark.parametrize(
    ("source", "partial"),
    [("™", "TM"), ("—", "-"), ("…", ".."), ("½", "1"), ("ﬃ", "fi"), ("©", "c")],
)
def test_partial_expansions_are_rejected(source, partial):
    assert locate_projected_claim_spans(source, partial) == []


@pytest.mark.parametrize(
    ("source", "full"), [("™", "(TM)"), ("—", "--"), ("ﬃ", "ffi"), ("°", "degrees")]
)
def test_complete_expansions_have_original_character_coordinates(source, full):
    assert locate_projected_claim_spans(source, full) == [ProjectedClaimSpan(0, len(source), source, True)]


def test_deleted_interior_is_not_hidden_from_original_evidence():
    assert locate_projected_claim_spans("a中🛢b", "ab") == [ProjectedClaimSpan(0, 4, "a中🛢b", True)]


def test_deleted_boundaries_produce_all_distinct_review_candidates():
    source = "中🛢AB文🙂"
    matches = locate_projected_claim_spans(source, "AB")
    assert {(span.start, span.end) for span in matches} == {
        (start, end) for start in (0, 1, 2) for end in (4, 5, 6)
    }
    assert len(matches) == 9
    assert [span for span in matches if not span.lossy] == [ProjectedClaimSpan(2, 4, "AB", False)]
    for span in matches:
        assert source[span.start : span.end] == span.quote
        validate_projected_claim_span(source, "AB", span)


def test_composition_cluster_cannot_silently_drop_combining_mark_at_boundary():
    assert locate_projected_claim_spans("cafe\u0301", "cafe") == [ProjectedClaimSpan(0, 5, "cafe\u0301", True)]
    assert locate_projected_claim_spans("A\u030a", "A") == [ProjectedClaimSpan(0, 2, "A\u030a", True)]


def test_unicode_composition_and_reordering_are_equivalent_to_full_projection():
    source = "a\u0315\u0300 e\u0301\u0323 \u1100\u1161\u11a8 End"
    needle = project_upstream_ascii(source)
    matches = locate_projected_claim_spans(source, needle)
    assert len(matches) == 1
    assert matches[0].quote == source
    assert matches[0].lossy


def test_repeated_and_overlapping_candidates_remain_visible():
    locator = UpstreamAsciiProjectionLocator("café cafe cafe\u0301 banana")
    assert [span.quote for span in locator.locate("cafe")] == ["café", "cafe", "cafe\u0301"]
    assert [span.lossy for span in locator.locate("cafe")] == [True, False, True]
    assert [span.quote for span in locator.locate("ana")] == ["ana", "ana"]
    assert locator.locate("absent") == []


def test_original_blank_lines_are_paragraph_barriers():
    assert locate_projected_claim_spans("First\r\n \r\nSecond", "First Second") == []
    assert locate_projected_claim_spans("First\rSecond", "First Second")[0].quote == "First\rSecond"
    assert locate_projected_claim_spans("First\n\ufeff\nSecond", "First Second") == []


@pytest.mark.parametrize(
    ("source", "upstream"), [("Carbon", "carbon"), ("A &amp; B", "A & B"), ("one word", "one words")]
)
def test_projection_does_not_add_fuzzy_case_or_html_matching(source, upstream):
    assert locate_projected_claim_spans(source, upstream) == []


@pytest.mark.parametrize("source", ["", " \t\n", "中文🛢️"])
def test_empty_projection_has_no_candidates(source):
    assert locate_projected_claim_spans(source, "ASCII") == []


@pytest.mark.parametrize("value", ["", " \n\t", "café", "a\tb", "two  spaces"])
def test_noncanonical_or_blank_upstream_is_rejected(value):
    with pytest.raises(ValueError):
        locate_projected_claim_spans("a b café", value)


@pytest.mark.parametrize("value", [None, 3, b"ASCII"])
def test_non_string_inputs_are_rejected(value):
    with pytest.raises(TypeError):
        UpstreamAsciiProjectionLocator(value)
    with pytest.raises(TypeError):
        project_upstream_ascii(value)
    with pytest.raises(TypeError):
        locate_projected_claim_spans("ASCII", value)


@pytest.mark.parametrize(
    "changes", [{"start": True}, {"end": 50}, {"quote": "invented"}, {"lossy": False}, {"match_method": "fuzzy"}]
)
def test_corrupted_candidate_fails_original_slice_validation(changes):
    valid = ProjectedClaimSpan(0, 4, "café", True)
    with pytest.raises(ValueError):
        validate_projected_claim_span("café", "cafe", replace(valid, **changes))


def test_validator_rejects_partial_expansion_and_untyped_span():
    with pytest.raises(ValueError, match="Complete original slice"):
        validate_projected_claim_span("™", "TM", ProjectedClaimSpan(0, 1, "™", True))
    with pytest.raises(TypeError, match="ProjectedClaimSpan"):
        validate_projected_claim_span("ABC", "ABC", (0, 3, "ABC", False))
