"""Fixed source-quality markers; no inference that unmarked text is complete."""

from typing import Literal

TextQualityCode = Literal[
    "body_truncated_suspected", "body_source_partial", "body_partial_recovery",
    "body_short", "body_empty", "body_footer_only", "body_garbled", "body_related_navigation",
]
TEXT_QUALITY_CODES = frozenset(TextQualityCode.__args__)


def current_text_quality_codes(issues):
    if not isinstance(issues, list):
        return []
    codes = set()
    for item in issues:
        if not isinstance(item, dict) or item.get("code") not in TEXT_QUALITY_CODES:
            continue
        # The recovery adapter explicitly retains this historical diagnostic.
        # It is not a diagnosis of the newly stored body, regardless of severity.
        detail = item.get("detail")
        if isinstance(detail, str) and detail.startswith("Previous CSV body:"):
            continue
        codes.add(item["code"])
    return sorted(codes)


def text_quality_context(codes):
    return {"codes": list(codes), "complete_article_verified": False,
            "meaning": "Stored source-version quality markers, not a semantic correctness score. "
                       "Suspected truncation remains suspected; partial recovery is partial. "
                       "No marker does not establish a complete article."}
