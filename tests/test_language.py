"""Offline language regressions; these checks do not judge factual support."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from lingua import Language

import observatory.language as language_module
from observatory.language import check_claim_languages, language_hint
from observatory.models import Evidence
from observatory.rag import GroundedClaim, ModelAnswer, validate_answer

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "language_regressions.json").read_text(
        encoding="utf-8"
    )
)
ENGLISH_QUESTION = (
    "Please explain the proposed carbon capture project and its stated limitations."
)
CHINESE_QUESTION = "请说明这篇广告如何描述项目计划，以及它对成本和减排效果提出了哪些限制。"
ENGLISH_CLAIM = (
    "The advertisement describes a proposed carbon capture project and explains "
    "that its costs and actual emissions reductions remain uncertain."
)
CHINESE_CLAIM = (
    "这篇广告描述了一项仍在规划中的碳捕集项目，"
    "并指出项目的建设成本和实际减排效果仍然存在不确定性。"
)
FRENCH_CLAIM = (
    "Selon cette publicité, le projet de captage du carbone reste à l'étude "
    "et ses coûts de construction sont encore incertains."
)
ENGLISH_QUOTE = (
    "The advertisement describes a proposed carbon capture project. "
    "Its costs and actual emissions reductions remain uncertain."
)
CHINESE_QUOTE = "广告描述了一项仍在规划中的碳捕集项目，并指出建设成本和实际减排效果仍存在不确定性。"


def evidence(text):
    return Evidence(
        evidence_id="language-evidence",
        record_id="selected-record",
        version_id="preserved-version",
        dataset="native",
        title="Synthetic language-only fixture",
        text=text,
        start=117,
        end=117 + len(text),
        url="",  # Preserve an already applied source-link visibility setting.
    )


def model_answer(text, ev):
    return ModelAnswer(
        status="answered",
        claims=[GroundedClaim(text=text, evidence_id=ev.evidence_id, quote=ev.text)],
    )


def historical_claims(case):
    """Remove only the displayed citation suffix; preserve the original fixture."""
    blocks = case["answer"].split("\n\n")
    assert len(blocks) == len(case["citations"])
    claims = []
    for number, (block, citation) in enumerate(zip(blocks, case["citations"]), 1):
        suffix = f" [{number}]"
        assert block.endswith(suffix)
        claims.append(GroundedClaim(text=block[: -len(suffix)], **citation))
    return claims


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_recorded_wrong_language_is_blocked_without_changing_cost_or_evidence(case):
    # These are three specific observed failures, not an estimated error rate.
    assert re.fullmatch(r"[0-9a-f]{64}", FIXTURE["source_sha256"])
    target = language_hint(case["question"])
    assert target["code"] == case["expected_question_language"] == "en"
    assert language_hint(case["answer"])["code"] == case["observed_answer_language"]
    parsed = ModelAnswer(status="answered", claims=historical_claims(case))
    before = parsed.model_dump()
    selected = [Evidence.model_validate(e) for e in case["cited_evidence"]]
    evidence_before = [e.model_dump() for e in selected]

    result = validate_answer(
        parsed, selected, case["reported_answer_cost_usd"], target=target
    )

    assert result.status == "service_unavailable"
    assert result.failure_reason == "answer_language_mismatch"
    assert result.language_check["status"] == "mismatch"
    assert result.cost_usd == case["reported_answer_cost_usd"]
    assert result.citations == []
    assert [e.model_dump() for e in result.evidence] == evidence_before
    assert parsed.model_dump() == before  # The unmodified generation stays auditable.
    assert all(c.text not in result.answer for c in parsed.claims)


@pytest.mark.parametrize(
    "question,claim,quote,expected",
    [
        (ENGLISH_QUESTION, ENGLISH_CLAIM, ENGLISH_QUOTE, "en"),
        (CHINESE_QUESTION, CHINESE_CLAIM, ENGLISH_QUOTE, "zh"),
        (ENGLISH_QUESTION, ENGLISH_CLAIM, CHINESE_QUOTE, "en"),
        (CHINESE_QUESTION, CHINESE_CLAIM, CHINESE_QUOTE, "zh"),
    ],
    ids=["en-en", "zh-en-source", "en-zh-source", "zh-zh"],
)
def test_only_generated_claim_language_is_checked_and_source_quote_is_unchanged(
    question, claim, quote, expected
):
    target = language_hint(question)
    assert target["code"] == expected
    ev = evidence(quote)
    before = ev.model_dump()

    result = validate_answer(model_answer(claim, ev), [ev], 0.0123, target=target)

    assert result.status == "answered"
    assert result.language_check["status"] == "match"
    assert result.citations[0].quote == quote
    assert result.evidence[0].model_dump() == before
    assert result.cost_usd == 0.0123


def test_one_french_claim_is_not_hidden_by_five_english_claims():
    audit = check_claim_languages(
        [ENGLISH_CLAIM] * 5 + [FRENCH_CLAIM], language_hint(ENGLISH_QUESTION)
    )
    assert all(hint["code"] == "en" for hint in audit["claims"][:5])
    assert audit["claims"][-1]["code"] == "fr"
    assert audit["status"] == "mismatch"


def test_foreign_claim_blocks_even_when_the_combined_hint_matches(monkeypatch):
    target = language_hint(ENGLISH_QUESTION)

    def controlled_hint(text):
        code = "fr" if text == FRENCH_CLAIM else "en"
        return {"code": code, "name": code, "letters": 100, "margin": 0.8}

    monkeypatch.setattr(language_module, "language_hint", controlled_hint)
    audit = check_claim_languages([ENGLISH_CLAIM, FRENCH_CLAIM], target)
    assert audit["combined"]["code"] == "en"
    assert audit["status"] == "mismatch"


@pytest.mark.parametrize("text", ["CCS?", "2025 20% CO2", "为什么？", "用中文解释", "", "  "])
def test_short_acronyms_numbers_and_chinese_do_not_force_a_language(text, monkeypatch):
    def detector_must_not_run():
        raise AssertionError("A short input must stay inconclusive before detection")

    monkeypatch.setattr(language_module, "detector", detector_must_not_run)
    hint = language_hint(text)
    assert hint["code"] is None and hint["margin"] is None
    assert hint["letters"] == sum(c.isalpha() for c in text)


def test_unicode_letters_are_counted_without_using_utf8_byte_length():
    text = "请说明这篇广告如何描述项目计划以及成本和减排效果的限制"
    assert len(text) >= 20
    hint = language_hint(text)
    assert hint["letters"] == len(text)
    assert hint["code"] == "zh"


def test_low_margin_is_inconclusive_even_when_english_is_top_ranked(monkeypatch):
    # Synthetic detector outputs exercise uncertainty handling, not accuracy.
    monkeypatch.setattr(
        language_module,
        "detector",
        lambda: SimpleNamespace(
            compute_language_confidence_values=lambda _: [
                SimpleNamespace(language=Language.ENGLISH, value=0.54),
                SimpleNamespace(language=Language.FRENCH, value=0.46),
            ]
        ),
    )
    hint = language_hint(ENGLISH_CLAIM)
    assert hint["code"] is None and hint["name"] is None
    assert hint["margin"] == pytest.approx(0.08)
    audit = check_claim_languages(
        [ENGLISH_CLAIM], {"code": "en", "name": "English"}
    )
    assert audit["status"] == "inconclusive"


def test_english_proper_names_do_not_cause_a_false_language_rejection():
    text = (
        "According to Patrick Pouyanné, TotalEnergies proposes a carbon capture "
        "project at Lacq."
    )
    # This detector version finds the short proper-name-heavy sentence ambiguous.
    audit = check_claim_languages([text], language_hint(ENGLISH_QUESTION))
    assert audit["status"] == "inconclusive"
    ev = evidence(ENGLISH_QUOTE)
    result = validate_answer(
        model_answer(text, ev), [ev], target=language_hint(ENGLISH_QUESTION)
    )
    assert result.status == "answered"
    assert result.language_check["status"] == "inconclusive"


def test_chinese_prose_with_english_company_names_keeps_its_language():
    text = (
        "这篇广告介绍了 TotalEnergies 在 Baytown 的 blue hydrogen 项目，"
        "并明确说明它仍处于规划阶段。"
    )
    audit = check_claim_languages([text], language_hint(CHINESE_QUESTION))
    assert audit["status"] == "match"
    assert audit["claims"][0]["code"] == "zh"


@pytest.mark.parametrize("target", [None, language_hint("CCS?"), language_hint("用中文解释")])
def test_unknown_target_remains_inconclusive_instead_of_claiming_validation(target):
    audit = check_claim_languages([FRENCH_CLAIM], target)
    assert audit["status"] == "inconclusive"
    ev = evidence(ENGLISH_QUOTE)
    result = validate_answer(model_answer(FRENCH_CLAIM, ev), [ev], target=target)
    assert result.status == "answered"
    assert result.language_check["status"] == "inconclusive"
    assert result.citations[0].quote == ev.text


def test_short_claim_is_not_certified_by_a_longer_matching_claim():
    audit = check_claim_languages(
        [ENGLISH_CLAIM, "CO2"], language_hint(ENGLISH_QUESTION)
    )
    assert audit["combined"]["code"] == "en"
    assert audit["claims"][1]["code"] is None
    assert audit["status"] == "inconclusive"


def test_model_language_declaration_does_not_override_its_actual_prose():
    ev = evidence(ENGLISH_QUOTE)
    parsed = ModelAnswer.model_validate(
        {
            "status": "answered",
            "language": "en",  # Untrusted extra model metadata is no proof.
            "claims": [
                {"text": FRENCH_CLAIM, "evidence_id": ev.evidence_id, "quote": ev.text}
            ],
        }
    )
    result = validate_answer(parsed, [ev], target=language_hint(ENGLISH_QUESTION))
    assert result.status == "service_unavailable"
    assert result.language_check["status"] == "mismatch"


def test_inconclusive_language_does_not_bypass_citation_validation():
    ev = evidence(ENGLISH_QUOTE)
    parsed = model_answer("CO2", ev)
    parsed.claims[0].quote = "This invented quote is absent from the source."
    with pytest.raises(ValueError, match="Unverifiable citation"):
        validate_answer(parsed, [ev], target=language_hint("CCS?"))
