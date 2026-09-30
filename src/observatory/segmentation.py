"""Validated sentence spans over an offset-preserving view of source text."""

import re

import pysbd

SEGMENTATION_PROFILE = "pysbd-en-layout-v1"
_REGION_SEPARATOR = re.compile(r"\f|(?:\r?\n)[ \t]*(?:\r?\n)+")


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    source = text[start:end]
    return start + len(source) - len(source.lstrip()), start + len(source.rstrip())


def text_regions(text: str) -> list[tuple[int, int]]:
    """Return nonempty original spans separated by blank paragraphs or form feeds.

    Both ends exclude whitespace. Single layout newlines remain inside a region.
    Region boundaries constrain sentence detection; they are not source repairs.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    regions = []
    cursor = 0
    for separator in _REGION_SEPARATOR.finditer(text):
        start, end = _trim_span(text, cursor, separator.start())
        if start < end:
            regions.append((start, end))
        cursor = separator.end()
    start, end = _trim_span(text, cursor, len(text))
    if start < end:
        regions.append((start, end))
    return regions


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Return trimmed, ordered source spans from validated English pySBD output.

    Each whitespace character becomes one space only in the segmentation view.
    Bad, overlapping or incomplete spans fail explicitly; the original text and
    its Python-character coordinates never change. A predicted span is not a
    guarantee of linguistic sentence completeness or a complete source article.
    """
    regions = text_regions(text)
    if not regions:
        return []
    segmenter = pysbd.Segmenter(language="en", clean=False, char_span=True)
    sentences = []
    for region_start, region_end in regions:
        source = text[region_start:region_end]
        view = re.sub(r"\s", " ", source)
        cursor = 0
        for span in segmenter.segment(view):
            start = getattr(span, "start", None)
            end = getattr(span, "end", None)
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= cursor <= start < end <= len(view)
                or view[cursor:start].strip()
                or view[start:end] != getattr(span, "sent", None)
            ):
                raise ValueError("Sentence offsets failed source validation")
            cursor = end
            start, end = _trim_span(source, start, end)
            if start < end:
                sentences.append((region_start + start, region_start + end))
        if view[cursor:].strip():
            raise ValueError("Sentence segmentation omitted source text")
    return sentences
