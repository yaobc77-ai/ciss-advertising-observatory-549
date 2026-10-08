"""New synthetic title/record selection stages; scripted calls, no API/SQL."""
import json
from copy import deepcopy
from types import SimpleNamespace

from test_tool_search_diagnostics import (
    SOURCE,
    NoProvider,
    catalog_for,
    production_coverage,
)

from observatory.models import Filters
from observatory.research_agent import ResearchAgent
from observatory.research_tools import ToolCatalog


class ScriptedAgent(ResearchAgent):
    def __init__(self, catalog, calls, *, max_steps=4):
        super().__init__(NoProvider(), catalog, max_steps=max_steps)
        self.calls = calls
        self.advertised = []

    def _dispatch(self, inputs, definitions, visitor, step):
        self.advertised.append({definition["name"] for definition in definitions})
        name, arguments = (self.calls[step - 1] if step <= len(self.calls) else (
            "request_clarification", {"message": "Select a source before continuing.", "reason": "missing_context"}))
        return SimpleNamespace(status="completed", output=[{"type": "function_call", "name": name,
            "call_id": f"fresh-selection-{step}", "arguments": json.dumps(arguments)}]), {
                "synthetic_dispatch": True, "provider_calls": 0}, 0.0


def make_catalog(base=None):
    catalog = catalog_for(production_coverage(), web_enabled=False)
    base = base or Filters(include_inferred_dates=False)
    catalog = ToolCatalog(catalog.service, base)
    row = deepcopy(SOURCE)
    row["metadata_origins"] = [{"origin_kind": "original_metadata", "priority": 0,
        "cells": [{"field": field, "present": True, "payload_field_path": [field], "value": row[field]}
                  for field in ("title", "publisher")],
        "provenance": {"row": 1, "source_sha256": "d" * 64}}]
    catalog.service.db.original_record_metadata = lambda filters, record_id: deepcopy(row)
    catalog.service.db.find_records = lambda filters, title, limit: {
        "rows": [deepcopy(row)], "total_candidates": 1, "match_type": "exact"}
    return catalog, base


TITLE_QUESTION = 'What is the original metadata title of "Fresh Canal Pilot Bulletin"?'
READ = ("get_record_metadata", {"record_id": SOURCE["record_id"], "fields": ["title"]})


def test_title_question_cannot_start_with_an_unselected_id():
    catalog, base = make_catalog()
    agent = ScriptedAgent(catalog, [READ])
    run = agent.run(TITLE_QUESTION, base, "fresh-title-stage")
    assert run.route == "clarify", run.audit()
    assert "get_record_metadata" not in agent.advertised[0]


def test_verified_title_selection_enables_metadata_read_of_same_record():
    catalog, base = make_catalog()
    agent = ScriptedAgent(catalog, [("find_records", {"title": SOURCE["title"]}), READ])
    run = agent.run(f'What is the original metadata title of "{SOURCE["title"]}"?', base, "fresh-selected-stage")
    assert run.route == "metadata" and run.result["record"]["record_id"] == SOURCE["record_id"], run.audit()
    assert "get_record_metadata" not in agent.advertised[0]
    assert "get_record_metadata" in agent.advertised[1]


def test_single_trusted_active_record_id_allows_direct_metadata_read():
    catalog, base = make_catalog(Filters(record_ids=[SOURCE["record_id"]], include_inferred_dates=False))
    agent = ScriptedAgent(catalog, [READ])
    run = agent.run("What is the original metadata title of the selected record?", base, "fresh-trusted-stage")
    assert run.route == "metadata", run.audit()
    assert "get_record_metadata" in agent.advertised[0]


def test_single_trusted_active_record_id_cannot_be_replaced_by_arguments():
    catalog, base = make_catalog(Filters(record_ids=[SOURCE["record_id"]], include_inferred_dates=False))
    agent = ScriptedAgent(catalog, [("get_record_metadata", {"record_id": "contract-other-id", "fields": ["title"]})])
    calls = []
    catalog.service.db.original_record_metadata = lambda *args: calls.append(args)
    run = agent.run("What is the original metadata title of the selected record?", base, "fresh-swapped-trusted-stage")
    assert run.route == "clarify" and not calls, run.audit()


def test_multiple_trusted_ids_still_require_selection():
    catalog, base = make_catalog(Filters(record_ids=[SOURCE["record_id"], "contract-other-id"], include_inferred_dates=False))
    agent = ScriptedAgent(catalog, [READ])
    run = agent.run("What is the original metadata title of the selected records?", base, "fresh-multiple-stage")
    assert run.route == "clarify", run.audit()
    assert "get_record_metadata" not in agent.advertised[0]


def composite_parts():
    parts = ['Read the original metadata title of "Independent prototype notice"',
             'read the original metadata publisher of "Independent prototype notice"']
    plan = {"tasks": [{"question_part": part, "dataset": "native", "route": "metadata"} for part in parts]}
    return "; ".join(parts) + ".", plan


def test_composite_metadata_cannot_skip_selection_for_current_part():
    catalog, base = make_catalog()
    question, plan = composite_parts()
    agent = ScriptedAgent(catalog, [("set_research_plan", plan), READ])
    run = agent.run(question, base, "fresh-composite-stage")
    assert run.route == "composite" and not run.result["complete"] and not run.result["parts"], run.audit()
    assert "get_record_metadata" not in agent.advertised[1]


def test_composite_budget_exhaustion_does_not_relax_next_selection():
    catalog, base = make_catalog()
    question, plan = composite_parts()
    agent = ScriptedAgent(catalog, [("set_research_plan", plan),
        ("find_records", {"title": SOURCE["title"]}), READ,
        ("find_records", {"title": SOURCE["title"]})], max_steps=4)
    run = agent.run(question, base, "fresh-composite-budget-stage")
    assert run.route == "composite" and not run.result["complete"], run.audit()
    assert len(run.result["parts"]) == 1 and len(run.result["pending_parts"]) == 1
    assert "get_record_metadata" not in agent.advertised[3]
    assert run.failure_reason == "research_step_limit"


def test_single_trusted_record_allows_complete_composite_metadata_reads():
    catalog, base = make_catalog(Filters(record_ids=[SOURCE["record_id"]], include_inferred_dates=False))
    question, plan = composite_parts()
    agent = ScriptedAgent(catalog, [("set_research_plan", plan), READ,
        ("get_record_metadata", {"record_id": SOURCE["record_id"], "fields": ["publisher"]})])
    run = agent.run(question, base, "fresh-trusted-composite-stage")
    assert run.route == "composite" and run.result["complete"], run.audit()
    assert len(run.result["parts"]) == 2 and not run.result["pending_parts"]
