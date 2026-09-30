"""Paragraph-first token-bounded chunks with exact Python-character offsets."""

import re
from functools import lru_cache

import tiktoken

from .segmentation import sentence_spans

CHUNKING_PROFILE_VERSION = "chunking-strategies-v1"


@lru_cache(maxsize=1)
def _encoding():
    return tiktoken.get_encoding("cl100k_base")


def _tokens(text: str) -> int:
    return len(_encoding().encode(text, disallowed_special=()))


def _paragraphs(body: str) -> list[tuple[int, int, str]]:
    spans = []
    start = 0
    for separator in re.finditer(r"(?:\r?\n)[ \t]*(?:\r?\n)+", body):
        if body[start : separator.start()].strip():
            spans.append((start, separator.start(), f"p{len(spans) + 1}"))
        start = separator.end()
    if body[start:].strip():
        spans.append((start, len(body), f"p{len(spans) + 1}"))
    return spans


def retrieval_spans(
    body: str,
    *,
    retrieval_ranges: list[tuple[int, int]] | None = None,
    retrieval_end: int | None = None,
) -> list[tuple[int, int]]:
    """Resolve original half-open spans. Explicit ranges override the legacy prefix."""
    if not isinstance(body, str):
        raise TypeError("body must be a string")
    if retrieval_ranges is not None:
        if not retrieval_ranges:
            raise ValueError("retrieval_ranges must contain at least one nonempty range")
        previous_end = 0
        spans = []
        for start, end in retrieval_ranges:
            if type(start) is not int or type(end) is not int:
                raise ValueError("retrieval_ranges require integer character offsets")
            if not 0 <= start < end <= len(body):
                raise ValueError("retrieval_ranges must be nonempty and inside the original body")
            if start < previous_end:
                raise ValueError("retrieval_ranges must be ordered and non-overlapping")
            spans.append((start, end))
            previous_end = end
        return spans
    end = len(body) if retrieval_end is None else retrieval_end
    if type(end) is not int or not 0 <= end <= len(body):
        raise ValueError("Retrieval boundary exceeds original body or is invalid")
    return [(0, end)] if end else []


def _token_prefix_end(body: str, start: int, max_tokens: int) -> int:
    """Find a token prefix ending on a Unicode character boundary, never decode a split byte."""
    encoded = _encoding().encode(body[start:], disallowed_special=())
    if len(encoded) <= max_tokens:
        return len(body)
    byte_count = sum(
        len(_encoding().decode_single_token_bytes(token))
        for token in encoded[:max_tokens]
    )
    prefix = body[start:].encode("utf-8")[:byte_count].decode("utf-8", errors="ignore")
    end = start + len(prefix)
    # Retokenizing a character prefix can change the final token boundary.
    while end > start and _tokens(body[start:end]) > max_tokens:
        end -= 1
    if end == start:
        raise ValueError("max_tokens is too small to hold the next Unicode character")
    return end


