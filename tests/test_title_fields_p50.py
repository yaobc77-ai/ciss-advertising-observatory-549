"""Fresh fictional title and requested-field checks; no provider or database calls."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from observatory.models import Filters
from observatory.original_metadata import project_record_metadata
from observatory.research_agent import ResearchAgent
from observatory.research_tools import TOOLS, ToolCatalog
from observatory.service import Service

RECORD = {"record_id": "native:p50-fictional-moonlit-orchard", "dataset": "native",
          "version_id": "f" * 64, "body_hash": "a" * 64}


def metadata(fields, cells=None, *, conflict=False, provenance_review=False):
    cells = cells or {}
    origin = {"origin_kind": "original_metadata", "priority": 0,
              "cells": [{"field": field, "present": True, "value": value,
                         "payload_field_path": ["raw", "metadata", field]}
                        for field, value in cells.items()],
              "provenance": [{"row": 5, "sha256": "b" * 64}]}
    if provenance_review:
        origin["provenance"].append({"row": 6, "sha256": "c" * 64})
    origins = [origin]
    if conflict:
        other = deepcopy(origin)
        other["cells"][0]["value"] = "Different fictional source value"
        origins.append(other)
    return project_record_metadata({**RECORD, "metadata_origins": origins}, fields,
                                   public_url=lambda value: value)


def test_requested_known_fields_are_kept_when_another_requested_field_is_missing():
    data = metadata(["publisher", "publication_date"], {"publisher": "  Fable Journal  "})
    result = Service._tool_metadata_answer(data)
    assert result.status == "answered"
    assert result.failure_reason == "original_source_metadata_missing"
    assert "  Fable Journal  " in result.answer
    assert result.structured_result["complete"] is False
    assert result.structured_result["requested_fields"] == ["publisher", "publication_date"]
    assert result.structured_result["known_fields"] == ["publisher"]
    assert result.structured_result["missing_fields"] == ["publication_date"]
    assert result.structured_result["review_fields"] == []
    assert result.structured_result["record"] == RECORD
    assert result.structured_result["source_refs"] == data["source_refs"]


def test_unrequested_missing_location_does_not_block_recorded_wording():
    data = metadata(["disclosure_language"], {"disclosure_language": "  Funded collaboration  "})
    result = Service._tool_metadata_answer(data)
    assert result.status == "answered" and not result.failure_reason
    assert result.structured_result["complete"] is True
    assert result.structured_result["known_fields"] == ["disclosure_language"]
    assert "disclosure_location" not in result.structured_result["requested_fields"]


@pytest.mark.parametrize("value", [None, "", "unknown", "n/a"])
def test_all_requested_unknown_fields_stay_insufficient(value):
    result = Service._tool_metadata_answer(metadata(["publication_date"], {"publication_date": value}))
    assert result.status == "insufficient_evidence"
    assert result.structured_result["complete"] is False
    assert result.structured_result["known_fields"] == []
    assert result.structured_result["missing_fields"] == ["publication_date"]


@pytest.mark.parametrize("conflict,provenance_review", [(True, False), (False, True)])
def test_conflicting_field_or_provenance_is_review_only(conflict, provenance_review):
    data = metadata(["publisher"], {"publisher": "Fable Journal"},
                    conflict=conflict, provenance_review=provenance_review)
    result = Service._tool_metadata_answer(data)
    assert result.status == "insufficient_evidence"
    assert result.structured_result["complete"] is False
    assert result.structured_result["known_fields"] == []
    assert result.structured_result["review_fields"] == ["publisher"]
    assert "Fable Journal" not in result.answer


@pytest.mark.parametrize("status", ["source_links_hidden", "unsafe_url_withheld", "unsupported_value", "exceeds_limit"])
def test_unavailable_value_cannot_be_promoted_to_known(status):
    data = {"status": "ok", "record": RECORD,
            "original_fields": {"original_url": {"status": status, "value": "Unusable source value"}}}
    result = Service._tool_metadata_answer(data)
    assert result.status == "insufficient_evidence"
    assert result.structured_result["known_fields"] == []
    assert result.structured_result["missing_fields"] == ["original_url"]
    assert "Unusable source value" not in result.answer


def test_empty_original_metadata_remains_incomplete():
    result = Service._tool_metadata_answer({"record": RECORD, "original_fields": {}})
    assert result.status == "insufficient_evidence"
    assert result.structured_result["complete"] is False


def test_partial_metadata_cannot_complete_a_finished_composite_plan():
    partial = metadata(["publisher", "publication_date"], {"publisher": "Fable Journal"})
    complete = metadata(["collection_search_term"], {"collection_search_term": "orchard lighting"})
    data = {"complete": True, "pending_parts": [], "parts": [
        {"question_part": "Read its stored publisher and date", "dataset": "native", "route": "metadata", "result": partial},
        {"question_part": "Read its stored collection term", "dataset": "native", "route": "metadata", "result": complete},
    ]}
    result = object.__new__(Service)._tool_composite_answer(data, Filters())
    assert result.status == "insufficient_evidence" and result.failure_reason == "research_plan_incomplete"
    assert result.structured_result["complete"] is False
    assert result.structured_result["pending_parts"] == []
    assert result.structured_result["incomplete_parts"] == [{
        "question_part": "Read its stored publisher and date", "dataset": "native", "route": "metadata",
        "failure_reason": "original_source_metadata_missing"}]
    assert result.structured_result["parts"][0]["answer"]["status"] == "answered"
    assert "Fable Journal" in result.answer and "orchard lighting" in result.answer


class ScriptedTitleAgent(ResearchAgent):
    def __init__(self, catalog, calls):
        super().__init__(SimpleNamespace(), catalog, entity_context={})
        self.calls = calls
        self.advertised = []

    def _dispatch(self, inputs, definitions, visitor, step):
        self.advertised.append({definition["name"] for definition in definitions})
        name, arguments = self.calls[step - 1]
        return SimpleNamespace(status="completed", output=[{"type": "function_call", "name": name,
            "call_id": f"fictional-p50-{step}", "arguments": json.dumps(arguments)}]), {
                "synthetic_dispatch": True, "provider_calls": 0}, 0.0


class FictionalCatalog:
    def __init__(self, data, *, find_status="ok"):
        self.data, self.find_status, self.calls = data, find_status, []

    def definitions(self):
        return ToolCatalog(None, Filters()).definitions()

    def call(self, name, arguments):
        TOOLS[name][0].model_validate(arguments)
        self.calls.append((name, deepcopy(arguments)))
        if name == "find_records":
            return {"status": self.find_status, "records": [{**RECORD, "title": "Moonlit Orchard"}],
                    "total_candidates": 1 if self.find_status == "ok" else 2,
                    "filters": Filters().model_dump(mode="json")}
        return deepcopy(self.data)


def test_existing_title_lookup_binds_exact_record_before_requested_field_read():
    data = metadata(["publisher"], {"publisher": "Fable Journal"})
    catalog = FictionalCatalog(data)
    agent = ScriptedTitleAgent(catalog, [
        ("find_records", {"title": "Moonlit Orchard"}),
        ("get_record_metadata", {"record_id": RECORD["record_id"], "fields": ["publisher"]}),
    ])
    run = agent.run('Read the stored publisher for "Moonlit Orchard"', Filters(), "fictional")
    assert run.route == "metadata" and not run.failure_reason and run.cost_usd == 0
    assert [name for name, _ in catalog.calls] == ["find_records", "get_record_metadata"]
    assert "get_record_metadata" not in agent.advertised[0]
    assert "get_record_metadata" in agent.advertised[1]
    assert run.result["record"] == RECORD
    assert list(run.result["original_fields"]) == ["publisher"]


@pytest.mark.parametrize("changed", ["record_id", "version_id", "body_hash"])
def test_title_binding_rejects_a_different_record_or_source_version(changed):
    data = metadata(["publisher"], {"publisher": "Fable Journal"})
    data["record"][changed] = "different-source-identity"
    agent = ScriptedTitleAgent(FictionalCatalog(data), [
        ("find_records", {"title": "Moonlit Orchard"}),
        ("get_record_metadata", {"record_id": RECORD["record_id"], "fields": ["publisher"]}),
    ])
    run = agent.run('Read the stored publisher for "Moonlit Orchard"', Filters(), "fictional")
    assert run.failure_reason == "research_record_version_changed"


def test_ambiguous_title_does_not_read_one_candidate():
    catalog = FictionalCatalog({}, find_status="ambiguous")
    agent = ScriptedTitleAgent(catalog, [("find_records", {"title": "Moonlit Orchard"})])
    run = agent.run('Read the stored publisher for "Moonlit Orchard"', Filters(), "fictional")
    assert run.route == "clarify" and run.failure_reason == "original_source_record_unresolved"
    assert len(catalog.calls) == 1
