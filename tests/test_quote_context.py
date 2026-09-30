import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory.rag import MAX_QUOTE_WORDS, quote_catalog


def quotes(text):
    return [p["quote"] for p in quote_catalog([
        SimpleNamespace(evidence_id="source", text=text)
    ]).values()]


def test_pdf_quantity_and_speaker_stay_with_their_qualifications():
    data = json.loads((Path(__file__).parent / "fixtures/pdf265_quote_context.json").read_text(encoding="utf-8"))
    quantity, attribution = [e["text"] for e in data["evidence"]]
    q = next(q for q in quotes(quantity) if "2.5 million" in q)
    plain = " ".join(q.split())
    assert "Just sixteen mollusks" in plain
    assert "at least two or three years — and continuously." in plain
    q = next(q for q in quotes(attribution) if "estuaries" in q)
    plain = " ".join(q.split())
    assert "We’ve shown that valvometry works" in plain
    assert "says Laurent Cazes." in plain
    assert "We now need to see if it is effective in estuaries" in plain
    for text in (quantity, attribution):
        assert all(q in text and len(q.split()) <= MAX_QUOTE_WORDS for q in quotes(text))


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\t", "\u00a0", "\u2003"])
def test_layout_whitespace_preserves_abbreviations_and_exact_source(separator):
    original = (
        "Dr. Doe says the U.S. pilot could measure 2.5 units daily. "
        "“We need another 1.25 years,” she says."
    )
    wrapped = original.replace(" ", separator)
    assert quotes(wrapped) == [wrapped]


@pytest.mark.parametrize("boundary", ["\n\n", "\r\n \t\r\n", "\f", "\n\f\n"])
def test_unpunctuated_paragraphs_and_pages_never_join(boundary):
    assert quotes("First paragraph" + boundary + "Second paragraph") == [
        "First paragraph", "Second paragraph"
    ]


def test_long_excerpt_and_truncated_tail_are_preserved_without_completing_them():
    text = "… " + " ".join(f"word{i}" for i in range(140)) + " and the planned"
    result = quotes(text)
    assert result[0].startswith("… ") and result[-1].endswith("and the planned")
    assert all(q in text and len(q.split()) <= MAX_QUOTE_WORDS for q in result)
    assert {m.group() for m in re.finditer(r"\S+", text)} == {
        word for q in result for word in q.split()
    }


@pytest.mark.parametrize("kind", ["gap", "changed", "negative", "overflow", "empty", "omitted"])
def test_invalid_segmenter_spans_cannot_produce_quotes(monkeypatch, kind):
    def segment(view):
        if kind == "omitted":
            return []
        start, end, sent = 0, len(view), view
        if kind == "gap":
            start, sent = 2, view[2:]
        elif kind == "changed":
            sent = "fabrication"
        elif kind == "negative":
            start = -1
        elif kind == "overflow":
            end += 1
        elif kind == "empty":
            end, sent = 0, ""
        return [SimpleNamespace(start=start, end=end, sent=sent)]

    monkeypatch.setattr("observatory.segmentation.pysbd.Segmenter", lambda **kwargs: SimpleNamespace(segment=segment))
    with pytest.raises(ValueError, match="Sentence"):
        quotes("Alpha\nbeta.")
