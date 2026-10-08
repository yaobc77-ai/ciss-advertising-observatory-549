"""Fresh development cases: ferry/school records, no client question roster."""

from types import SimpleNamespace

import pytest

from observatory.models import Filters
from observatory.research_agent import ResearchAgent, _validate_plan
from observatory.research_tools import ToolCatalog, strict_schema

TOOLS = (
    "record_statistics", "search_records", "get_record", "get_record_metadata",
    "get_record_sources", "get_graph_neighborhood", "get_claims_matches",
    "find_records", "resolve_entity", "get_graph_schema",
)


class Catalog:
    def __init__(self, replies=None):
        self.replies = replies or {}
        self.calls = []

    def definitions(self):
        return [{"type": "function", "name": name, "strict": True,
                 "parameters": {"type": "object", "properties": {},
                                "required": [], "additionalProperties": False}}
                for name in TOOLS]

    def call(self, name, args):
        self.calls.append((name, args))
        return self.replies.get(name, {"status": "ok", "filters": {"dataset": "native"}})


class ScriptedAgent(ResearchAgent):
    """Replay decisions; no provider and no assertion of model quality."""

    def __init__(self, catalog, decisions):
        super().__init__(SimpleNamespace(), catalog, entity_context={})
        self.decisions = iter(decisions)
        self.advertised = []

    def _dispatch(self, inputs, definitions, visitor, step):
        import json

        self.advertised.append([item["name"] for item in definitions])
        name, args = next(self.decisions)
        return ({"status": "completed", "output": [{"type": "function_call",
                "name": name, "call_id": f"synthetic-call-{step}",
                "arguments": json.dumps(args)}]}, {"step": step, "state": "synthetic"}, 0.0)


def _found():
    return {"status": "ok", "total_candidates": 1, "records": [{
        "record_id": "harbor-original", "dataset": "native",
        "version_id": "version-a", "body_hash": "body-a",
    }]}


def _metadata(record_id="harbor-original", version="version-a", body_hash="body-a"):
    return {"status": "ok", "record": {"record_id": record_id, "dataset": "native",
            "version_id": version, "body_hash": body_hash}, "original_fields": {}}


def test_original_read_cannot_switch_to_another_existing_record():
    catalog = Catalog({"find_records": _found(), "get_record_metadata": _metadata("other-valid")})
    agent = ScriptedAgent(catalog, [("find_records", {"title": "Harbor timetable"}),
                          ("get_record_metadata", {"record_id": "other-valid"})])
    run = agent.run('Read the stored publication date of "Harbor timetable".', Filters(), "dev")
    assert run.route == "clarify"
    assert run.failure_reason == "research_record_binding_mismatch"
    assert [name for name, _ in catalog.calls] == ["find_records"]


@pytest.mark.parametrize("version,body_hash", [("version-b", "body-a"), ("version-a", "body-b")])
def test_original_read_rejects_changed_selection_version(version, body_hash):
    catalog = Catalog({"find_records": _found(), "get_record_metadata": _metadata(version=version, body_hash=body_hash)})
    agent = ScriptedAgent(catalog, [("find_records", {"title": "Harbor timetable"}),
                          ("get_record_metadata", {"record_id": "harbor-original"})])
    run = agent.run('Read the stored publication date of "Harbor timetable".', Filters(), "dev")
    assert run.route == "unavailable"
    assert run.failure_reason == "research_record_version_changed"


def test_original_read_keeps_matched_record():
    catalog = Catalog({"find_records": _found(), "get_record_metadata": _metadata()})
    agent = ScriptedAgent(catalog, [("find_records", {"title": "Harbor timetable"}),
                          ("get_record_metadata", {"record_id": "harbor-original"})])
    run = agent.run('Read the stored publication date of "Harbor timetable".', Filters(), "dev")
    assert run.route == "metadata"
    assert run.result["record"]["record_id"] == "harbor-original"
    assert "record_statistics" not in agent.advertised[0]
    assert "search_records" not in agent.advertised[1]
    assert "request_clarification" in agent.advertised[1]


@pytest.mark.parametrize("record_ids,metadata_available", [([], False), (["harbor-original"], True)])
def test_ordinary_single_read_retains_tools_subject_to_record_binding(record_ids, metadata_available):
    filters = Filters(record_ids=record_ids)
    catalog = Catalog({"record_statistics": {"status": "ok", "filters": filters.model_dump(mode="json")}})
    agent = ScriptedAgent(catalog, [("record_statistics", {"filters": {"dataset": "native"}})])
    run = agent.run("Show record totals.", filters, "dev")
    assert run.route == "statistics"
    available = set(agent.advertised[0])
    assert (set(TOOLS) - {"get_record_metadata"}).issubset(available)
    assert ("get_record_metadata" in available) == metadata_available
    assert run.cost_usd == 0


