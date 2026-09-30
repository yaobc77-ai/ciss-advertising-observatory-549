from itertools import pairwise
from types import SimpleNamespace

import pytest
import tiktoken

from observatory.chunking import chunk_body, chunk_retrieval_body
from observatory.segmentation import sentence_spans, text_regions


def token_count(text):
    return len(tiktoken.get_encoding("cl100k_base").encode(text, disallowed_special=()))


def assert_source_contract(body, chunks, maximum, ranges=None):
    covered = set()
    ranges = ranges or [(0, len(body))]
    for chunk in chunks:
        start, end = chunk["start"], chunk["end"]
        assert chunk["text"] == body[start:end]
        assert chunk["token_count"] == token_count(chunk["text"])
        assert 0 < chunk["token_count"] <= maximum
        assert any(a <= start < end <= b for a, b in ranges)
        covered.update(range(start, end))
    for a, b in ranges:
        assert all(i in covered for i in range(a, b) if not body[i].isspace())
    assert all(
        a["start"] < b["start"] and a["end"] < b["end"]
        for a, b in pairwise(chunks)
    )


def test_segmentation_preserves_layout_offsets_and_numeric_abbreviations():
    text = "  Dr. Smith measured\r\n1.5 litres in café 🙂.\nIt worked.  "
    spans = sentence_spans(text)
    assert [text[a:b] for a, b in spans] == [
        "Dr. Smith measured\r\n1.5 litres in café 🙂.",
        "It worked.",
    ]
    assert spans[0][0] == 2
    assert all(text[a:b] == text[a:b].strip() for a, b in spans)


@pytest.mark.parametrize("boundary", ["\n\n", "\r\n \t\r\n", "\f", "\n\n\f"])
def test_hard_regions_stop_unpunctuated_sentences_at_source_boundaries(boundary):
    text = " \tFirst paragraph" + boundary + "Second paragraph \n"
    regions = text_regions(text)
    assert [text[a:b] for a, b in regions] == ["First paragraph", "Second paragraph"]
    assert sentence_spans(text) == regions


def test_empty_input_and_type_contract():
    assert sentence_spans(" \n\n\f\t") == []
    assert text_regions("") == []
    with pytest.raises(TypeError):
        sentence_spans(None)


@pytest.mark.parametrize("bad", [
    "negative", "overflow", "overlap", "prefix_gap", "suffix_gap", "wrong_text", "not_int",
])
def test_invalid_segmenter_spans_fail_without_silent_text_loss(monkeypatch, bad):
    def segment(view):
        length = len(view)
        good = SimpleNamespace(start=0, end=length, sent=view)
        if bad == "negative":
            return [SimpleNamespace(start=-1, end=length, sent=view)]
        if bad == "overflow":
            return [SimpleNamespace(start=0, end=length + 1, sent=view)]
        if bad == "overlap":
            return [good, good]
        if bad == "prefix_gap":
            return [SimpleNamespace(start=1, end=length, sent=view[1:])]
        if bad == "suffix_gap":
            return [SimpleNamespace(start=0, end=length - 1, sent=view[:-1])]
        if bad == "not_int":
            return [SimpleNamespace(start=False, end=length, sent=view)]
        return [SimpleNamespace(start=0, end=length, sent="Changed source")]

    monkeypatch.setattr(
        "observatory.segmentation.pysbd.Segmenter",
        lambda **kwargs: SimpleNamespace(segment=segment),
    )
    with pytest.raises(ValueError, match="Sentence"):
        sentence_spans("Alpha\nbeta.")


