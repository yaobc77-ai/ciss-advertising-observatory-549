"""Opt-in, review-only source projection for the supplied CLAIMS paragraph run.

This independently implements the recorded notebook transformation; it never
imports or executes the notebook. The ASCII text is a lookup view only. Every
candidate retains its literal original Unicode quote and half-open Python
character coordinates. This lossy projection cannot authorize publication.
"""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from observatory.claims_spans import normalize_claim_text

UPSTREAM_NOTEBOOK_SHA256 = "8c4b6bf88a38dff90bb2446e9293577056b3d9b7a78924d23ca377ebbcba768e"
PROJECTION_METHOD = "upstream_ascii_projection_v1"
UNICODE_DATABASE_VERSION = unicodedata.unidata_version
_REPLACEMENTS = (
    ("\u201c", '"'), ("\u201d", '"'), ("\u2018", "'"), ("\u2019", "'"),
    ("\u2013", "-"), ("\u2014", "--"), ("\u2010", "-"), ("\u2011", "-"),
    ("\u2012", "-"), ("\u2015", "--"), ("\u2026", "..."), ("\u2032", "'"),
    ("\u2033", '"'), ("\u2022", "-"), ("\u00b7", "-"), ("\u2192", "->"),
    ("\u00a0", " "), ("\u200b", ""), ("\u200e", ""), ("\u200f", ""),
    ("\ufeff", ""), ("\u00ad", ""), ("\u00b0", " degrees"), ("\u00d7", "x"),
    ("\u00a9", "(c)"), ("\u00ae", "(R)"), ("\u2122", "(TM)"),
    ("\u00bd", "1/2"), ("\u00bc", "1/4"), ("\u00be", "3/4"),
)
_REPLACEMENT_MAP = dict(_REPLACEMENTS)
PROJECTION_CONTRACT_JSON = json.dumps(
    {
        "method": PROJECTION_METHOD,
        "source_notebook": "CLAIMS_2.0_model/src/notebooks/clean_paragraph_data.ipynb",
        "source_notebook_sha256": UPSTREAM_NOTEBOOK_SHA256,
        "unicode_database_version": UNICODE_DATABASE_VERSION,
        "transformation": [
            "NFC", "ordered_literal_replacements", "NFKD", "ASCII_encode_ignore",
            "collapse_repeated_ASCII_spaces", "CRLF_and_CR_to_LF",
            "split_on_blank_lines", "collapse_Unicode_whitespace_and_strip",
        ],
        "replacements": _REPLACEMENTS,
        "offsets": "half_open_original_Python_Unicode_characters",
        "candidate_validation": "reproject_complete_original_slice_equals_canonical_input",
        "normalization_units": "complete_NFC_composition_cluster_and_full_expansion",
        "deleted_boundaries": "enumerate_adjacent_zero_output_units",
        "publication_policy": "review_only",
    },
    ensure_ascii=True,
    sort_keys=True,
    separators=(",", ":"),
)
NORMALIZATION_CONTRACT_SHA256 = hashlib.sha256(PROJECTION_CONTRACT_JSON.encode("ascii")).hexdigest()


def projection_contract() -> dict:
    """Return fresh versioned metadata, including the runtime Unicode database."""
    return json.loads(PROJECTION_CONTRACT_JSON)


def _require_text(text: str, name: str) -> None:
    if not isinstance(text, str):
        raise TypeError(f"{name} must be a string")


def _fix_encoding(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    for original, replacement in _REPLACEMENTS:
        text = text.replace(original, replacement)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"  +", " ", text)


def project_upstream_ascii(text: str) -> str:
    """Project one paragraph using the pinned recipe; never modify the stored body.

    The caller supplies a paragraph. A locator additionally respects the source
    notebook's blank-line paragraph boundaries when searching a complete body.
    """
    _require_text(text, "text")
    return normalize_claim_text(_fix_encoding(text).replace("\r\n", "\n").replace("\r", "\n"))


def _require_input(upstream_text: str) -> str:
    _require_text(upstream_text, "upstream_text")
    cleaned = upstream_text.strip()
    if not cleaned:
        raise ValueError("upstream_text must contain non-whitespace text")
    if not cleaned.isascii() or normalize_claim_text(cleaned) != cleaned:
        raise ValueError("upstream_text must be canonical ASCII paragraph text")
    return cleaned


@dataclass(frozen=True, slots=True)
class ProjectedClaimSpan:
    """A review candidate; lossy marks changes beyond whitespace-only layout."""

    start: int
    end: int
    quote: str
    lossy: bool
    match_method: Literal["upstream_ascii_projection_v1"] = PROJECTION_METHOD


def validate_projected_claim_span(
    source_text: str, upstream_text: str, span: ProjectedClaimSpan
) -> None:
    """Validate exact raw location and projection equality, never label correctness."""
    _require_text(source_text, "source_text")
    needle = _require_input(upstream_text)
    if not isinstance(span, ProjectedClaimSpan):
        raise TypeError("span must be a ProjectedClaimSpan")
    if (
        type(span.start) is not int or type(span.end) is not int
        or not 0 <= span.start < span.end <= len(source_text)
    ):
        raise ValueError("Projected claim offsets are outside the original source")
    if not isinstance(span.quote, str) or source_text[span.start : span.end] != span.quote:
        raise ValueError("Projected claim quote differs from the original source slice")
    if span.match_method != PROJECTION_METHOD:
        raise ValueError("Unknown claim projection method")
    if project_upstream_ascii(span.quote) != needle:
        raise ValueError("Complete original slice does not project to the upstream input")
    expected_lossy = normalize_claim_text(span.quote) != needle
    if type(span.lossy) is not bool or span.lossy != expected_lossy:
        raise ValueError("Projected claim lossy flag differs from the original quote")


