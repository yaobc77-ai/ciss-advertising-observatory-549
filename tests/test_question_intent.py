"""Independent fictional cases for the typed intent compiler, without model/DB calls."""

from datetime import date

import pytest
from pydantic import ValidationError

from observatory.models import Filters
from observatory.question_intent import (
    EvidenceTask,
    IntentCompileError,
    MetadataTask,
    QuestionIntent,
    RecordLocator,
    RecordTask,
    SharedConstraint,
    SourcesTask,
    StatisticsTask,
    compile_intent,
    intent_tool_definition,
)
from observatory.research_tools import (
    FiltersRequest,
    SearchRequest,
    StatisticsPeriod,
    StatisticsRequest,
)


def statistics(question, *, dataset="native", condition=None, **request):
    return StatisticsTask(question_part=question, dataset=dataset, route="statistics",
                          request=StatisticsRequest(**request), content_condition=condition)


def ready(*tasks, constraints=()):
    return QuestionIntent(status="ready", tasks=list(tasks), shared_constraints=list(constraints))


def test_period_comparison_is_one_existing_statistics_request():
    question = "Compare AlderWorks records in 2017 and 2020."
    task = statistics(question, filters=FiltersRequest(sponsors=["AlderWorks"]), periods=[
        StatisticsPeriod(label="early", date_from=date(2017, 1, 1), date_to=date(2017, 12, 31)),
        StatisticsPeriod(label="late", date_from=date(2020, 1, 1), date_to=date(2020, 12, 31)),
    ], measure="count")
    compiled = compile_intent(ready(task), question, Filters())
    assert compiled.status == "ready"
    assert len(compiled.tasks) == 1
    item = compiled.tasks[0]
    assert item.tool_name == "record_statistics"
    assert [period["date_from"] for period in item.arguments["periods"]] == ["2017-01-01", "2020-01-01"]
    assert item.arguments["filters"]["sponsors"] == ["AlderWorks"]


def test_share_preserves_distinct_named_numerator_and_denominator():
    question = "Within Paper Lantern records, what share are sponsored by AlderWorks?"
    task = statistics(question, measure="share", group_by="none",
                      filters=FiltersRequest(sponsors=["AlderWorks"]),
                      denominator_filters=FiltersRequest(publishers=["Paper Lantern"]))
    args = compile_intent(ready(task), question, Filters()).tasks[0].arguments
    assert args["filters"]["sponsors"] == ["AlderWorks"]
    assert args["denominator_filters"]["publishers"] == ["Paper Lantern"]
    assert args["measure"] == "share"


def test_shared_date_qualifier_applies_to_every_declared_read():
    prefix = "During 2019, "
    first, second = "count AlderWorks records", "list Paper Lantern sponsors"
    question = prefix + first + "; then " + second
    constraint = SharedConstraint(question_part=prefix, applies_to=[0, 1],
                                  filters=FiltersRequest(date_from=date(2019, 1, 1), date_to=date(2019, 12, 31)))
    compiled = compile_intent(ready(
        statistics(first, filters=FiltersRequest(sponsors=["AlderWorks"])),
        statistics(second, filters=FiltersRequest(publishers=["Paper Lantern"]), group_by="sponsors"),
        constraints=[constraint],
    ), question, Filters())
    assert compiled.status == "ready"
    assert len(compiled.tasks) == 2
    assert all(task.arguments["filters"]["date_from"] == "2019-01-01" for task in compiled.tasks)
    assert [item["kind"] for item in compiled.coverage] == ["task", "task", "shared"]


def test_common_share_qualifier_also_applies_to_comparison_group():
    qualifier, part = "During 2018, ", "show AlderWorks share"
    intent = ready(statistics(part, filters=FiltersRequest(sponsors=["AlderWorks"]), measure="share"),
                   constraints=[SharedConstraint(question_part=qualifier, applies_to=[0],
                                                 filters=FiltersRequest(date_from=date(2018, 1, 1)))])
    args = compile_intent(intent, qualifier + part, Filters()).tasks[0].arguments
    assert args["filters"]["date_from"] == args["denominator_filters"]["date_from"] == "2018-01-01"


