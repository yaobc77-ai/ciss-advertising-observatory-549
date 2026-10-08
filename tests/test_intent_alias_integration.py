"""Fresh fictional registry checks across intent compilation and tool scope guards."""

import json
from types import SimpleNamespace

import pytest

from observatory import entities, research_tools
from observatory.models import Filters
from observatory.question_intent import (
    EvidenceTask,
    IntentCompileError,
    QuestionIntent,
    SharedConstraint,
    StatisticsTask,
    compile_intent,
)
from observatory.research_tools import (
    FiltersRequest,
    SearchRequest,
    StatisticsRequest,
    ToolCatalog,
)


@pytest.fixture(autouse=True)
def fictional_registry(monkeypatch):
    data = {
        "organizations": [
            {"id": "org:sable-loom", "name": "Sable Loom Industries", "type": "company",
             "aliases": ["SLI", "寓织"], "former_names": ["Dusk Spindle"],
             "source_values": {"native.sponsor": ["sable-loom"],
                               "social.sponsor": ["Sable Loom Social"]}},
            {"id": "org:copper-orchard", "name": "Copper Orchard", "type": "company",
             "aliases": ["CO"], "source_values": {"native.sponsor": ["copper-orchard"]}},
        ],
        "outlets": [
            {"id": "outlet:teal-folio", "name": "Teal Folio", "aliases": ["TF", "蓝叶"],
             "former_names": ["Old Teal Weekly"],
             "source_values": {"native.publisher": ["teal-folio"]}},
        ],
    }
    registry = entities.Registry(data, json.dumps(data, sort_keys=True).encode())
    monkeypatch.setattr(entities, "registry", lambda: registry)
    monkeypatch.setattr(research_tools, "entity_registry", lambda: registry)
    return registry


class FictionService:
    def __init__(self):
        self.reads = []

    def facets(self, dataset):
        sponsors = {
            "native": ["sable-loom", "copper-orchard", "Hazel", "Hazel Studio"],
            "social": ["Sable Loom Social"],
        }
        selected = ("native", "social") if dataset == "all" else (dataset,)
        return {"sponsors": [name for collection in selected for name in sponsors[collection]],
                "publishers": ["teal-folio"] if "native" in selected else [],
                "platforms": [], "accounts": [], "keywords": [], "labels": []}

    def health(self):
        return {"status": "ok", "data_version": "fiction-data", "statistics_version": "fiction-stats",
                "countable_record_counts": {"native": 7, "social": 3}}

    def _statistics_answer(self, plan):
        self.reads.append(plan)
        selected = ("native", "social") if plan.filters.dataset == "all" else (plan.filters.dataset,)
        return SimpleNamespace(structured_result={
            "collections": [{"dataset": dataset, "total": 1} for dataset in selected],
            "groups": [], "records": [],
        })


def count_intent(question, filters, dataset="native", **request):
    return QuestionIntent(status="ready", tasks=[StatisticsTask(
        route="statistics", question_part=question, dataset=dataset,
        request=StatisticsRequest(filters=filters, **request),
    )])


def count(question, filters, *, base=None, dataset="native", **request):
    base = base or Filters(dataset=dataset)
    compiled = compile_intent(count_intent(question, filters, dataset, **request), question, base)
    service = FictionService()
    catalog = ToolCatalog(service, base)
    task = compiled.tasks[0]
    return compiled, catalog.call(task.tool_name, task.arguments), service


@pytest.mark.parametrize("dimension,source,mention", [
    ("sponsors", "sable-loom", "SLI"),
    ("sponsors", "sable-loom", "Dusk Spindle"),
    ("sponsors", "sable-loom", "寓织"),
    ("sponsors", "Sable Loom Industries", "SLI"),
    ("publishers", "teal-folio", "TF"),
    ("publishers", "teal-folio", "Old Teal Weekly"),
    ("publishers", "teal-folio", "蓝叶"),
    ("publishers", "Teal Folio", "TF"),
])
def test_registered_mention_compiles_without_rewriting_and_catalog_resolves(dimension, source, mention,
                                                                         fictional_registry):
    question = f"Count ({mention}) records"
    compiled, result, service = count(question, FiltersRequest(**{dimension: [source]}))
    assert compiled.tasks[0].arguments["filters"][dimension] == [source]
    assert result["status"] == "ok" and len(service.reads) == 1
    expected = "sable-loom" if dimension == "sponsors" else "teal-folio"
    assert getattr(service.reads[0].filters, dimension) == [expected]
    if source != expected:
        assert result["alias_resolutions"][0]["registry_version"] == fictional_registry.version
        assert result["alias_resolutions"][0]["review_status"] == "ai_proposed"


@pytest.mark.parametrize("question,value", [
    ("Count sli records", "sable-loom"),
    ("Count preslip records", "sable-loom"),
    ("Count SLIM records", "sable-loom"),
    ("Count ASLI records", "sable-loom"),
    ("Count nq records", "NQ"),
    ("Count Copper Orchard records", "sable-loom"),
])
def test_unmentioned_entity_or_short_uppercase_acronym_is_rejected(question, value):
    with pytest.raises(IntentCompileError) as error:
        count(question, FiltersRequest(sponsors=[value]))
    assert error.value.reason == "intent_nonliteral_filter"


