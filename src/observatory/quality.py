"""Conservative, explainable checks; findings never rewrite source text."""

import hashlib
import re
from urllib.parse import unquote, urlsplit

from observatory.models import Issue, RecordInput

PLACEHOLDERS = frozenset(
    {"none", "null", "nan", "n/a", "na", "n.a.", "unknown", "-", "--"}
)
MIN_BODY_WORDS = 40
_MOJIBAKE = re.compile(r"‚Ä|â€|ï»¿|\ufffd")
_RELATED_NAVIGATION = re.compile(
    r"for more on the subject\s*:|read more stories", re.IGNORECASE
)
_FOOTER = re.compile(
    r"news (?:and )?editorial staff.{0,100}no role|"
    r"newsroom was not involved|all rights reserved|privacy policy|"
    r"cookie (?:policy|preferences)|terms (?:of use|and conditions)",
    re.IGNORECASE | re.DOTALL,
)


def missing(value: object) -> bool:
    return value is None or (
        isinstance(value, str)
        and (not value.strip() or value.strip().lower() in PLACEHOLDERS)
    )


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def retrieval_boundary(body: str) -> int | None:
    """First explicit navigation marker; original text is never deleted."""
    match = _RELATED_NAVIGATION.search(body)
    return match.start() if match else None


def valid_url(value: object) -> bool:
    if not isinstance(value, str) or any(c.isspace() for c in value.strip()):
        return False
    try:
        parsed = urlsplit(value.strip())
        return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def asset_url_reason(url: str) -> str | None:
    """Exclude explicit image files / image-size resources, never infer from topic."""
    leaf = unquote(urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1]
    if re.search(r"\.(?:png|jpe?g|gif|webp|svg|ico|avif)$", leaf, re.IGNORECASE):
        return "URL path ends in an image-file extension"
    if re.search(r"(?:^|[-_])\d{2,5}x\d{2,5}(?:\.[a-z]+)?$", leaf, re.IGNORECASE):
        return "URL final path segment ends in explicit image dimensions"
    return None


def inspect_body(body: str) -> list[Issue]:
    if body is None or (isinstance(body, str) and not body.strip()):
        return [
            Issue(
                code="body_missing",
                severity="error",
                detail="No article/post text is available.",
            )
        ]
    if not isinstance(body, str):
        return [
            Issue(
                code="body_numeric"
                if isinstance(body, (int, float))
                else "body_wrong_type",
                severity="error",
                detail="Body is not a text value.",
            )
        ]
    stripped = body.strip()
    if stripped.casefold() == "video":
        return [
            Issue(
                code="body_video_placeholder",
                detail="Body contains only the video placeholder, not a transcript.",
            )
        ]
    if stripped.casefold() in PLACEHOLDERS:
        return [
            Issue(
                code="body_missing",
                severity="error",
                detail="Body contains only a missing-value placeholder.",
            )
        ]
    if re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", stripped):
        return [
            Issue(
                code="body_numeric",
                severity="error",
                detail="Body contains only a numeric value.",
            )
        ]

    issues = []
    words = re.findall(r"\b\w+\b", stripped, re.UNICODE)
    boundary = retrieval_boundary(body)
    if boundary is not None:
        issues.append(
            Issue(
                code="body_related_navigation",
                severity="info",
                detail=f"Explicit related-story navigation starts at character {boundary}; retrieval excludes [{boundary}, {len(body)}). This suffix may also contain valid later article text; original body is preserved for boundary review.",
            )
        )
    if 2800 <= len(stripped) <= 3000 and stripped.endswith("..."):
        issues.append(
            Issue(
                code="body_truncated_suspected",
                severity="info",
                detail="Body ends with an ellipsis near the observed 3000-character source limit. Available passages may be retrieved, but must not support claims about absence in a complete article.",
            )
        )
    if _MOJIBAKE.search(body):
        issues.append(
            Issue(
                code="body_garbled",
                detail="Known mojibake/replacement-character markers require text review.",
            )
        )
    if len(words) < MIN_BODY_WORDS:
        issues.append(
            Issue(
                code="body_short",
                detail=f"Fewer than {MIN_BODY_WORDS} words; may be a title, caption, or incomplete extraction.",
            )
        )
    if (
        len(words) < 80
        and stripped.endswith("?")
        and not re.search(r"[.!](?:\s|$)", stripped)
    ):
        issues.append(
            Issue(
                code="body_question_only",
                detail="Short text contains only question-like content; a title is not a full article.",
            )
        )
    # Full articles can legitimately contain a disclosure/footer. Flag only short
    # extracts, or repeated boilerplate occupying a substantial share of text.
    matches = list(_FOOTER.finditer(stripped))
    if matches and (
        len(words) < 80 or sum(m.end() - m.start() for m in matches) > len(stripped) / 2
    ):
        issues.append(
            Issue(
                code="body_footer_only",
                detail="Short or predominantly boilerplate/disclosure extract needs body verification.",
            )
        )
    return issues


def mark_duplicate_bodies(records: list[RecordInput]) -> None:
    """Keep distinct URLs and counts; flag exact text duplicates, not placeholders."""
    groups: dict[str, list[RecordInput]] = {}
    for record in records:
        if missing(record.body) or record.body.strip().casefold() == "video":
            continue
        groups.setdefault(body_hash(record.body), []).append(record)
    for digest, group in groups.items():
        if len(group) < 2:
            continue
        ids = sorted(record.record_id for record in group)
        for record in group:
            record.raw["duplicate_body_record_ids"] = ids
            record.issues.append(
                Issue(
                    code="duplicate_body",
                    severity="info",
                    detail=f"Exact body SHA-256 {digest} occurs at {len(group)} different records; no automatic identity merge.",
                )
            )