def test_explanatory_qualifier_is_recorded_without_an_extra_read():
    part, note = "Read the collection search term", "This is a collection label."
    task = MetadataTask(route="metadata", dataset="native", question_part=part,
                        locator=RecordLocator(), fields=["collection_search_term"])
    shared = SharedConstraint(kind="interpretive_note", question_part=note, applies_to=[0])
    result = compile_intent(ready(task, constraints=[shared]), part + ". " + note,
                            Filters(record_ids=["native:fiction-1"]))
    assert result.status == "ready"
    assert len(result.tasks) == 1
    assert result.tasks[0].arguments["fields"] == ["collection_search_term"]


def test_title_metadata_defers_id_and_preserves_every_requested_field():
    question = 'Read publication date and publisher for "The Painted Kite".'
    task = MetadataTask(route="metadata", dataset="native", question_part=question,
                        locator=RecordLocator(title="The Painted Kite"), fields=["publication_date", "publisher"])
    item = compile_intent(ready(task), question, Filters()).tasks[0]
    assert item.title_lookup["title"] == "The Painted Kite"
    assert "record_id" not in item.arguments
    assert "pending-title-binding" not in repr(item)
    args = item.bind_record("native:fiction-8")
    assert args["record_id"] == "native:fiction-8"
    assert args["fields"] == ["publication_date", "publisher"]
    assert "record_id" not in item.arguments


@pytest.mark.parametrize("route,tool,task_class", [("record", "get_record", RecordTask),
                                                   ("sources", "get_record_sources", SourcesTask)])
def test_ui_singleton_record_is_bound_to_existing_request(route, tool, task_class):
    question = "Read the selected record"
    task = task_class(route=route, dataset="native", question_part=question, locator=RecordLocator())
    item = compile_intent(ready(task), question, Filters(record_ids=["native:fiction-6"])).tasks[0]
    assert item.tool_name == tool
    assert item.arguments["record_id"] == "native:fiction-6"
    assert item.title_lookup is None
    with pytest.raises(ValueError, match="pending title"):
        item.bind_record("native:fiction-7")


def test_multiple_ui_records_require_selection():
    question = "Read stored publisher"
    task = MetadataTask(route="metadata", dataset="native", question_part=question,
                        locator=RecordLocator(), fields=["publisher"])
    result = compile_intent(ready(task), question, Filters(record_ids=["native:a", "native:b"]))
    assert result.status == "clarify"
    assert result.failure_reason == "intent_record_unresolved"


@pytest.mark.parametrize("dimension", ["publishers", "sponsors", "platforms", "accounts", "keywords", "labels", "record_ids"])
def test_fabricated_filter_values_are_rejected(dimension):
    question = "Count selected records"
    value = "native:invented-fiction" if dimension == "record_ids" else "Invented Fiction Value"
    task = statistics(question, dataset="social", filters=FiltersRequest(**{dimension: [value]}))
    with pytest.raises(IntentCompileError) as error:
        compile_intent(ready(task), question, Filters(dataset="social"))
    assert error.value.reason == "intent_nonliteral_filter"


def test_short_name_inside_a_longer_latin_name_is_not_a_literal_binding():
    question = "Count Archer records"
    with pytest.raises(IntentCompileError, match="neither literal"):
        compile_intent(ready(statistics(question, filters=FiltersRequest(sponsors=["Arc"]))), question, Filters())


def test_literal_alias_is_left_raw_for_catalog_normalization():
    question = "Count ACW records"
    task = statistics(question, filters=FiltersRequest(sponsors=["acw"]))
    assert compile_intent(ready(task), question, Filters()).tasks[0].arguments["filters"]["sponsors"] == ["acw"]


def test_active_source_value_can_be_used_without_repeating_its_name():
    question = "Count selected records"
    task = statistics(question, filters=FiltersRequest(publishers=["Paper Lantern"]))
    assert compile_intent(ready(task), question, Filters(publishers=["Paper Lantern"])).status == "ready"


@pytest.mark.parametrize("second", ["count records with known dates", "show disclosure wording"])
def test_omitted_substantive_clause_is_rejected(second):
    first = "count AlderWorks records"
    with pytest.raises(IntentCompileError) as error:
        compile_intent(ready(statistics(first)), first + "; and " + second, Filters())
    assert error.value.reason == "intent_uncovered_question"