def test_trusted_ui_value_stays_valid_without_a_question_mention():
    compiled, result, service = count("Count selected records", FiltersRequest(sponsors=["sable-loom"]),
                                      base=Filters(sponsors=["sable-loom"]))
    assert compiled.status == "ready" and result["status"] == "ok"
    assert service.reads[0].filters.sponsors == ["sable-loom"]


@pytest.mark.parametrize("dimension", ["accounts", "platforms", "keywords", "labels", "record_ids"])
def test_organization_alias_does_not_authorize_other_filter_dimensions(dimension):
    with pytest.raises(IntentCompileError) as error:
        count("Count SLI records", FiltersRequest(**{dimension: ["sable-loom"]}))
    assert error.value.reason == "intent_nonliteral_filter"


def test_outlet_alias_does_not_authorize_a_sponsor():
    with pytest.raises(IntentCompileError) as error:
        count("Count TF records", FiltersRequest(sponsors=["Teal Folio"]))
    assert error.value.reason == "intent_nonliteral_filter"


@pytest.mark.parametrize("dataset,expected", [
    ("native", ["sable-loom"]),
    ("social", ["Sable Loom Social"]),
    ("all", ["Sable Loom Social", "sable-loom"]),
])
def test_same_entity_expands_only_to_the_requested_collection(dataset, expected):
    _, result, service = count("Count SLI records", FiltersRequest(sponsors=["Sable Loom Industries"]),
                               dataset=dataset, base=Filters(dataset="all"))
    assert result["status"] == "ok"
    assert service.reads[0].filters.sponsors == expected
    assert service.reads[0].filters.dataset == dataset
    assert [row["dataset"] for row in result["collections"]] == (
        ["native", "social"] if dataset == "all" else [dataset])


def test_registered_mention_cannot_expand_active_entity_selection():
    _, result, service = count("Count SLI records", FiltersRequest(sponsors=["Sable Loom Industries"]),
                               base=Filters(sponsors=["copper-orchard"]))
    assert result["status"] == "clarify" and service.reads == []


def test_same_entity_alias_cannot_add_an_unselected_source_spelling():
    _, result, service = count("Count SLI records", FiltersRequest(sponsors=["Sable Loom Industries"]),
                               dataset="all", base=Filters(dataset="all", sponsors=["sable-loom"]))
    assert result["status"] == "ok"
    assert service.reads[0].filters.sponsors == ["sable-loom"]


def test_registered_mention_cannot_change_active_collection():
    with pytest.raises(IntentCompileError) as error:
        count("Count SLI records", FiltersRequest(sponsors=["Sable Loom Industries"]),
              dataset="social", base=Filters(dataset="native"))
    assert error.value.reason == "intent_dataset_conflict"


@pytest.mark.parametrize("name", ["Unregistered Carousel", "Hazel"])
def test_literal_unknown_or_ambiguous_source_still_requires_clarification(name):
    _, result, service = count(f"Count {name} records", FiltersRequest(sponsors=[name]))
    assert result["status"] == "clarify" and service.reads == []


def test_shared_and_denominator_aliases_reach_existing_share_scope():
    part, qualifier = "What share of TF records is from SLI", "during the current selection"
    question = f"{part}; {qualifier}"
    intent = count_intent(part, FiltersRequest(sponsors=["Sable Loom Industries"]), measure="share",
                          denominator_filters=FiltersRequest(publishers=["Teal Folio"]))
    intent.shared_constraints = [SharedConstraint(
        question_part=qualifier, applies_to=[0], filters=FiltersRequest(publishers=["Teal Folio"]))]
    task = compile_intent(intent, question, Filters()).tasks[0]
    service = FictionService()
    result = ToolCatalog(service, Filters()).call(task.tool_name, task.arguments)
    assert result["status"] == "ok"
    plan = service.reads[0]
    assert plan.filters.sponsors == ["sable-loom"]
    assert plan.filters.publishers == plan.denominator_filters.publishers == ["teal-folio"]
    assert plan.denominator_filters.sponsors == []


def test_evidence_comparison_aliases_keep_separate_source_scopes():
    question = "Compare SLI and CO wording"
    task = EvidenceTask(route="evidence", question_part=question, dataset="native", request=SearchRequest(
        query=question, comparison_scopes=[
            {"query": "wording", "filters": {"sponsors": ["Sable Loom Industries"]}},
            {"query": "wording", "filters": {"sponsors": ["Copper Orchard"]}},
        ]))
    compiled = compile_intent(QuestionIntent(status="ready", tasks=[task]), question, Filters())
    catalog = ToolCatalog(FictionService(), Filters())
    request = SearchRequest.model_validate(compiled.tasks[0].arguments)
    assert [catalog.narrow(item.filters).sponsors for item in request.comparison_scopes] == [
        ["sable-loom"], ["copper-orchard"],
    ]