def test_compound_tasks_are_advertised_before_any_final_read():
    question = "Read Harbor sources and show school records."
    tasks = [{"question_part": "Read Harbor sources", "dataset": "native", "route": "sources"},
             {"question_part": "show school records.", "dataset": "native", "route": "record"}]
    catalog = Catalog({"get_record_sources": {"status": "ok", "record": {"dataset": "native"}},
                       "get_record": {"status": "ok", "record": {"dataset": "native"}}})
    agent = ScriptedAgent(catalog, [("set_research_plan", {"tasks": tasks}),
                          ("get_record_sources", {"record_id": "harbor-original"}),
                          ("get_record", {"record_id": "school-original"})])
    run = agent.run(question, Filters(), "dev")
    assert run.route == "composite"
    assert run.result["complete"] is True
    assert "record_statistics" not in agent.advertised[0]
    assert "get_record_sources" in agent.advertised[1]
    assert "get_record" not in agent.advertised[1]
    assert "get_record" in agent.advertised[2]
    assert "get_record_sources" not in agent.advertised[2]


def test_model_cannot_use_an_unadvertised_read():
    catalog = Catalog()
    agent = ScriptedAgent(catalog, [("record_statistics", {})])
    run = agent.run('Read the stored publication date of "Harbor timetable".', Filters(), "dev")
    assert run.failure_reason == "original_source_metadata_required"
    assert catalog.calls == []


def test_plan_rejects_overlapping_question_parts():
    question = "Read Harbor sources and show school records."
    args = {"tasks": [{"question_part": question, "dataset": "native", "route": "sources"},
                      {"question_part": "show school records.", "dataset": "native", "route": "record"}]}
    with pytest.raises(ValueError, match="overlap"):
        _validate_plan(args, question, Filters())


def test_plan_rejects_reversed_question_order():
    question = "Read Harbor sources and show school records."
    args = {"tasks": [{"question_part": "show school records.", "dataset": "native", "route": "record"},
                      {"question_part": "Read Harbor sources", "dataset": "native", "route": "sources"}]}
    with pytest.raises(ValueError, match="order"):
        _validate_plan(args, question, Filters())


def test_plan_read_does_not_reuse_another_parts_record_binding():
    question = 'Read the original date of "Harbor timetable" and show school records.'
    tasks = [{"question_part": 'Read the original date of "Harbor timetable"', "dataset": "native", "route": "metadata"},
             {"question_part": "show school records.", "dataset": "native", "route": "record"}]
    catalog = Catalog({"find_records": _found(), "get_record_metadata": _metadata(),
                       "get_record": {"status": "ok", "record": {"record_id": "school-original", "dataset": "native"}}})
    agent = ScriptedAgent(catalog, [("set_research_plan", {"tasks": tasks}),
                          ("find_records", {"title": "Harbor timetable"}),
                          ("get_record_metadata", {"record_id": "harbor-original"}),
                          ("get_record", {"record_id": "school-original"})])
    run = agent.run(question, Filters(), "dev")
    assert run.route == "composite"
    assert run.result["complete"] is True


def test_actual_catalog_schemas_are_valid_and_preserve_the_title_argument():
    catalog = ToolCatalog(SimpleNamespace(), Filters())
    definitions = ResearchAgent(SimpleNamespace(), catalog)._definitions()
    find = next(item for item in definitions if item["name"] == "find_records")
    assert "title" in find["parameters"]["properties"]
    assert "title" in find["parameters"]["required"]


def test_schema_annotation_removal_does_not_remove_identically_named_fields():
    schema = {"type": "object", "title": "Synthetic school schema", "properties": {
        "title": {"type": "string", "title": "Title", "default": ""},
        "default": {"type": "string", "default": "teacher value"},
        "properties": {"type": "object", "properties": {"title": {"type": "string"}}},
    }}
    strict = strict_schema(schema)
    assert "title" not in strict
    assert set(strict["properties"]) == {"title", "default", "properties"}
    assert "default" not in strict["properties"]["default"]
    assert "title" in strict["properties"]["properties"]["properties"]
    assert set(strict["required"]) == set(strict["properties"])
    assert schema["title"] == "Synthetic school schema"


@pytest.mark.parametrize("tool", ["get_record", "get_record_sources"])
@pytest.mark.parametrize("changed", [False, True])
def test_bound_source_reads_validate_record_version_independent_of_observation(tool, changed):
    record = _metadata(version="version-b" if changed else "version-a")
    record["body"] = {"body_hash": "a-different-preserved-observation"}
    catalog = Catalog({"find_records": _found(), tool: record})
    agent = ScriptedAgent(catalog, [("find_records", {"title": "Harbor timetable"}),
                          (tool, {"record_id": "harbor-original"})])
    run = agent.run('Read "Harbor timetable".', Filters(), "dev")
    if changed:
        assert run.failure_reason == "research_record_version_changed"
    else:
        assert run.route in {"record", "sources"}


def test_unique_selection_cannot_be_replaced_by_another_title_lookup():
    catalog = Catalog({"find_records": _found()})
    agent = ScriptedAgent(catalog, [("find_records", {"title": "Harbor timetable"}),
                          ("find_records", {"title": "School timetable"})])
    run = agent.run('Read the stored date of "Harbor timetable".', Filters(), "dev")
    assert run.failure_reason == "research_record_binding_mismatch"
    assert len(catalog.calls) == 1


def test_public_record_projection_preserves_hash_without_source_body():
    service = SimpleNamespace(_public_rows=lambda rows: rows)
    catalog = ToolCatalog(service, Filters())
    projection = catalog._record_projection({"record_id": "harbor-original", "dataset": "native",
        "version_id": "version-a", "body_hash": "body-a", "body": "Private source passage"})
    assert projection["body_hash"] == "body-a"
    assert "body" not in projection