def test_task_order_and_overlap_are_rejected():
    question = "count records; read publisher"
    with pytest.raises(IntentCompileError):
        compile_intent(ready(statistics("read publisher"), statistics("count records")), question, Filters())
    with pytest.raises(IntentCompileError):
        compile_intent(ready(statistics(question), statistics("read publisher")), question, Filters())


def test_unknown_shared_qualifier_is_clarified_instead_of_dropped():
    part, qualifier = "count records", " excluding special cases"
    constraint = SharedConstraint(question_part=qualifier, applies_to=[0])
    result = compile_intent(ready(statistics(part), constraints=[constraint]), part + qualifier, Filters())
    assert result.status == "clarify"
    assert result.failure_reason == "intent_shared_constraint_unrepresented"


def test_conflicting_shared_filter_is_rejected():
    first, qualifier = "count AlderWorks records", " for BirchWorks"
    intent = ready(statistics(first, filters=FiltersRequest(sponsors=["AlderWorks"])),
                   constraints=[SharedConstraint(question_part=qualifier, applies_to=[0],
                                                 filters=FiltersRequest(sponsors=["BirchWorks"]))])
    with pytest.raises(IntentCompileError) as error:
        compile_intent(intent, first + qualifier, Filters())
    assert error.value.reason == "intent_shared_scope_conflict"


def test_content_conditioned_total_has_no_compiled_read():
    question = "Count AlderWorks records mentioning moon gardens"
    result = compile_intent(ready(statistics(question, condition="mentioning moon gardens")), question, Filters())
    assert result.status == "unsupported"
    assert not result.tasks
    assert result.failure_reason == "intent_content_statistics_unsupported"


@pytest.mark.parametrize("coverage", ["complete_list", "content_count"])
def test_exhaustive_content_requests_do_not_become_retrieved_examples(coverage):
    question = "Find all moon garden articles"
    task = EvidenceTask(route="evidence", dataset="native", question_part=question,
                        request=SearchRequest(query="moon garden"), coverage=coverage)
    result = compile_intent(ready(task), question, Filters())
    assert result.status == "unsupported"
    assert not result.tasks


def test_mixed_evidence_composite_is_explicitly_unsupported():
    first, second = "count records", "find moon garden articles"
    evidence = EvidenceTask(route="evidence", dataset="native", question_part=second,
                            request=SearchRequest(query="moon garden"))
    result = compile_intent(ready(statistics(first), evidence), first + "; then " + second, Filters())
    assert result.failure_reason == "intent_evidence_composite_unsupported"
    assert not result.tasks


def test_comparison_scopes_keep_shared_qualifier():
    qualifier, part = "During 2016, ", "compare AlderWorks and BirchWorks wording"
    task = EvidenceTask(route="evidence", dataset="native", question_part=part,
                        request=SearchRequest(query="wording", comparison_scopes=[
                            {"query": "wording", "filters": {"sponsors": ["AlderWorks"]}},
                            {"query": "wording", "filters": {"sponsors": ["BirchWorks"]}},
                        ]))
    shared = SharedConstraint(question_part=qualifier, applies_to=[0], filters=FiltersRequest(date_from=date(2016, 1, 1)))
    args = compile_intent(ready(task, constraints=[shared]), qualifier + part, Filters()).tasks[0].arguments
    assert all(item["filters"]["date_from"] == "2016-01-01" for item in args["comparison_scopes"])


def test_model_cannot_change_dataset_or_source_date_basis():
    question = "Count selected records"
    with pytest.raises(IntentCompileError) as error:
        compile_intent(ready(statistics(question, dataset="social")), question, Filters(dataset="native"))
    assert error.value.reason == "intent_dataset_conflict"
    with pytest.raises(IntentCompileError) as error:
        compile_intent(ready(statistics(question, filters=FiltersRequest(include_inferred_dates=True))), question, Filters())
    assert error.value.reason == "intent_date_basis_conflict"


@pytest.mark.parametrize("task_dataset,requested_dataset,base_dataset", [
    ("all", None, "native"), ("all", None, "social"),
    ("native", "all", "all"), ("native", "social", "all"),
])
def test_explicit_collection_scope_is_never_silently_clipped(task_dataset, requested_dataset, base_dataset):
    question = "Count selected records"
    task = statistics(question, dataset=task_dataset, filters=FiltersRequest(dataset=requested_dataset))
    with pytest.raises(IntentCompileError) as error:
        compile_intent(ready(task), question, Filters(dataset=base_dataset))
    assert error.value.reason == "intent_dataset_conflict"


