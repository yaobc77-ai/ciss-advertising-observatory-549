"""Independent synthetic repair checks; no customer questions or answer files."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from types import SimpleNamespace

import pytest

from observatory.evaluation_mask import (
    EvaluationOnlyError,
    customer_question_texts,
    evaluation_active,
    evaluation_session,
)
from observatory.models import Answer, Filters
from observatory.prompts import ANSWER_SYSTEM, RESEARCH_SYSTEM
from observatory.question_policy import (
    entity_dates_used_as_filters,
    literal_scope_preserved,
    original_metadata_question,
    question_contract,
)
from observatory.research_agent import (
    ResearchAgent,
    ResearchRun,
    _require_strict_objects,
)
from observatory.service import Service


def definition(name):
    return {"type": "function", "name": name, "strict": True, "description": "Synthetic read",
            "parameters": {"type": "object", "additionalProperties": False,
                           "properties": {}, "required": []}}


def stats(dataset, total):
    return {"status": "ok", "kind": "count", "method": "database", "group_by": "none",
            "filters": Filters(dataset=dataset).model_dump(mode="json"),
            "collections": [{"dataset": dataset, "total": total, "retrievable": total, "unknown_dates": 0}], "groups": []}


def metadata(value="Archive date"):
    return {"status": "ok", "record": {"record_id": "synthetic-record", "dataset": "native"},
            "original_fields": {"publication_date": {"status": "recorded" if value else "unknown", "value": value}}}


class Catalog:
    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def definitions(self):
        return [definition(name) for name in ("record_statistics", "get_record_metadata", "find_records", "get_content_matches")]

    def call(self, name, args):
        self.calls.append((name, deepcopy(args)))
        result = self.results.get(name)
        return result(args) if callable(result) else deepcopy(result)


class ScriptedAgent(ResearchAgent):
    def __init__(self, script, catalog=None, **kwargs):
        super().__init__(None, catalog or Catalog(), **kwargs)
        self.script = iter(script)
        self.sent = []

    def _dispatch(self, inputs, definitions, visitor, step):
        self.sent.append(deepcopy({"input": inputs, "tools": definitions}))
        name, args = next(self.script)
        response = {"status": "completed", "output": [{"type": "function_call", "name": name,
                    "call_id": f"synthetic-{step}", "arguments": json.dumps(args)}]}
        return response, {"state": "synthetic", "step": step}, 0.0


def plan():
    return {"tasks": [{"question_part": "Count native records", "dataset": "native", "route": "statistics"},
                      {"question_part": "count social records", "dataset": "social", "route": "statistics"}]}


def test_customer_loader_denies_before_file_read(monkeypatch):
    def fail_read(*args, **kwargs):
        raise AssertionError("Loader must reject before reading bytes")
    monkeypatch.setattr("observatory.evaluation_mask.Path.read_bytes", fail_read)
    assert not evaluation_active()
    with pytest.raises(EvaluationOnlyError):
        customer_question_texts()


@pytest.mark.parametrize("purpose", ["training", "development", "prompt_tuning", "", "customer_test "])
def test_evaluation_purpose_is_explicit(purpose):
    with pytest.raises(EvaluationOnlyError):
        with evaluation_session(purpose=purpose):
            pass


def test_evaluation_context_resets_even_on_exception():
    with pytest.raises(ValueError):
        with evaluation_session(purpose="evaluation"):
            assert evaluation_active()
            raise ValueError("synthetic")
    assert not evaluation_active()


def test_normal_request_has_no_holdout_catalog_or_tool():
    agent = ScriptedAgent([("record_statistics", {"filters": {"dataset": "native"}})],
                          Catalog({"record_statistics": stats("native", 7)}))
    result = agent.run("Count records", Filters(dataset="native"), "synthetic")
    assert result.route == "statistics" and result.result["collections"][0]["total"] == 7
    payload = json.loads(agent.sent[0]["input"][1]["content"])
    assert set(payload) == {"question", "active_scope", "question_contract", "entity_context", "reference_date"}
    assert "get_content_matches" not in {item["name"] for item in agent.sent[0]["tools"]}
    for system in (RESEARCH_SYSTEM, ANSWER_SYSTEM):
        assert "client_content_questions" not in system and "Q01" not in system and "Q24" not in system
    for item in agent.sent[0]["tools"]:
        _require_strict_objects(item["parameters"])


def test_stored_original_field_cannot_route_to_statistics():
    catalog = Catalog({"record_statistics": stats("native", 7)})
    agent = ScriptedAgent([("record_statistics", {})], catalog)
    result = agent.run("Read the stored spreadsheet date for Spring Notes", Filters(dataset="native"), "synthetic")
    assert result.failure_reason == "original_source_metadata_required" and catalog.calls == []


@pytest.mark.parametrize("status", ["ambiguous", "not_found"])
def test_title_lookup_does_not_select_an_external_substitute(status):
    agent = ScriptedAgent([("find_records", {"title": "Spring Notes"})],
                          Catalog({"find_records": {"status": status, "records": []}}))
    result = agent.run("Read the original spreadsheet date for Spring Notes", Filters(dataset="native"), "synthetic")
    assert result.route == "clarify" and result.failure_reason == "original_source_record_unresolved"


def web_harness(calls):
    service = object.__new__(Service)
    service.health = lambda: {"status": "ok", "data_version": "v1"}

    def search(*args, **kwargs):
        calls.append(args)
        return {"status": "disabled", "reason": "web_search_not_enabled", "cost_usd": 0}

    service._search_external = search
    return service


def test_original_unknown_still_attempts_a_labeled_web_answer():
    # User decision 2026-10-05: a field missing from the source is supplemented,
    # and what remains missing goes to a labelled web search. The stored field stays Unknown.
    calls = []
    result = Service._tool_metadata_answer(metadata(None))
    assert result.status == "insufficient_evidence"
    assert web_harness(calls)._ensure_answer("Read its date", Filters(), "synthetic", result) is result
    assert len(calls) == 1


def test_compound_reads_preserve_both_datasets_and_counts():
    catalog = Catalog({"record_statistics": lambda args: stats(args["filters"]["dataset"],
                       7 if args["filters"]["dataset"] == "native" else 11)})
    agent = ScriptedAgent([("set_research_plan", plan()),
                          ("record_statistics", {"filters": {"dataset": "native"}}),
                          ("record_statistics", {"filters": {"dataset": "social"}})], catalog)
    run = agent.run("Count native records and count social records", Filters(dataset="all"), "synthetic")
    assert run.route == "composite" and run.result["complete"]
    assert [part["result"]["collections"][0]["total"] for part in run.result["parts"]] == [7, 11]
    result = object.__new__(Service)._tool_composite_answer(run.result, Filters(dataset="all"))
    assert result.status == "answered" and "7" in result.answer and "11" in result.answer
    from observatory.app import _render_answer_result
    shown = str(_render_answer_result(result.model_dump(mode="json"), True, object()))
    assert "7" in shown and "11" in shown and "query-records-scope" not in shown


@pytest.mark.parametrize("bad_args", [{"filters": {"dataset": "native"}}, {"filters": {"dataset": "all"}}])
def test_compound_dropped_dataset_is_not_completed(bad_args):
    catalog = Catalog({"record_statistics": stats("native", 7)})
    agent = ScriptedAgent([("set_research_plan", plan()),
                          ("record_statistics", {"filters": {"dataset": "native"}}),
                          ("record_statistics", bad_args)], catalog)
    run = agent.run("Count native records and count social records", Filters(dataset="all"), "synthetic")
    assert run.failure_reason == "explicit_collection_scope_missing"
    assert not run.result["complete"] and len(run.result["parts"]) == 1 and len(catalog.calls) == 1
    result = object.__new__(Service)._tool_composite_answer(run.result, Filters(dataset="all"))
    assert result.status == "insufficient_evidence" and "count social records" in result.answer


def test_compound_plan_cannot_omit_an_extra_user_task():
    agent = ScriptedAgent([("set_research_plan", plan())])
    run = agent.run("Count native records and count social records and show source files", Filters(dataset="all"), "synthetic")
    assert run.failure_reason == "research_plan_invalid" and agent.catalog.calls == []


def test_compound_plan_keeps_completed_part_when_limit_is_hit():
    agent = ScriptedAgent([("set_research_plan", plan()),
                          ("record_statistics", {"filters": {"dataset": "native"}})],
                         Catalog({"record_statistics": stats("native", 7)}), max_steps=2)
    run = agent.run("Count native records and count social records", Filters(dataset="all"), "synthetic")
    assert run.route == "composite" and run.failure_reason == "research_step_limit"
    assert len(run.result["parts"]) == 1 and len(run.result["pending_parts"]) == 1


@pytest.mark.parametrize("question, expected", [
    ("Count records for Orion (2012–2021)", True),
    ("Count records from Orion (2012–2021)", True),
    ("Count records for Orion (2012–2021) during 2019", False),
    ("Count records for Orion", False),
])
def test_literal_entity_date_is_not_a_date_range(question, expected):
    assert entity_dates_used_as_filters(question, {"sponsors": [{"value": "Orion (2012–2021)"}]},
                                        {"filters": {"date_from": "2012-01-01"}}) is expected


def test_entity_date_guard_blocks_before_tool_read():
    agent = ScriptedAgent([("record_statistics", {"filters": {"date_from": "2012-01-01"}})],
                          entity_context={"sponsors": [{"value": "Orion (2012–2021)"}]})
    run = agent.run("Count records for Orion (2012–2021)", Filters(dataset="social"), "synthetic")
    assert run.failure_reason == "entity_name_is_not_date_scope" and agent.catalog.calls == []


class SavedDB:
    def __init__(self):
        self.saved = []

    def save_answer(self, question, filters, result, version):
        self.saved.append((deepcopy(result.model_dump(mode="json")), version))


def test_final_audit_saved_once_with_returned_latency(monkeypatch):
    db = SavedDB()
    agent = SimpleNamespace(run=lambda *args, **kwargs: ResearchRun(route="metadata", result=metadata(), original_question="Read stored date"))
    service = Service(SimpleNamespace(research_agent_enabled=True), db=db, rag=object(), research_agent=agent)
    service.health = lambda: {"status": "ok", "data_version": "synthetic-version"}
    ticks = iter([1.0, 1.01, 1.02, 1.50])
    monkeypatch.setattr("observatory.service.time.monotonic", lambda: next(ticks))
    result = service.answer("Read stored date", Filters(dataset="native"), "synthetic")
    assert result.status == "answered" and len(db.saved) == 1
    assert db.saved[0][0] == result.model_dump(mode="json") and result.latency_ms == 500
    assert db.saved[0][1] == "synthetic-version"


def test_original_metadata_cues_do_not_parse_general_counts():
    assert original_metadata_question("Read the original field date")
    assert original_metadata_question("Read the stored original title")
    assert not original_metadata_question("Count records by year")


def test_masked_launcher_recognizes_protected_paths_without_reading():
    from scripts.run_masked_checks import protected_path
    assert protected_path("eval/customer_metrics/arbitrary.json")
    assert protected_path(".runtime/full_test_20261007/root/arbitrary.json")
    assert protected_path("tests/__pycache__/test_question_policy.cpython-312.pyc")
    assert not protected_path("tests/test_masked_repair.py")


def test_normal_public_content_read_requires_evaluation_context():
    with pytest.raises(EvaluationOnlyError):
        object.__new__(Service).content_matches(Filters(dataset="native"), question_id="synthetic")


def test_partial_plan_attempts_a_labeled_web_answer_and_keeps_the_reason():
    calls = []
    result = Answer(status="insufficient_evidence", answer="A database task is unfinished",
                    failure_reason="research_plan_incomplete", answer_mode="tools")
    assert web_harness(calls)._ensure_answer("Complete both parts", Filters(), "synthetic", result) is result
    assert len(calls) == 1 and result.failure_reason == "research_plan_incomplete"


def test_original_field_ui_displays_unknown_and_source_status():
    from observatory.app import _render_answer_result
    result = Service._tool_metadata_answer(metadata(None)).model_dump(mode="json")
    cards = _render_answer_result(result, True, object())
    shown = str(cards)
    assert "Original source fields" in shown and "Unknown" in shown and "publication date" in shown
    assert "Data result unavailable" not in shown


def test_partial_plan_ui_keeps_record_detail_and_unfinished_task():
    from observatory.app import _render_answer_result
    record_answer = {"status": "answered", "answer_mode": "tools", "answer": "Saved source record",
                     "structured_result": {"kind": "record", "record": {"record_id": "synthetic-record", "title": "Spring Notes"},
                                           "body": {"text": "A synthetic article.", "start": 0, "end": 20, "total_characters": 20}}}
    result = {"status": "insufficient_evidence", "answer_mode": "tools", "structured_result": {
        "kind": "composite", "complete": False,
        "parts": [{"question_part": "Read Spring Notes", "answer": record_answer}],
        "pending_parts": [{"question_part": "count social records"}]}}
    shown = str(_render_answer_result(result, True, object()))
    assert "Partial answer" in shown and "Spring Notes" in shown and "count social records" in shown


def test_content_panel_has_no_test_options_and_is_hidden():
    from observatory.content_ui import content_panel
    panel = content_panel()
    assert panel.style == {"display": "none"}
    assert "question_exact" not in str(panel)


def test_plan_outside_active_collection_is_rejected_before_read():
    agent = ScriptedAgent([("set_research_plan", plan())])
    run = agent.run("Count native records and count social records", Filters(dataset="native"), "synthetic")
    assert run.failure_reason == "research_plan_invalid" and agent.catalog.calls == []


def test_wrong_result_dataset_cannot_complete_plan():
    catalog = Catalog({"record_statistics": stats("social", 11)})
    agent = ScriptedAgent([("set_research_plan", plan()),
                          ("record_statistics", {"filters": {"dataset": "native"}})], catalog)
    run = agent.run("Count native records and count social records", Filters(dataset="all"), "synthetic")
    assert run.failure_reason == "research_plan_result_scope_mismatch" and not run.result["complete"]
    assert not run.result["parts"]


def test_content_predicate_cannot_become_an_unrelated_count():
    agent = ScriptedAgent([("record_statistics", {"filters": {"dataset": "native"}})])
    run = agent.run("Count articles that describe staff training as helpful", Filters(dataset="native"), "synthetic")
    assert run.route == "clarify" and agent.catalog.calls == []


def test_unplanned_compound_does_not_publish_first_count():
    agent = ScriptedAgent([("record_statistics", {"filters": {"dataset": "native"}})],
                          Catalog({"record_statistics": stats("native", 7)}))
    run = agent.run("Count native records and count social records", Filters(dataset="all"), "synthetic")
    assert run.route == "clarify" and run.failure_reason in {"research_plan_required", "explicit_collection_scope_missing"}
    assert agent.catalog.calls == []


@pytest.mark.parametrize("condition", ["publisher", "date"])
def test_planned_predicate_cannot_disappear(condition):
    first = "Count records for North Daily" if condition == "publisher" else "Count records during 2019"
    second = "count records for West Weekly" if condition == "publisher" else "count records during 2020"
    task_plan = {"tasks": [{"question_part": part, "dataset": "native", "route": "statistics"}
                          for part in (first, second)]}
    agent = ScriptedAgent([("set_research_plan", task_plan),
                          ("record_statistics", {"filters": {"dataset": "native"}})],
                         Catalog({"record_statistics": stats("native", 7)}),
                         entity_context={"publishers": [{"value": "North Daily"}, {"value": "West Weekly"}]})
    run = agent.run(first + " and " + second, Filters(dataset="native"), "synthetic")
    assert run.failure_reason == "research_plan_predicate_missing" and not run.result["complete"]
    assert agent.catalog.calls == []


def test_direct_legacy_evidence_does_not_embed_or_search_original_title():
    service = object.__new__(Service)
    result = service._answer_evidence("Read the stored original title", Filters(dataset="native"), "synthetic")
    assert result.status == "insufficient_evidence" and result.failure_reason == "original_source_metadata_required"


def test_legacy_compound_cannot_drop_second_read():
    result = object.__new__(Service)._answer_legacy("Count native records and count social records", Filters(dataset="all"), "synthetic")
    assert result.failure_reason == "research_plan_required"


def test_date_inside_literal_name_does_not_add_a_plan_date_filter():
    name = "Orion (2012-01-01)"
    assert literal_scope_preserved("Count records for " + name, {"sponsors": [{"value": name}]},
                                   {"sponsors": [name]}, Filters(dataset="native").model_dump(mode="json"))


def test_planned_publisher_filter_cannot_add_an_unrequested_publisher():
    assert not literal_scope_preserved("Count records for Lumen", {"publishers": [{"value": "Lumen"}, {"value": "Cedar"}]},
                                       {"publishers": ["Lumen", "Cedar"]}, Filters().model_dump(mode="json"))


def test_short_entity_cannot_match_inside_an_ordinary_word():
    assert literal_scope_preserved("Count articles", {"publishers": [{"value": "Art"}]}, {}, Filters().model_dump(mode="json"))


@pytest.mark.parametrize("question, requested", [
    ("Count records during 2019", {"date_from": "2019-01-01", "date_to": "2019-12-31"}),
    ("Count records between 2019-01-01 and 2019-12-31", {"date_from": "2019-01-01", "date_to": "2019-12-31"}),
])
def test_planned_dates_preserve_a_narrower_trusted_ui_scope(question, requested):
    base = Filters(date_from="2019-06-01", date_to="2019-08-31").model_dump(mode="json")
    assert literal_scope_preserved(question, {}, requested, base)
    assert literal_scope_preserved(question, {}, {"date_from": "2019-06-01", "date_to": "2019-08-31"}, base)


def test_explicit_publisher_role_does_not_require_same_named_sponsor():
    assert literal_scope_preserved("Count records for publisher Lumen", {"publishers": [{"value": "Lumen"}], "sponsors": [{"value": "Lumen"}]},
                                   {"publishers": ["Lumen"]}, Filters().model_dump(mode="json"))


def test_unmentioned_model_restriction_cannot_shrink_a_planned_count():
    assert not literal_scope_preserved("Count records for publisher Lumen", {"publishers": [{"value": "Lumen"}], "sponsors": [{"value": "Cedar"}]},
                                       {"publishers": ["Lumen"], "sponsors": ["Cedar"]}, Filters().model_dump(mode="json"))


def test_separator_variant_is_still_a_compound_read():
    agent = ScriptedAgent([("record_statistics", {"filters": {"dataset": "native"}})])
    run = agent.run("Count native records; count social records", Filters(dataset="all"), "synthetic")
    assert run.route == "clarify" and agent.catalog.calls == []


def test_single_explicit_social_collection_cannot_use_native_count():
    agent = ScriptedAgent([("record_statistics", {"filters": {"dataset": "native"}})])
    run = agent.run("Count social records", Filters(dataset="all"), "synthetic")
    assert run.failure_reason == "explicit_collection_scope_missing" and agent.catalog.calls == []


def test_final_audit_context_is_request_local_across_threads():
    barrier = Barrier(2)

    def service():
        db = SavedDB()
        def run(*args, **kwargs):
            barrier.wait(timeout=5)
            return ResearchRun(route="metadata", result=metadata(), original_question="Read stored date")
        item = Service(SimpleNamespace(research_agent_enabled=True), db=db, rag=object(),
                       research_agent=SimpleNamespace(run=run))
        item.health = lambda: {"status": "ok", "data_version": "synthetic-version"}
        return item, db

    outer, outer_db = service()
    direct, direct_db = service()
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(outer.answer, "Read stored date", Filters(dataset="native"), "synthetic")
        two = pool.submit(direct._answer_with_tools, "Read stored date", Filters(dataset="native"), "synthetic")
        for future, db in ((one, outer_db), (two, direct_db)):
            result = future.result(timeout=10)
            assert result.status == "answered" and len(db.saved) == 1
            assert db.saved[0][0] == result.model_dump(mode="json")


@pytest.mark.parametrize("question", ['Read record "Native Records Digest"', 'Read record "Notes; Count Records"'])
def test_quoted_title_is_not_a_collection_or_task_instruction(question):
    contract = question_contract(question)
    assert not contract["explicit_datasets"] and not contract["compound_read_request"]
