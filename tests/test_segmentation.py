"""Opt-in coverage preserves source without changing the strict frozen policy."""

from itertools import pairwise
from types import SimpleNamespace

import pytest

from observatory.chunking import chunk_body, chunk_retrieval_body
from observatory.indexing import definition
from observatory.segmentation import (
    COVERAGE_SEGMENTATION_PROFILE,
    SEGMENTATION_PROFILE,
    sentence_spans,
    sentence_spans_with_status,
)


def assert_coverage(text, spans):
    cursor = 0
    for start, end in spans:
        assert cursor <= start < end <= len(text)
        assert not text[cursor:start].strip()
        assert text[start:end] == text[start:end].strip()
        cursor = end
    assert not text[cursor:].strip()


def test_actual_heat_symbol_collision_is_preserved_as_source_not_deleted():
    # Observed in energyinstitute/status/1684173011681783815, source tail [110,167).
    body = "♨️Where are new district heating networks being explored?"
    with pytest.raises(ValueError, match="omitted source text"):
        sentence_spans(body)
    result = sentence_spans_with_status(body, coverage_fallback=True)
    assert result.spans == ((0, len(body)),)
    assert result.fallbacks[0].reason == "empty_prediction"
    assert body[result.fallbacks[0].start:result.fallbacks[0].end] == body
    assert_coverage(body, result.spans)


@pytest.mark.parametrize("case", ["prefix_gap", "internal_gap", "suffix_gap", "empty"])
def test_legal_predictions_keep_their_spans_and_fill_only_uncovered_source(monkeypatch, case):
    text = "Alpha. GAP. Beta."
    spans = [SimpleNamespace(start=0, end=6, sent=text[:6]), SimpleNamespace(start=12, end=17, sent=text[12:])]
    if case == "prefix_gap":
        spans = [SimpleNamespace(start=7, end=len(text), sent=text[7:])]
    elif case == "suffix_gap":
        spans = [spans[0]]
    elif case == "empty":
        spans = []
    monkeypatch.setattr("observatory.segmentation.pysbd.Segmenter", lambda **kwargs: SimpleNamespace(segment=lambda view: spans))
    result = sentence_spans_with_status(text, coverage_fallback=True)
    assert result.fallbacks
    assert_coverage(text, result.spans)
    for predicted in spans:
        assert (predicted.start, predicted.end) in result.spans


@pytest.mark.parametrize("bad", ["negative", "overflow", "overlap", "wrong_text", "not_int"])
def test_invalid_predictions_discard_entire_region_before_recovering(monkeypatch, bad):
    text = "Alpha. Beta."
    good = SimpleNamespace(start=0, end=6, sent=text[:6])
    invalid = SimpleNamespace(start=7, end=len(text), sent=text[7:])
    if bad == "negative":
        invalid.start = -1
    elif bad == "overflow":
        invalid.end += 1
    elif bad == "overlap":
        invalid = good
    elif bad == "wrong_text":
        invalid.sent = "Changed source"
    else:
        invalid.start = False
    monkeypatch.setattr("observatory.segmentation.pysbd.Segmenter", lambda **kwargs: SimpleNamespace(segment=lambda view: [good, invalid]))
    result = sentence_spans_with_status(text, coverage_fallback=True)
    assert result.spans == ((0, len(text)),)
    assert result.fallbacks[0].reason == "invalid_segmenter_offsets_or_text"
    assert_coverage(text, result.spans)
    with pytest.raises(ValueError, match="Sentence"):
        sentence_spans(text)


def test_third_party_exception_is_visible_and_opt_in_only(monkeypatch):
    def failure(view):
        raise RuntimeError("synthetic third-party failure")
    monkeypatch.setattr("observatory.segmentation.pysbd.Segmenter", lambda **kwargs: SimpleNamespace(segment=failure))
    with pytest.raises(RuntimeError, match="third-party"):
        sentence_spans("Alpha.")
    result = sentence_spans_with_status("Alpha.", coverage_fallback=True)
    assert result.spans == ((0, 6),) and result.fallbacks[0].reason == "segmenter_exception:RuntimeError"


def test_source_layout_and_separate_regions_are_preserved_in_recovery():
    text = "  ♨️Where is heating planned?\r\n\r\nSecond region.\fThird region 🙂.  "
    result = sentence_spans_with_status(text, coverage_fallback=True)
    assert_coverage(text, result.spans)
    assert all("\f" not in text[a:b] and "\r\n\r\n" not in text[a:b] for a, b in result.spans)


def test_new_coverage_chunks_retain_all_source_and_token_limits():
    body = "♨️Where are new district heating networks being explored?\n" + "中文🙂 evidence " * 130
    chunks = chunk_body(body, 35, 7, strategy="sentence_coverage")
    cursor = 0
    for chunk in chunks:
        start, end = chunk["start"], chunk["end"]
        assert chunk["text"] == body[start:end] and chunk["token_count"] <= 35
        assert not body[cursor:start].strip()
        cursor = max(cursor, end)
    assert not body[cursor:].strip()
    assert all(a["start"] < b["start"] and a["end"] < b["end"] for a, b in pairwise(chunks))


def test_coverage_strategy_preserves_global_retained_ranges_and_excludes_other_text():
    first = "♨️Where is district heating planned?"
    unrelated = "\n\nEXCLUDED unrelated article\n\n"
    last = "♨️Where is heat reused?"
    body = first + unrelated + last
    ranges = [(0, len(first)), (len(first + unrelated), len(body))]
    chunks = chunk_retrieval_body(body, 600, 100, retrieval_ranges=ranges, strategy="sentence_coverage")
    assert len(chunks) == 2
    assert [(chunk["start"], chunk["end"]) for chunk in chunks] == ranges
    assert all("EXCLUDED" not in chunk["text"] for chunk in chunks)


def test_strict_profile_definition_does_not_include_the_opt_in_policy():
    spec = definition("sentence600-v1")
    assert spec["strategy"] == "sentence" and spec["segmentation"] == SEGMENTATION_PROFILE
    assert spec["segmentation"] != COVERAGE_SEGMENTATION_PROFILE
    with pytest.raises(ValueError, match="Unknown retrieval profile"):
        definition("sentence600-coverage-v1")
    assert sentence_spans("Normal sentence. Another sentence.") == sentence_spans("Normal sentence. Another sentence.", coverage_fallback=True)


def test_fallback_policy_requires_an_explicit_boolean():
    with pytest.raises(TypeError, match="explicit boolean"):
        sentence_spans("Alpha.", coverage_fallback="false")
