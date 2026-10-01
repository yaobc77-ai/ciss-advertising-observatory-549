"""Local language hints and a conservative mismatch guard, not semantic scoring."""

import re
from functools import lru_cache

from lingua import LanguageDetectorBuilder

# Engineering heuristics, not calibrated probabilities. Short names/acronyms can
# receive high detector scores (e.g. CCS -> Hungarian), so they stay inconclusive.
MIN_LETTERS = 20
MIN_MARGIN = 0.20
POLICY_VERSION = "lingua-2.2.0-min20-margin0.20-v4"

# Only exact titles from the selected evidence can be omitted from detection.
# Quotation marks alone do not establish that text came from a source.
_QUOTED = re.compile(r'“[^”]*”|"[^"]*"|«[^»]*»|「[^」]*」|『[^』]*』|‘[^’]*’')


def _prose(text, source_titles):
    unchecked_quotes = []
    ignored = 0

    def replace(match):
        nonlocal ignored
        content = match.group()[1:-1]
        if content in source_titles:
            ignored += 1
            return " "
        unchecked_quotes.append(content)
        return match.group()

    stripped = _QUOTED.sub(replace, text)
    # A title alone cannot establish the language of an answer's prose.
    prose = stripped if sum(c.isalpha() for c in stripped) >= MIN_LETTERS else text
    return prose, unchecked_quotes, ignored


@lru_cache(maxsize=1)
def detector():
    return LanguageDetectorBuilder.from_all_languages().build()


def language_hint(text):
    letters = sum(c.isalpha() for c in text)
    result = {"code": None, "name": None, "letters": letters, "margin": None}
    if letters < MIN_LETTERS:
        return result
    scores = detector().compute_language_confidence_values(text)
    if len(scores) < 2:
        return result
    margin = scores[0].value - scores[1].value
    result["margin"] = round(margin, 6)
    if margin >= MIN_MARGIN:
        result.update(
            code=scores[0].language.iso_code_639_1.name.lower(),
            name=scores[0].language.name.replace("_", " ").title(),
        )
    return result


def check_claim_languages(texts, target, *, source_titles=()):
    """Only pass generated prose here. Source quotes must retain their language."""
    audit = {
        "policy": POLICY_VERSION,
        "target": target,
        "status": "inconclusive",
        "claims": [],
        "combined": None,
        "quoted_prose": [],
        "title_spans_ignored": 0,
    }
    if not target or not target["code"]:
        return audit
    titles = {title for title in source_titles if isinstance(title, str) and title}
    parsed = [_prose(text, titles) for text in texts]
    prose = [item[0] for item in parsed]
    audit["quoted_prose"] = [language_hint(quote) for item in parsed for quote in item[1]]
    audit["title_spans_ignored"] = sum(item[2] for item in parsed)
    audit["claims"] = [language_hint(text) for text in prose]
    audit["combined"] = language_hint("\n".join(prose))
    # Each claim is judged on its own; the joined text only decides when no
    # claim is conclusive. Under v3 six English claims of the form "The
    # advertisement mentions carbon capture..." were joined into text that
    # lingua read as Latin (margin 0.9997), rejecting a correct answer.
    own = [*audit["claims"], *audit["quoted_prose"]]
    combined = audit["combined"]
    if any(h["code"] and h["code"] != target["code"] for h in own):
        audit["status"] = "mismatch"
    elif texts and all(h["code"] == target["code"] for h in own):
        audit["status"] = "match"
    elif not any(h["code"] for h in own) and combined["code"]:
        audit["status"] = "match" if combined["code"] == target["code"] else "mismatch"
    return audit