def test_all_selection_keeps_explicit_units_and_dataset_in_request():
    question = "Count native and social records"
    result = compile_intent(ready(statistics(question, dataset="all")), question, Filters(dataset="all"))
    assert result.tasks[0].dataset == "all"
    assert result.tasks[0].arguments["filters"]["dataset"] == "all"


def test_clarification_accounts_for_original_text_and_executes_nothing():
    question = "Count records last winter"
    intent = QuestionIntent(status="clarify", message="Specify calendar endpoints", unresolved_constraints=[question])
    result = compile_intent(intent, question, Filters())
    assert result.status == "clarify"
    assert not result.tasks
    assert result.coverage[0]["text"] == question


def test_strict_schema_and_models_exclude_arbitrary_tool_sql_or_findings():
    definition = intent_tool_definition()
    assert definition["strict"] is True
    assert definition["name"] == "interpret_question"
    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node.get("properties", {}))
            for child in node.values():
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)
    check(definition["parameters"])
    for field in ("sql", "tool_name", "findings"):
        with pytest.raises(ValidationError):
            QuestionIntent.model_validate({"status": "ready", "tasks": [], field: "invented"})


def test_invalid_metadata_options_are_rejected_before_a_title_lookup():
    with pytest.raises(ValidationError):
        MetadataTask(route="metadata", dataset="native", question_part="read publisher",
                     locator=RecordLocator(title="Fiction"), fields=["publisher", "publisher"])
    question = "Read The Painted Kite"
    task = RecordTask(route="record", dataset="native", question_part=question,
                      locator=RecordLocator(title="The Painted Kite"), body_limit=12001)
    with pytest.raises(ValidationError):
        compile_intent(ready(task), question, Filters())


def test_interpretive_note_cannot_carry_a_hidden_scope_filter():
    with pytest.raises(ValidationError, match="cannot silently impose"):
        SharedConstraint(kind="interpretive_note", question_part="only dates after 2020", applies_to=[0],
                         filters=FiltersRequest(date_from=date(2020, 1, 1)))


def test_title_and_record_observation_cannot_be_invented():
    question = "Read the selected record"
    task = RecordTask(route="record", dataset="native", question_part=question,
                      locator=RecordLocator(title="Missing Fiction Title"))
    with pytest.raises(IntentCompileError) as error:
        compile_intent(ready(task), question, Filters())
    assert error.value.reason == "intent_nonliteral_title"
    task = RecordTask(route="record", dataset="social", question_part=question,
                      locator=RecordLocator(), source_observation_id="junkipedia:9990001")
    with pytest.raises(IntentCompileError) as error:
        compile_intent(ready(task), question, Filters(dataset="social", record_ids=["social:fiction"]))
    assert error.value.reason == "intent_nonliteral_observation"


def test_composite_cannot_exceed_three_tasks():
    with pytest.raises(ValidationError):
        ready(*(statistics("count records") for _ in range(4)))


def test_shared_content_reaches_each_comparison_retrieval_query():
    question = "Compare Aurora Utilities and Cedar Energy; discuss proposed storage upgrades"
    task = EvidenceTask(
        route="evidence", question_part="Compare Aurora Utilities and Cedar Energy", dataset="native",
        request=SearchRequest(query="compare projects", comparison_scopes=[
            {"query": "Aurora Utilities projects", "filters": {"sponsors": ["Aurora Utilities"]}},
            {"query": "Cedar Energy projects", "filters": {"sponsors": ["Cedar Energy"]}},
        ]),
    )
    intent = ready(task)
    intent.shared_constraints = [SharedConstraint(
        question_part="discuss proposed storage upgrades", applies_to=[0],
        content_condition="proposed storage upgrades",
    )]
    compiled = compile_intent(intent, question, Filters(dataset="native"))
    args = compiled.tasks[0].arguments
    assert args["query"] == question
    assert [item["query"] for item in args["comparison_scopes"]] == [question, question]
    assert [item["filters"]["sponsors"] for item in args["comparison_scopes"]] == [
        ["Aurora Utilities"], ["Cedar Energy"],
    ]