@dataclass(frozen=True, slots=True)
class _Unit:
    start: int
    end: int
    ascii_text: str


def _composition_units(source: str) -> list[_Unit]:
    """Keep normalization interactions atomic, including combining marks and Hangul."""
    ranges: list[tuple[int, int]] = []
    start = 0
    for end in range(1, len(source)):
        character = source[end]
        cluster = source[start:end]
        if unicodedata.combining(character) or (
            unicodedata.normalize("NFC", cluster + character)
            != unicodedata.normalize("NFC", cluster) + unicodedata.normalize("NFC", character)
        ):
            continue
        ranges.append((start, end))
        start = end
    if source:
        ranges.append((start, len(source)))
    if "".join(unicodedata.normalize("NFC", source[a:b]) for a, b in ranges) != unicodedata.normalize("NFC", source):
        raise ValueError("Original Unicode composition could not be projected safely")
    units = []
    for start, end in ranges:
        normalized = unicodedata.normalize("NFC", source[start:end])
        replaced = "".join(_REPLACEMENT_MAP.get(character, character) for character in normalized)
        ascii_text = unicodedata.normalize("NFKD", replaced).encode("ascii", "ignore").decode("ascii")
        units.append(_Unit(start, end, ascii_text))
    return units


def _projection_regions(units: list[_Unit]) -> list[tuple[str, list[tuple[int, int]]]]:
    characters: list[str] = []
    offsets: list[tuple[int, int]] = []
    for unit in units:
        characters.extend(unit.ascii_text)
        offsets.extend([(unit.start, unit.end)] * len(unit.ascii_text))
    fixed_text = "".join(characters)
    # Apply CRLF/CR conversion to the lookup view while preserving raw locations.
    unified: list[str] = []
    unified_offsets: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(fixed_text):
        character = fixed_text[cursor]
        if fixed_text.startswith("\r\n", cursor):
            unified.append("\n")
            unified_offsets.append((offsets[cursor][0], offsets[cursor + 1][1]))
            cursor += 2
        else:
            unified.append("\n" if character == "\r" else character)
            unified_offsets.append(offsets[cursor])
            cursor += 1
    text = "".join(unified)
    regions = []
    start = 0
    boundaries = list(re.finditer(r"\n\s*\n+", text))
    for end, next_start in [(part.start(), part.end()) for part in boundaries] + [(len(text), len(text))]:
        normalized: list[str] = []
        normalized_offsets: list[tuple[int, int]] = []
        for part in re.finditer(r"\s+|\S", text[start:end]):
            first = start + part.start()
            last = start + part.end() - 1
            value = part.group()
            if value.isspace():
                if not normalized:
                    continue
                value = " "
            normalized.append(value)
            normalized_offsets.append((unified_offsets[first][0], unified_offsets[last][1]))
        if normalized and normalized[-1] == " ":
            normalized.pop()
            normalized_offsets.pop()
        if normalized:
            regions.append(("".join(normalized), normalized_offsets))
        start = next_start
    return regions


class UpstreamAsciiProjectionLocator:
    """Cache review-only lookup views; return every valid original-span candidate.

    An expansion such as ™ -> (TM) or — -> -- is never partially matched. Adjacent
    original characters deleted by ASCII projection produce explicit boundary
    alternatives. A candidate containing deleted text or changed punctuation is
    lossy and requires review. Exact ASCII candidates still use this opt-in method.
    """

    def __init__(self, source_text: str) -> None:
        _require_text(source_text, "source_text")
        self._source_text = source_text
        self._units = _composition_units(source_text)
        self._regions = _projection_regions(self._units)
        if " ".join(text for text, _ in self._regions) != project_upstream_ascii(source_text):
            raise ValueError("ASCII lookup view differs from the pinned source transformation")
        self._unit_starts = {unit.start: index for index, unit in enumerate(self._units)}
        self._unit_ends = {unit.end: index for index, unit in enumerate(self._units)}

    def locate(self, upstream_text: str) -> list[ProjectedClaimSpan]:
        """Return sorted distinct candidates; reject blank or noncanonical input."""
        needle = _require_input(upstream_text)
        candidates: dict[tuple[int, int], ProjectedClaimSpan] = {}
        for text, offsets in self._regions:
            cursor = 0
            while (position := text.find(needle, cursor)) != -1:
                first, last = offsets[position][0], offsets[position + len(needle) - 1][1]
                starts, ends = [first], [last]
                before = self._unit_starts[first] - 1
                while before >= 0 and not self._units[before].ascii_text:
                    starts.append(self._units[before].start)
                    before -= 1
                after = self._unit_ends[last] + 1
                while after < len(self._units) and not self._units[after].ascii_text:
                    ends.append(self._units[after].end)
                    after += 1
                for start in starts:
                    for end in ends:
                        quote = self._source_text[start:end]
                        if project_upstream_ascii(quote) != needle:
                            continue
                        span = ProjectedClaimSpan(
                            start, end, quote, normalize_claim_text(quote) != needle
                        )
                        validate_projected_claim_span(self._source_text, upstream_text, span)
                        candidates[(start, end)] = span
                cursor = position + 1
        return [candidates[key] for key in sorted(candidates)]


def locate_projected_claim_spans(source_text: str, upstream_text: str) -> list[ProjectedClaimSpan]:
    """Convenience one-off lookup; prefer a cached locator for many saved paragraphs."""
    return UpstreamAsciiProjectionLocator(source_text).locate(upstream_text)