def test_sentence_ends_and_overlap_starts_align_when_sentences_fit():
    body = " ".join(
        f"The {name} team measured emissions carefully during the project."
        for name in ["first", "second", "third", "fourth", "fifth", "sixth", "seventh"]
    )
    maximum, overlap = 38, 13
    chunks = chunk_body(body, maximum, overlap, strategy="sentence")
    sentences = sentence_spans(body)
    assert len(chunks) >= 3
    assert_source_contract(body, chunks, maximum)
    starts = {a for a, _ in sentences}
    ends = {b for _, b in sentences}
    for chunk in chunks:
        assert chunk["start"] + len(chunk["text"].rstrip()) in ends
        assert chunk["start"] in starts
        next_end = next((b for _, b in sentences if b > chunk["end"]), None)
        if next_end is not None:
            assert token_count(body[chunk["start"]:next_end]) > maximum
    assert any(b["start"] < a["end"] for a, b in pairwise(chunks))
    for a, b in pairwise(chunks):
        assert token_count(body[b["start"]:a["end"]]) <= overlap


def test_drop_overlap_instead_of_splitting_a_next_sentence_that_fits_alone():
    first = "Alpha " + "word " * 12 + "ends. "
    second = "Beta " + "word " * 12 + "ends. "
    third = "Gamma " + "word " * 24 + "ends."
    body = first + second + third
    maximum = token_count(first + second)
    assert token_count(third) <= maximum
    chunks = chunk_body(body, maximum, token_count(second), strategy="sentence")
    assert_source_contract(body, chunks, maximum)
    assert len(chunks) == 2
    assert chunks[0]["text"] == first + second
    assert chunks[1]["text"] == third


def test_short_sentence_overlap_before_overlong_sentence_never_repeats_old_end():
    first = "Alpha team measured emissions. Beta team checked the results. "
    long_sentence = "Gamma " + "unpunctuated detail " * 70 + "ends."
    body = first + long_sentence
    chunks = chunk_body(body, 32, 12, strategy="sentence")
    assert_source_contract(body, chunks, 32)
    assert len(chunks) > 5
    assert chunks[0]["text"] == first
    assert chunks[1]["start"] == len(first)
    assert all(b["end"] > a["end"] for a, b in pairwise(chunks))


@pytest.mark.parametrize("overlap", [0, 7])
def test_overlong_unicode_sentence_fallback_is_bounded_and_covers_source(overlap):
    body = "中文与🙂 café e\u0301 <|endoftext|> " * 60 + "ends."
    chunks = chunk_body(body, 35, overlap, strategy="sentence")
    assert len(chunks) > 5
    assert_source_contract(body, chunks, 35)
    assert all("\ufffd" not in c["text"] for c in chunks)
    assert all(b["start"] <= a["end"] for a, b in pairwise(chunks))


def test_retained_ranges_keep_global_offsets_and_never_bridge_excluded_navigation():
    first = "First article statement. More detail follows. " * 8
    excluded = "\n\nUNRELATED LINK: another article is not evidence.\n\n"
    last = "Last article statement. Further results follow. " * 8
    body = first + excluded + last
    ranges = [(0, len(first)), (len(first + excluded), len(body))]
    chunks = chunk_retrieval_body(
        body, 30, 8, retrieval_ranges=ranges, retrieval_end=4, strategy="sentence"
    )
    assert_source_contract(body, chunks, 30, ranges)
    assert all("UNRELATED" not in c["text"] for c in chunks)
    assert all(c["paragraph_ids"] == ["p1"] for c in chunks if c["end"] <= len(first))
    assert all(c["paragraph_ids"] == ["p3"] for c in chunks if c["start"] >= ranges[1][0])


def test_legacy_default_is_unchanged_and_does_not_call_sentence_detector(monkeypatch):
    def unexpected(_):
        raise AssertionError("legacy must not invoke sentence detection")

    monkeypatch.setattr("observatory.chunking.sentence_spans", unexpected)
    body = "Alpha energy sentence. " * 20 + "\n\n" + "Beta fuel sentence. " * 20
    assert chunk_body(body, 100, 15) == chunk_body(body, 100, 15, strategy="legacy")
    assert chunk_retrieval_body(body, 100, 15) == chunk_retrieval_body(
        body, 100, 15, strategy="legacy"
    )


@pytest.mark.parametrize("function", [chunk_body, chunk_retrieval_body])
def test_unknown_strategy_is_rejected_even_for_empty_source(function):
    with pytest.raises(ValueError, match="strategy"):
        function("", strategy="misspelled")
