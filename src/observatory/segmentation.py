"""Validated sentence spans over an offset-preserving view of source text."""

import re
from dataclasses import dataclass

import pysbd

SEGMENTATION_PROFILE = "pysbd-en-layout-v1"
COVERAGE_SEGMENTATION_PROFILE = "pysbd-en-layout-coverage-v1"
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


@dataclass(frozen=True)
class SourceFallback:
    start: int
    end: int
    reason: str


@dataclass(frozen=True)
class SentenceSpanResult:
    spans: tuple[tuple[int, int], ...]
    fallbacks: tuple[SourceFallback, ...]
    profile: str


def sentence_spans_with_status(text: str, *, coverage_fallback: bool = False) -> SentenceSpanResult:
    """Return trimmed, ordered source spans from validated English pySBD output.

    Each whitespace character becomes one space only in the segmentation view.
    The frozen strict policy remains the default. Opt-in coverage policy retains
    uncovered source spans or the whole region when predicted positions fail.
    Those fallbacks preserve text; they do not claim linguistic sentence bounds.
    """
    regions = text_regions(text)
    if type(coverage_fallback) is not bool:
        raise TypeError("coverage_fallback must be an explicit boolean")
    profile = COVERAGE_SEGMENTATION_PROFILE if coverage_fallback else SEGMENTATION_PROFILE
    if not regions:
        return SentenceSpanResult((), (), profile)
    segmenter = pysbd.Segmenter(language="en", clean=False, char_span=True)
    sentences, fallbacks = [], []
    for region_start, region_end in regions:
        source = text[region_start:region_end]
        view = re.sub(r"\s", " ", source)
        cursor, region_spans, region_fallbacks = 0, [], []
        try:
            predicted = list(segmenter.segment(view))
        except Exception as exc:
            if not coverage_fallback:
                raise
            sentences.append((region_start, region_end))
            fallbacks.append(SourceFallback(region_start, region_end, "segmenter_exception:" + type(exc).__name__))
            continue
        invalid_prediction = False
        for span in predicted:
            start = getattr(span, "start", None)
            end = getattr(span, "end", None)
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= cursor <= start < end <= len(view)
                or view[start:end] != getattr(span, "sent", None)
            ):
                if not coverage_fallback:
                    raise ValueError("Sentence offsets failed source validation")
                invalid_prediction = True
                break
            if view[cursor:start].strip():
                if not coverage_fallback:
                    raise ValueError("Sentence offsets failed source validation")
                gap_start, gap_end = _trim_span(source, cursor, start)
                region_spans.append((region_start + gap_start, region_start + gap_end))
                region_fallbacks.append(SourceFallback(region_start + gap_start, region_start + gap_end, "uncovered_prefix_or_gap"))
            cursor = end
            start, end = _trim_span(source, start, end)
            if start < end:
                region_spans.append((region_start + start, region_start + end))
        if invalid_prediction:
            # Do not combine half a failed prediction with a whole-region fallback.
            sentences.append((region_start, region_end))
            fallbacks.append(SourceFallback(region_start, region_end, "invalid_segmenter_offsets_or_text"))
            continue
        if view[cursor:].strip():
            if not coverage_fallback:
                raise ValueError("Sentence segmentation omitted source text")
            tail_start, tail_end = _trim_span(source, cursor, len(source))
            region_spans.append((region_start + tail_start, region_start + tail_end))
            region_fallbacks.append(SourceFallback(region_start + tail_start, region_start + tail_end, "uncovered_suffix" if predicted else "empty_prediction"))
        sentences.extend(region_spans)
        fallbacks.extend(region_fallbacks)
    return SentenceSpanResult(tuple(sentences), tuple(fallbacks), profile)


def sentence_spans(text: str, *, coverage_fallback: bool = False) -> list[tuple[int, int]]:
    """Return source spans; strict behavior is unchanged unless explicitly opted in."""
    return list(sentence_spans_with_status(text, coverage_fallback=coverage_fallback).spans)
