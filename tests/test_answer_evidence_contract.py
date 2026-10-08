"""New synthetic development inputs; no customer questions or model calls."""

import pytest

from observatory.models import AnswerSection, CitedStatement, Evidence
from observatory.rag import (
    GroundedClaim,
    GroundedQuote,
    ModelAnswer,
    materialize_selections,
    quote_catalog,
    selection_schema,
    validate_answer,
)


def source(identity="e1", text="The library plans to open in June, subject to inspection."):
    return Evidence(evidence_id=identity, record_id="library-" + identity,
                    version_id="version-1", dataset="native", title="Library schedule",
                    text=text, start=0, end=len(text))


def structured(claims):
    refs = list(range(1, len(claims) + 1))
    return ModelAnswer(status="answered", claims=claims,
                       summary=[CitedStatement(text="The notice describes a conditional plan.",
                                               citation_indices=refs)],
                       sections=[AnswerSection(title="Schedule", citation_indices=refs)])


def test_same_exact_source_quote_reuses_number_across_claims_and_sections():
    evidence = source()
    parsed = structured([
        GroundedClaim(evidence_id="e1", quote=evidence.text,
                      text="The notice says the library plans to open in June."),
        GroundedClaim(evidence_id="e1", quote=evidence.text,
                      text="The notice says opening is subject to inspection."),
    ])
    result = validate_answer(parsed, [evidence])
    assert len(result.citations) == 1
    assert [claim.citation_indices for claim in result.cited_claims] == [[1], [1]]
    assert result.summary[0].citation_indices == [1]
    assert result.sections[0].citation_indices == [1]
    assert result.answer.count("[1]") == 2
    assert "[2]" not in result.answer


def test_support_quote_can_be_reused_as_primary_without_duplicate_display():
    evidence = source(text="The museum plans to reopen. Inspection is still pending.")
    first, second = "The museum plans to reopen.", "Inspection is still pending."
    parsed = structured([
        GroundedClaim(evidence_id="e1", quote=first, text="The notice describes a plan.",
                      support_quotes=[GroundedQuote(evidence_id="e1", quote=second)]),
        GroundedClaim(evidence_id="e1", quote=second,
                      text="The notice says inspection remains pending."),
    ])
    result = validate_answer(parsed, [evidence])
    assert len(result.citations) == 2
    assert [claim.citation_indices for claim in result.cited_claims] == [[1, 2], [2]]
    assert result.summary[0].citation_indices == [1, 2]


def test_matching_words_from_different_evidence_keep_separate_provenance():
    evidence = [source("e1"), source("e2")]
    parsed = structured([GroundedClaim(evidence_id=e.evidence_id, quote=e.text,
                                     text="This notice describes a plan.") for e in evidence])
    result = validate_answer(parsed, evidence)
    assert [c.evidence_id for c in result.citations] == ["e1", "e2"]
    assert [c.citation_indices for c in result.cited_claims] == [[1], [2]]


def test_different_quotes_from_one_evidence_are_not_collapsed():
    evidence = source(text="A plan exists. Opening has not occurred.")
    parsed = structured([GroundedClaim(evidence_id="e1", quote=q, text="The notice says " + q)
                         for q in ("A plan exists.", "Opening has not occurred.")])
    result = validate_answer(parsed, [evidence])
    assert [c.quote for c in result.citations] == ["A plan exists.", "Opening has not occurred."]


@pytest.mark.parametrize("content", ["claims", "summary", "sections"])
def test_insufficient_result_cannot_silently_discard_content(content):
    values = {"claims": [GroundedClaim(evidence_id="e1", quote=source().text,
                                     text="The notice contains a plan.")],
              "summary": [CitedStatement(text="There is a plan.", citation_indices=[1])],
              "sections": [AnswerSection(title="Plan", citation_indices=[1])]}
    payload = {"status": "insufficient_evidence", "claims": []}
    payload[content] = values[content]
    parsed = ModelAnswer.model_validate(payload)
    with pytest.raises(ValueError, match="Contradictory answer status"):
        validate_answer(parsed, [source()])


@pytest.mark.parametrize("content", ["claims", "summary", "sections"])
def test_selection_materialization_rejects_contradictory_insufficient_status(content):
    catalog = quote_catalog([source()])
    schema = selection_schema(catalog)
    values = {"claims": [{"passage_id": "Q1", "text": "The notice describes a plan."}],
              "summary": [{"text": "A plan exists.", "citation_indices": [1]}],
              "sections": [{"title": "Plan", "citation_indices": [1]}]}
    payload = {"status": "insufficient_evidence", "claims": [], "summary": [], "sections": []}
    payload[content] = values[content]
    with pytest.raises(ValueError, match="Contradictory answer status"):
        materialize_selections(schema.model_validate(payload), catalog)


def test_empty_insufficient_result_remains_valid():
    result = validate_answer(ModelAnswer(status="insufficient_evidence", claims=[]), [source()])
    assert result.status == "insufficient_evidence" and not result.citations


def test_reused_quote_keeps_exact_whitespace():
    evidence = source(text="The  library\nplans to reopen.")
    parsed = structured([GroundedClaim(evidence_id="e1", quote=evidence.text,
                                     text="The notice describes a plan.") for _ in range(2)])
    result = validate_answer(parsed, [evidence])
    assert len(result.citations) == 1
    assert result.citations[0].quote == evidence.text


def test_invalid_later_quote_still_blocks_answer():
    evidence = source()
    parsed = structured([GroundedClaim(evidence_id="e1", quote=q, text="The notice describes a plan.")
                         for q in (evidence.text, "The library opened last year.")])
    with pytest.raises(ValueError, match="Unverifiable citation"):
        validate_answer(parsed, [evidence])
