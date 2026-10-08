"""Fresh synthetic refusal-policy contracts; scripted outputs are not model evaluation."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from observatory.config import Settings
from observatory.models import Evidence
from observatory.prompts import ANSWER_SECTIONS, ANSWER_SYSTEM
from observatory.rag import (
    Rag,
    materialize_selections,
    quote_catalog,
    selection_schema,
    validate_answer,
)

# These invented cases cover unrelated topics and distinct evidence limitations.
# They do not come from customer validation questions or previous per-question tests.
CASES = (
    {
        "id": "roof_schedule_unestimated_output",
        "question": "What completion month and annual output does the school report?",
        "body": "The school report forecasts roof completion in July. Annual electricity output has not been estimated.",
        "claims": [
            "The school report forecasts roof completion in July.",
            "The school report says annual electricity output has not been estimated.",
        ],
        "summary": "The report forecasts completion in July and explicitly leaves annual electricity output unestimated.",
        "quality": [],
    },
    {
        "id": "valve_condition_and_unset_date",
        "question": "Under what condition can the valve return to service, and when?",
        "body": "The engineering note says the valve can return to service only after an independent pressure test passes. The return date has not been set.",
        "claims": [
            "The engineering note requires a passed independent pressure test before the valve returns to service.",
            "The engineering note says the return date has not been set.",
        ],
        "summary": "The note gives a pressure-test condition but says that the return date has not been set.",
        "quality": [],
    },
    {
        "id": "survey_explicit_local_limit",
        "question": "Which areas have been examined in the survey?",
        "body": "The survey report says the eastern trench has been examined. The western trench has not yet been examined.",
        "claims": [
            "The survey report says the eastern trench has been examined.",
            "The survey report says the western trench has not yet been examined.",
        ],
        "summary": "The report distinguishes an examined eastern trench from a western trench that has not yet been examined.",
        "quality": ["body_source_partial"],
    },
    {
        "id": "partial_capture_supported_local_action",
        "question": "What action is described in the saved interview excerpt?",
        "body": "In the interview, Dr. Rao describes a planned classroom trial of reusable containers.",
        "claims": [
            "The saved interview excerpt attributes a planned classroom trial of reusable containers to Dr. Rao.",
        ],
        "summary": "The saved excerpt describes Dr. Rao's planned classroom trial of reusable containers.",
        "quality": ["body_truncated_suspected"],
    },
    {
        "id": "explicit_uncertainty_itself_responsive",
        "question": "What does the memo establish about the project's funding status?",
        "body": "The committee memo states that the project's funding status remains unknown.",
        "claims": [
            "The committee memo explicitly states that the project's funding status remains unknown.",
        ],
        "summary": "The memo establishes uncertainty about funding rather than confirming approval or rejection.",
        "quality": [],
    },
    {
        "id": "irrelevant_nonempty_material",
        "question": "What pesticide levels were measured in the orchard?",
        "body": "The harbor bulletin lists this week's dock inspection rota.",
        "claims": [],
        "summary": "",
        "quality": [],
    },
    {
        "id": "topic_overlap_missing_requested_relation",
        "question": "What annual water savings did the tower achieve?",
        "body": "The tower brochure mentions water-saving pipes alongside a diagram of proposed equipment.",
        "claims": [],
        "summary": "",
        "quality": [],
    },
    {
        "id": "unresolved_actor_without_supporting_context",
        "question": "Which organization performed the completed trial?",
        "body": "The quoted speaker says, 'We performed the trial.' The excerpt does not identify who 'we' refers to.",
        "claims": [],
        "summary": "",
        "quality": ["body_source_partial"],
    },
)


def evidence(case):
    body = case["body"]
    return Evidence(
        evidence_id="synthetic-evidence", record_id="synthetic-record",
        version_id="synthetic-version", dataset="native",
        title="Synthetic source " + case["id"], text=body,
        start=40, end=40 + len(body), source_text_quality_codes=case["quality"],
    )


def selected(case):
    answered = bool(case["claims"])
    refs = list(range(1, len(case["claims"]) + 1))
    return {
        "status": "answered" if answered else "insufficient_evidence",
        "claims": [{"passage_id": "Q1", "support_passage_ids": [], "text": text}
                   for text in case["claims"]],
        "summary": [{"text": case["summary"], "citation_indices": refs}] if answered else [],
        "sections": [{"title": "Saved source", "citation_indices": refs}] if answered else [],
    }


@pytest.mark.parametrize("marker", [
    "Judge each requested part separately",
    "A missing detail must not erase other supported requested information",
    "An explicit source statement of uncertainty",
    "nonempty evidence or shared topic alone is not enough",
    'Do not convert "not supplied" into "does not exist"',
])
def test_answer_policy_has_distinct_bounded_answer_and_refusal_rules(marker):
    missing = dict(ANSWER_SECTIONS)["Missing information"]
    assert marker in missing


def test_schema_status_describes_bounded_answer_instead_of_full_completion():
    schema = selection_schema(quote_catalog([evidence(CASES[0])])).model_json_schema()
    description = schema["properties"]["status"].get("description", "")
    assert "at least one requested part" in description
    assert "not a certification" in description
    assert "shared topic" in description


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_source_supported_bounded_outputs_and_correct_refusals_keep_their_state(case):
    ev = evidence(case)
    before = ev.model_dump()
    catalog = quote_catalog([ev])
    parsed = selection_schema(catalog).model_validate(selected(case))
    answer = validate_answer(materialize_selections(parsed, catalog), [ev])
    assert answer.status == ("answered" if case["claims"] else "insufficient_evidence")
    assert ev.model_dump() == before
    if case["claims"]:
        assert [claim.text for claim in answer.cited_claims] == case["claims"]
        assert all(citation.quote in ev.text for citation in answer.citations)
        assert answer.summary[0].text == case["summary"]
        assert answer.evidence[0].source_text_quality_codes == case["quality"]
    else:
        assert not answer.citations and not answer.cited_claims and not answer.summary


@pytest.mark.parametrize("field", ["claims", "summary", "sections"])
def test_refusal_cannot_hide_a_scripted_bounded_answer(field):
    ev = evidence(CASES[0])
    payload = selected(CASES[0])
    payload["status"] = "insufficient_evidence"
    for omitted in {"claims", "summary", "sections"} - {field}:
        payload[omitted] = []
    parsed = selection_schema(quote_catalog([ev])).model_validate(payload)
    with pytest.raises(ValueError, match="Contradictory answer status"):
        materialize_selections(parsed, quote_catalog([ev]))


def test_answered_without_any_supported_statement_is_still_rejected():
    ev = evidence(CASES[5])
    payload = selected(CASES[5])
    payload["status"] = "answered"
    parsed = selection_schema(quote_catalog([ev])).model_validate(payload)
    with pytest.raises(ValueError):
        materialize_selections(parsed, quote_catalog([ev]))


def test_production_request_contains_sources_not_scripted_answers_and_has_no_retry():
    case = CASES[0]
    ev = evidence(case)
    connection = MagicMock()
    seen = []

    def respond(**request):
        seen.append(request)
        payload = json.loads(request["input"][1]["content"])
        assert payload["question"] == case["question"]
        assert payload["evidence"][0]["quote_catalog"] == {"Q1": case["body"]}
        assert payload["retrieval_scope"]["complete_matching_list"] is False
        assert not {"expected", "answer", "reference", "reference_answers"} & payload.keys()
        return SimpleNamespace(
            status="completed", model="offline-scripted",
            output_parsed=request["text_format"].model_validate(selected(case)),
            usage=SimpleNamespace(
                input_tokens=10, output_tokens=5,
                input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
                model_dump=lambda: {"input_tokens": 10, "output_tokens": 5},
            ),
        )

    client = SimpleNamespace(responses=SimpleNamespace(parse=Mock(side_effect=respond)))
    rag = Rag(SimpleNamespace(validate_evidence=lambda e: True, connect=lambda: connection),
              Settings(), client=client)
    rag.budget = SimpleNamespace(settle=Mock(), uncertain=Mock())
    answer = rag.generate(case["question"], [ev], "synthetic", "reserved")
    assert answer.status == "answered"
    assert len(seen) == client.responses.parse.call_count == 1
    assert ANSWER_SYSTEM in seen[0]["input"][0]["content"]
    rag.budget.settle.assert_called_once()
    rag.budget.uncertain.assert_not_called()


def test_no_source_does_not_dispatch_or_manufacture_a_bounded_answer():
    client = SimpleNamespace(responses=SimpleNamespace(parse=Mock()))
    rag = Rag(object(), Settings(), client=client)
    rag.budget = SimpleNamespace(cancel_unsent=Mock())
    answer = rag.generate("What launch date is established?", [], "synthetic", "reserved")
    assert answer.status == "insufficient_evidence" and not answer.citations
    client.responses.parse.assert_not_called()
    rag.budget.cancel_unsent.assert_called_once_with("reserved")