def _overlap_start(body: str, start: int, end: int, overlap_tokens: int) -> int:
    if not overlap_tokens:
        return end
    encoded = _encoding().encode(body[start:end], disallowed_special=())
    byte_count = sum(
        len(_encoding().decode_single_token_bytes(token))
        for token in encoded[-overlap_tokens:]
    )
    tail = (
        body[start:end].encode("utf-8")[-byte_count:].decode("utf-8", errors="ignore")
    )
    next_start = end - len(tail)
    while next_start < end and _tokens(body[next_start:end]) > overlap_tokens:
        next_start += 1
    # A paragraph-sized chunk can contain fewer tokens than requested overlap.
    # Bound overlap to leave at least half this chunk as forward progress.
    return max(next_start, start + max(1, (end - start) // 2))


def chunk_body(
    body: str, max_tokens: int = 600, overlap_tokens: int = 100,
    *, strategy: str = "legacy",
) -> list[dict]:
    if not isinstance(body, str):
        raise TypeError("body must be a string")
    if strategy not in ("legacy", "sentence"):
        raise ValueError("strategy must be 'legacy' or 'sentence'")
    if max_tokens < 1 or overlap_tokens < 0 or overlap_tokens >= max_tokens:
        raise ValueError("require max_tokens > overlap_tokens >= 0")
    if not body.strip():
        return []
    if strategy == "sentence":
        return _sentence_chunks(body, max_tokens, overlap_tokens)
    paragraphs = _paragraphs(body)
    chunks = []
    start = 0
    while start < len(body):
        limit = _token_prefix_end(body, start, max_tokens)
        # Use the last whole paragraph that fits; long single paragraphs split
        # at token/character boundaries. No normalization changes the offsets.
        fitting = [
            end
            for pstart, end, _ in paragraphs
            if start < end <= limit and pstart >= start
        ]
        end = max(fitting) if fitting and limit < len(body) else limit
        if end <= start:
            raise ValueError("chunking made no forward progress")
        text = body[start:end]
        if text.strip():
            chunks.append(
                {
                    "text": text,
                    "start": start,
                    "end": end,
                    "paragraph_ids": [
                        pid
                        for pstart, pend, pid in paragraphs
                        if pstart < end and pend > start
                    ],
                    "token_count": _tokens(text),
                }
            )
        if end == len(body):
            break
        start = _overlap_start(body, start, end, overlap_tokens)
    return chunks


def _sentence_chunks(body: str, max_tokens: int, overlap_tokens: int) -> list[dict]:
    """Pack sentence endings; use token boundaries only when no ending fits.

    Overlap uses a sentence start within the requested token allowance, or no
    overlap when a whole sentence cannot fit that allowance. Cuts inside an
    overlong sentence use the existing Unicode-safe token overlap fallback.
    """
    sentences = sentence_spans(body)
    paragraphs = _paragraphs(body)
    chunks = []
    start = 0
    while start < len(body):
        limit = _token_prefix_end(body, start, max_tokens)
        fitting = [
            end for _, end in sentences
            if start < end <= limit and _tokens(body[start:end]) <= max_tokens
        ]
        end = max(fitting) if fitting and limit < len(body) else limit
        # Preserve separators too; they do not turn the next sentence into a
        # partial sentence. The precomputed token prefix remains the upper bound.
        while (
            end < limit and body[end].isspace()
            and _tokens(body[start:end + 1]) <= max_tokens
        ):
            end += 1
        if end <= start:
            raise ValueError("chunking made no forward progress")
        text = body[start:end]
        if text.strip():
            chunks.append({
                "text": text,
                "start": start,
                "end": end,
                "paragraph_ids": [
                    pid for a, b, pid in paragraphs if a < end and b > start
                ],
                "token_count": _tokens(text),
            })
        if end == len(body):
            break
        if not overlap_tokens:
            start = end
            continue
        overlap_start = _overlap_start(body, start, end, overlap_tokens)
        starts = [
            a for a, _ in sentences
            if overlap_start <= a < end and a > start
            and _tokens(body[a:end]) <= overlap_tokens
        ]
        if starts:
            candidate = min(starts)
            next_sentence_end = next((b for _, b in sentences if b > end), None)
            # Do not emit a duplicate overlap-only chunk or split an otherwise
            # fitting next sentence merely to retain overlap from the last one.
            if (
                any(a < end < b for a, b in sentences)
                or next_sentence_end is None
                or _tokens(body[candidate:next_sentence_end]) <= max_tokens
            ):
                start = candidate
            else:
                start = end
        elif any(a < end < b for a, b in sentences):
            start = overlap_start
        else:
            start = end
    return chunks


def chunk_retrieval_body(
    body: str,
    max_tokens: int = 600,
    overlap_tokens: int = 100,
    *,
    retrieval_ranges: list[tuple[int, int]] | None = None,
    retrieval_end: int | None = None,
    strategy: str = "legacy",
) -> list[dict]:
    """Chunk each retained interval separately, retaining full-body paragraph IDs."""
    if max_tokens < 1 or overlap_tokens < 0 or overlap_tokens >= max_tokens:
        raise ValueError("require max_tokens > overlap_tokens >= 0")
    if strategy not in ("legacy", "sentence"):
        raise ValueError("strategy must be 'legacy' or 'sentence'")
    ranges = retrieval_spans(
        body, retrieval_ranges=retrieval_ranges, retrieval_end=retrieval_end
    )
    paragraphs = _paragraphs(body)
    chunks = []
    for region_start, region_end in ranges:
        for chunk in chunk_body(
            body[region_start:region_end], max_tokens, overlap_tokens, strategy=strategy
        ):
            chunk["start"] += region_start
            chunk["end"] += region_start
            chunk["paragraph_ids"] = [
                pid for start, end, pid in paragraphs
                if start < chunk["end"] and end > chunk["start"]
            ]
            assert body[chunk["start"]:chunk["end"]] == chunk["text"]
            chunks.append(chunk)
    return chunks
