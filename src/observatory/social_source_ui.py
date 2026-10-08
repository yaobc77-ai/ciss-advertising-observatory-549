"""Display saved source observations without treating differing captures as one post text."""

from copy import deepcopy

from dash import html
from pydantic import BaseModel, ValidationError

from .models import Citation, Evidence

_IDENTITIES = ("source_observation_id", "source_version_id", "source_body_hash")
_CONFLICT_LABELS = {
    "body": "post text", "sponsor": "company affiliation", "account": "account or channel name",
    "published_at": "publication date", "platform": "platform", "historical_labels": "historical labels",
}
_QUALITY_NOTES = {
    "body_footer_only": "The saved text includes terms or footer-style wording; it remains a source observation.",
    "body_short": "The saved text is short; completeness of the post or thread is not established.",
    "body_garbled": "Some saved characters require source review.",
    "body_truncated_suspected": "The saved text may be truncated; missing content has not been reconstructed.",
    "body_source_partial": "The saved text is a partial capture.",
    "body_source_completeness_unestablished": "The original source's complete contents have not been established.",
    "social_sentence_source_validation_failed": "Source characters are preserved with a segmentation fallback; sentence boundaries are not verified.",
}


def _mapping(value):
    if isinstance(value, BaseModel):
        return value.model_dump()
    return value if isinstance(value, dict) else {}


def _checked_observation(item):
    if not any(item.get(field) is not None for field in _IDENTITIES):
        return None
    try:
        return Evidence.model_validate(item)
    except (ValidationError, TypeError, ValueError):
        return None


def original_text_citation_matches(citation, item):
    """Match exact observation identities as well as the existing quote and evidence ID."""
    citation, item = _mapping(citation), _mapping(item)
    quote = citation.get("quote")
    if (citation.get("evidence_id") != item.get("evidence_id")
            or citation.get("evidence_type", "article_text") != "article_text"
            or citation.get("origin", "original_text") != "original_text"
            or not isinstance(quote, str) or not quote.strip()
            or quote not in str(item.get("text") or "")):
        return False
    if any(citation.get(field) is not None or item.get(field) is not None for field in _IDENTITIES):
        source = _checked_observation(item)
        if source is None:
            return False
        try:
            checked = Citation.model_validate(citation)
        except (ValidationError, TypeError, ValueError):
            return False
        return all(getattr(source, field) == getattr(checked, field) for field in _IDENTITIES)
    return True


def source_observation_notice(item):
    """Plain visible source attribution and review limits; hashes remain in technical details."""
    item = _mapping(item)
    if not any(item.get(field) is not None for field in _IDENTITIES):
        return []
    source = _checked_observation(item)
    if source is None:
        return [html.P("Saved source observation metadata is incomplete or invalid; its numbered citation is unavailable.", className="scope-note")]
    notice = [
        html.P(f"Saved source observation · {source.source_observation_id}", className="scope-note"),
        html.P(f"This is one of {source.source_observation_count} preserved source observations of the same counted post.", className="scope-note"),
        html.P("The quotation comes from this saved observation, including any captured preview or extra text. Complete post or thread contents and paid advertising status are not verified.", className="scope-note"),
    ]
    if source.source_conflicts:
        notice.append(html.P(
            "Unresolved source differences: " + ", ".join(_CONFLICT_LABELS[code] for code in source.source_conflicts)
            + ". The observations have not been adjudicated.", className="scope-note",
        ))
    for code in source.source_quality_codes:
        note = _QUALITY_NOTES.get(code, "Source review flag: " + code.replace("_", " ") + ".")
        notice.append(html.P("Source quality: " + note, className="scope-note"))
    return notice


def source_observation_technical_details(item):
    source = _checked_observation(_mapping(item))
    if source is None:
        return []
    return [
        html.Span(f"Source observation {source.source_observation_id}"),
        html.Span(f"Source observation version {source.source_version_id}"),
        html.Span(f"Source body SHA-256 {source.source_body_hash}"),
        html.Span(f"Observation character range {source.start}–{source.end}; offsets are within this observation's saved text"),
    ]


def _is_primary_quote(child):
    return isinstance(child, html.Blockquote) or (
        isinstance(child, html.Div) and getattr(child, "className", None) == "citation-quote"
    )


def decorate_source_observation_cards(cards, evidence, citations=()):
    """Hook after the existing card renderer; ordinary native cards remain untouched.

    Numbered quote blocks are rebuilt from exact observation matches, retaining
    their global answer numbers. Caller-owned Dash components are not mutated.
    """
    output = []
    citations = [_mapping(value) for value in citations]
    for card, value in zip(cards, evidence, strict=True):
        item = _mapping(value)
        has_observation = any(item.get(field) is not None for field in _IDENTITIES)
        has_observation_citation = any(
            citation.get("evidence_id") == item.get("evidence_id")
            and any(citation.get(field) is not None for field in _IDENTITIES) for citation in citations
        )
        if not has_observation and not has_observation_citation:
            output.append(card)
            continue
        card = deepcopy(card)
        children = list(card.children)
        insertion = next((index for index, child in enumerate(children) if _is_primary_quote(child)), len(children))
        children = [child for child in children if not _is_primary_quote(child)]
        supported = [
            (number, citation["quote"]) for number, citation in enumerate(citations, 1)
            if original_text_citation_matches(citation, item)
        ]
        blocks = [html.Div([
            html.Span(f"Citation [{number}]", className="evidence-code"), html.Blockquote(quote),
        ], className="citation-quote", id=f"answer-citation-{number}") for number, quote in supported]
        if not blocks:
            text = str(item.get("text") or "")
            blocks = [html.Blockquote(text[:520] + ("…" if len(text) > 520 else ""))]
        children[insertion:insertion] = [*source_observation_notice(item), *blocks]
        details = source_observation_technical_details(item)
        for child in children:
            if not isinstance(child, html.Details):
                continue
            detail_children = list(child.children)
            if not any(isinstance(node, html.Summary) and node.children == "Technical details" for node in detail_children):
                continue
            for node in detail_children:
                if isinstance(node, html.Div) and getattr(node, "className", None) == "record-reference":
                    node.children = [*node.children, *details]
        card.children = children
        output.append(card)
    return output
