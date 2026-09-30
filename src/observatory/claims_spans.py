"""Locate saved CLAIMS text in an original article without changing its evidence.

Coordinates are half-open Python Unicode character offsets, not UTF-8 bytes or
JavaScript UTF-16 offsets. Matching permits only collapsing consecutive Unicode
whitespace to one ASCII space and stripping whitespace at both ends. It does
not establish that a match belongs to the correct article or supports a label.
"""

import re
from dataclasses import dataclass
from typing import Literal

MatchMethod = Literal["exact", "whitespace_normalized"]


def _require_text(text: str, name: str) -> None:
    if not isinstance(text, str):
        raise TypeError(f"{name} must be a string")


def normalize_claim_text(text: str) -> str:
    """Collapse Unicode whitespace and strip ends; preserve every other character."""
    _require_text(text, "text")
    return " ".join(text.split())


def _require_upstream_text(upstream_text: str) -> str:
    _require_text(upstream_text, "upstream_text")
    if not upstream_text.strip():
        raise ValueError("upstream_text must contain non-whitespace text")
    return upstream_text.strip()


@dataclass(frozen=True, slots=True)
class ClaimSpan:
    """One original source occurrence; multiple occurrences require review."""

    start: int
    end: int
    quote: str
    match_method: MatchMethod


def validate_claim_span(source_text: str, upstream_text: str, span: ClaimSpan) -> None:
    """Raise if a span's offsets, literal quote or declared match method are invalid.

    This verifies source location only. It does not resolve duplicate occurrences
    or assert article identity, classification correctness or human approval.
    """
    _require_text(source_text, "source_text")
    trimmed_upstream = _require_upstream_text(upstream_text)
    if not isinstance(span, ClaimSpan):
        raise TypeError("span must be a ClaimSpan")
    if (
        type(span.start) is not int
        or type(span.end) is not int
        or not 0 <= span.start < span.end <= len(source_text)
    ):
        raise ValueError("Claim span offsets are outside the original source")
    if not isinstance(span.quote, str) or span.quote != source_text[span.start : span.end]:
        raise ValueError("Claim span quote differs from the original source slice")
    if not span.quote.strip() or span.quote != span.quote.strip():
        raise ValueError("Claim span must exclude surrounding whitespace")
    if span.match_method == "exact":
        if span.quote != trimmed_upstream:
            raise ValueError("Exact claim span differs from the trimmed upstream text")
    elif span.match_method == "whitespace_normalized":
        if span.quote == trimmed_upstream:
            raise ValueError("An exact claim span must declare the exact match method")
        if normalize_claim_text(span.quote) != normalize_claim_text(upstream_text):
            raise ValueError("Normalized claim span contains a non-whitespace difference")
    else:
        raise ValueError("Unknown claim span match method")


class ClaimSpanLocator:
    """Cache one original article's normalized view for repeated upstream lookups.

    Each normalized character maps to its complete original character interval.
    A normalized space maps to the whole original whitespace run. Consequently,
    returned quotes preserve tabs, newlines, non-breaking spaces and all other
    source characters. Empty source text is valid and has no matches.
    """

    def __init__(self, source_text: str) -> None:
        _require_text(source_text, "source_text")
        self._source_text = source_text
        characters: list[str] = []
        offsets: list[tuple[int, int]] = []
        for part in re.finditer(r"\s+|\S", source_text):
            value = part.group()
            if value.isspace():
                if characters:
                    characters.append(" ")
                    offsets.append((part.start(), part.end()))
            else:
                characters.append(value)
                offsets.append((part.start(), part.end()))
        if characters and characters[-1] == " ":
            characters.pop()
            offsets.pop()
        self._normalized_text = "".join(characters)
        self._offsets = offsets

    def locate(self, upstream_text: str) -> list[ClaimSpan]:
        """Return every source occurrence, including overlapping and repeated ones.

        ``exact`` means the literal original quote equals upstream text with only
        its surrounding whitespace removed. Other matches are explicitly marked
        ``whitespace_normalized``. No occurrence is preferred or selected as the
        correct one; an exact occurrence and a differently laid out occurrence
        both remain visible. Blank upstream text raises ValueError.
        """
        trimmed_upstream = _require_upstream_text(upstream_text)
        needle = normalize_claim_text(upstream_text)
        matches: list[ClaimSpan] = []
        cursor = 0
        while (position := self._normalized_text.find(needle, cursor)) != -1:
            start = self._offsets[position][0]
            end = self._offsets[position + len(needle) - 1][1]
            quote = self._source_text[start:end]
            method: MatchMethod = "exact" if quote == trimmed_upstream else "whitespace_normalized"
            span = ClaimSpan(start=start, end=end, quote=quote, match_method=method)
            validate_claim_span(self._source_text, upstream_text, span)
            matches.append(span)
            cursor = position + 1
        return matches


def locate_claim_spans(source_text: str, upstream_text: str) -> list[ClaimSpan]:
    """Locate all matches once; use ClaimSpanLocator to search one article repeatedly."""
    return ClaimSpanLocator(source_text).locate(upstream_text)
