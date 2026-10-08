"""Fresh fictional output contracts; no saved evaluation or provider calls."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from pydantic import ValidationError

from observatory.config import Settings
from observatory.db import digest
from observatory.models import Answer, Evidence, Filters
from observatory.rag import Rag, quote_catalog, selection_schema
from observatory.research_agent import ResearchRun
from observatory.service import Service


def fictional_evidence():
    text = "The museum notice says the gallery is scheduled to reopen in winter."
    return Evidence(evidence_id="museum-e1", record_id="museum-record", version_id="museum-v1",
                    dataset="native", title="Museum notice", text=text, start=0, end=len(text))


def selected_payload():
    return {
        "status": "answered",
        "claims": [{"passage_id": "Q1", "text": "The museum notice schedules reopening in winter."}],
        "summary": [{"text": "The notice schedules reopening in winter.", "citation_indices": [1]}],
        "sections": [{"title": "Reopening", "citation_indices": [1]}],
    }


@pytest.mark.parametrize("field,maximum", [("claims", 6), ("summary", 3), ("sections", 6)])
def test_provider_schema_enforces_the_same_cardinality_ceiling_as_validation(field, maximum):
    schema = selection_schema(quote_catalog([fictional_evidence()]))
    payload = selected_payload()
    payload[field] = [deepcopy(payload[field][0]) for _ in range(maximum + 1)]
    with pytest.raises(ValidationError):
        schema.model_validate(payload)
    assert schema.model_json_schema()["properties"][field]["maxItems"] == maximum


def generation_service(payload, *, response_status="completed", parsed_available=True):
    evidence = fictional_evidence()
    connection = MagicMock()
    db = SimpleNamespace(
        validate_evidence=lambda _: True, connect=lambda: connection,
        health=lambda: {"status": "ok", "data_version": "museum-version"},
        public_rows=lambda _: [], search=lambda *args, **kwargs: [evidence],
        save_answer=Mock(),
    )

    def respond(**request):
        return SimpleNamespace(
            status=response_status, model="offline-fictional",
            output_parsed=request["text_format"].model_validate(payload) if parsed_available else None,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                  input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
                                  model_dump=lambda: {"input_tokens": 10, "output_tokens": 5}),
        )

    parse = Mock(side_effect=respond)
    rag = Rag(db, Settings(), client=SimpleNamespace(responses=SimpleNamespace(parse=parse)))
    rag.budget = SimpleNamespace(reserve=Mock(return_value="museum-reservation"), settle=Mock(),
                                uncertain=Mock(), cancel_unsent=Mock(), reservation_cost=lambda _: 0.001)
    rag.embed = Mock(return_value=[[0.0]])
    service = Service(Settings(), db=db, rag=rag)
    service._search_external = Mock(side_effect=AssertionError("An invalid answer must not search the web"))
    return service, parse, connection


@pytest.mark.parametrize("fault,reason,stage", [
    ("missing_structure", "answer_structure_incomplete", "selection_materialization"),
    ("repeated_claim_group", "answer_section_coverage_mismatch", "answer_validation"),
    ("repeated_heading", "answer_section_heading_invalid", "answer_validation"),
    ("self_support", "passage_support_invalid", "selection_materialization"),
])
def test_completed_parsed_output_failure_retains_safe_stage_and_specific_reason(fault, reason, stage):
    payload = selected_payload()
    if fault == "missing_structure":
        payload["summary"] = []
    elif fault == "repeated_claim_group":
        payload["sections"].append({"title": "Opening schedule", "citation_indices": [1]})
    elif fault == "repeated_heading":
        payload["sections"].append(deepcopy(payload["sections"][0]))
    else:
        payload["claims"][0]["support_passage_ids"] = ["Q1"]
    service, parse, connection = generation_service(payload)
    answer = service._answer_evidence("What schedule does the museum notice give?", Filters(), "offline", audit=False)
    assert answer.status == "service_unavailable" and answer.failure_reason == reason
    assert answer.research_trace["generation_failure"] == {
        "stage": stage, "reason": reason, "provider_status": "completed",
        "parsed_output_available": True, "usage_settled": True, "error_type": "ValueError",
    }
    assert not answer.citations and not answer.summary
    assert answer.evidence == [fictional_evidence()]
    assert parse.call_count == service.rag.budget.settle.call_count == 1
    service._search_external.assert_not_called()
    service.rag.budget.cancel_unsent.assert_not_called()
    saved = connection.__enter__.return_value.execute.call_args.args[1]
    assert saved[2].obj == selection_schema(quote_catalog([fictional_evidence()])).model_validate(payload).model_dump()
    assert saved[3] == "completed"


@pytest.mark.parametrize("response_status,parsed_available", [("incomplete", True), ("completed", False)])
def test_incomplete_provider_result_is_distinguished_from_local_validation(response_status, parsed_available):
    service, parse, _ = generation_service(selected_payload(), response_status=response_status,
                                           parsed_available=parsed_available)
    answer = service._answer_evidence("What schedule does the museum notice give?", Filters(), "offline", audit=False)
    assert answer.status == "service_unavailable"
    assert answer.failure_reason == "generation_response_incomplete"
    assert answer.research_trace["generation_failure"]["stage"] == "provider_output"
    assert answer.research_trace["generation_failure"]["provider_status"] == response_status
    assert answer.research_trace["generation_failure"]["parsed_output_available"] is parsed_available
    assert parse.call_count == 1 and not answer.citations


def web_service():
    service = Service.__new__(Service)
    service.health = lambda: {"status": "ok", "data_version": "museum-version"}
    service._search_external = Mock(return_value={
        "status": "ok", "source_kind": "external_web", "cost_usd": 0.002,
        "summary": "A separate visitor page gives opening information.",
        "sources": [{"source_id": "W1", "title": "Visitor page", "url": "https://museum.example/visiting"}],
    })
    return service


@pytest.mark.parametrize("partial", [False, True])
def test_external_delivery_keeps_collection_completion_and_provenance_explicit(partial):
    service = web_service()
    structured = {"kind": "original_metadata", "complete": False, "missing_fields": ["opening_date"]} if partial else None
    original = Answer(status="answered" if partial else "insufficient_evidence", answer_mode="tools",
                      answer="The preserved notice has no recorded opening date.",
                      failure_reason="original_source_metadata_missing", structured_result=structured)
    before = original.model_dump()
    result = service._ensure_answer("Read the stored opening date.", Filters(), "offline", original)
    assert result.status == "answered" and result.answer_mode == "web_supplement"
    assert result.failure_reason == before["failure_reason"]
    assert result.structured_result == before["structured_result"]
    assert result.research_trace["collection_completion"] == {
        "complete": False, "status": "incomplete", "answer_status": before["status"],
        "answer_mode": "tools", "failure_reason": before["failure_reason"],
    }
    assert result.research_trace["answer_provenance"] == {
        "source_kind": "external_web", "role": "supplement",
        "status_meaning": "response_available", "collection_task_completed": False,
    }
    assert result.external_research["collection_answer"]["complete"] is False
    assert "collection task remains incomplete" in result.answer.lower()
    assert service._search_external.call_count == 1
    assert service._ensure_answer("Read the stored opening date.", Filters(), "offline", result) is result
    assert service._search_external.call_count == 1


def test_valid_boundary_sized_answer_keeps_exact_quotes_and_schema_identity():
    payload = selected_payload()
    payload["claims"] = [deepcopy(payload["claims"][0]) for _ in range(6)]
    payload["summary"] = [{"text": "The notice schedules reopening in winter.", "citation_indices": [i, i + 1]}
                          for i in (1, 3, 5)]
    payload["sections"] = [{"title": f"Schedule point {i}", "citation_indices": [i]} for i in range(1, 7)]
    service, parse, _ = generation_service(payload)
    answer = service._answer_evidence("What schedule does the museum notice give?", Filters(), "offline", audit=False)
    assert answer.status == "answered" and not answer.failure_reason
    assert len(answer.cited_claims) == 6 and len(answer.summary) == 3 and len(answer.sections) == 6
    assert [citation.quote for citation in answer.citations] == [fictional_evidence().text]
    assert "generation_failure" not in answer.research_trace
    current_schema = parse.call_args.kwargs["text_format"].model_json_schema()
    recorded = service.rag.budget.settle.call_args.args[2]["observatory_request"]
    assert recorded["schema_sha256"] == digest(json.dumps(current_schema, sort_keys=True))
    old_schema = deepcopy(current_schema)
    for field in ("claims", "summary", "sections"):
        old_schema["properties"][field].pop("maxItems")
    assert recorded["schema_sha256"] != digest(json.dumps(old_schema, sort_keys=True))
    assert parse.call_count == 1


def test_generation_failure_survives_tool_route_audit_merge():
    payload = selected_payload()
    payload["summary"] = []
    service, parse, _ = generation_service(payload)
    service.settings.research_agent_enabled = True
    filters = Filters(include_inferred_dates=True)
    service.research_agent = SimpleNamespace(run=lambda *args, **kwargs: ResearchRun(
        route="evidence", result={"filters": filters.model_dump(mode="json")},
        tool_trace=[{"tool": "search_records", "status": "ok"}], cost_usd=0.002,
    ))
    answer = service.answer("What schedule does the museum notice give?", filters, "offline")
    assert answer.status == "service_unavailable" and answer.failure_reason == "answer_structure_incomplete"
    assert answer.research_trace["generation_failure"]["stage"] == "selection_materialization"
    assert answer.research_trace["tools"] == [{"tool": "search_records", "status": "ok"}]
    assert answer.cost_usd == pytest.approx(0.003)
    assert parse.call_count == 1
    service._search_external.assert_not_called()
    assert service.db.save_answer.call_args.args[2] is answer


@pytest.mark.parametrize("operation", ["materialize_selections", "validate_answer"])
def test_unknown_validator_error_never_leaks_raw_diagnostics(monkeypatch, operation):
    canary = "synthetic_private_detail_should_not_be_returned"
    monkeypatch.setattr("observatory.rag." + operation, Mock(side_effect=ValueError(canary)))
    service, parse, _ = generation_service(selected_payload())
    answer = service._answer_evidence("What schedule does the museum notice give?", Filters(), "offline", audit=False)
    assert answer.failure_reason == "answer_validation_failed" and answer.status == "service_unavailable"
    assert canary not in answer.model_dump_json()
    assert answer.research_trace["generation_failure"]["error_type"] == "ValueError"
    assert parse.call_count == 1 and not answer.citations
