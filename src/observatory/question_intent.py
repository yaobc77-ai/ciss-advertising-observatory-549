"""Bounded question meaning compiled to existing read-only tool requests.

The schema records an interpretation, not proof that a model understood it.
Literal coverage detects omitted text; it cannot prove a predicate was mapped
correctly. Canonical entities, scope intersection, counting units and source
versions remain the existing catalog/service's authority.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .models import Filters
from .research_tools import (
    FiltersRequest,
    FindRecordsRequest,
    RecordMetadataRequest,
    RecordRequest,
    RecordTextRequest,
    SearchRequest,
    StatisticsRequest,
    strict_schema,
)

INTENT_VERSION = "question-intent-v1"
Dataset = Literal["native", "social", "all"]
Text = Annotated[str, Field(min_length=1, max_length=2000)]
MetadataField = Literal[
    "title", "publisher", "sponsor", "original_url", "publication_date",
    "collection_search_term", "disclosure_language", "disclosure_location",
]


class IntentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class IntentTask(IntentModel):
    question_part: Text
    dataset: Dataset


class StatisticsTask(IntentTask):
    route: Literal["statistics"]
    request: StatisticsRequest
    content_condition: Text | None = None


class EvidenceTask(IntentTask):
    route: Literal["evidence"]
    request: SearchRequest
    coverage: Literal["retrieved_examples", "complete_list", "content_count"] = "retrieved_examples"


class RecordLocator(IntentModel):
    title: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    record_id: Annotated[str, Field(min_length=1, max_length=200)] | None = None

    @model_validator(mode="after")
    def one_locator(self):
        if self.title is not None and self.record_id is not None:
            raise ValueError("Use a literal title or a record ID, not both")
        if self.title is not None and not self.title.strip():
            raise ValueError("A stored title cannot be blank")
        return self


class MetadataTask(IntentTask):
    route: Literal["metadata"]
    locator: RecordLocator
    filters: FiltersRequest | None = None
    fields: Annotated[list[MetadataField], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def distinct_fields(self):
        if len(self.fields) != len(set(self.fields)):
            raise ValueError("Request each original field once")
        return self


class RecordTask(IntentTask):
    route: Literal["record"]
    locator: RecordLocator
    filters: FiltersRequest | None = None
    source_observation_id: str | None = None
    body_start: StrictInt | None = None
    body_limit: StrictInt | None = None


class SourcesTask(IntentTask):
    route: Literal["sources"]
    locator: RecordLocator
    filters: FiltersRequest | None = None


Task = StatisticsTask | EvidenceTask | MetadataTask | RecordTask | SourcesTask


class SharedConstraint(IntentModel):
    kind: Literal["scope", "interpretive_note"] = "scope"
    question_part: Text
    applies_to: Annotated[list[Annotated[StrictInt, Field(ge=0, le=2)]], Field(min_length=1, max_length=3)]
    filters: FiltersRequest | None = None
    content_condition: Text | None = None

    @model_validator(mode="after")
    def note_is_not_scope(self):
        if self.kind == "interpretive_note" and (self.filters is not None or self.content_condition is not None):
            raise ValueError("An interpretive note cannot silently impose a database condition")
        return self


class QuestionIntent(IntentModel):
    schema_version: Literal["question-intent-v1"] = INTENT_VERSION
    status: Literal["ready", "clarify", "unsupported"]
    tasks: Annotated[list[Task], Field(max_length=3)] = Field(default_factory=list)
    shared_constraints: Annotated[list[SharedConstraint], Field(max_length=8)] = Field(default_factory=list)
    unresolved_constraints: Annotated[list[Text], Field(max_length=8)] = Field(default_factory=list)
    message: Text | None = None

    @model_validator(mode="after")
    def status_contract(self):
        if self.status == "ready" and (not self.tasks or self.unresolved_constraints):
            raise ValueError("A ready intent requires tasks and no unresolved constraints")
        if self.status != "ready" and not self.message:
            raise ValueError("Clarification or unsupported intent needs a message")
        return self


_RECORD_MODELS = {
    "metadata": RecordMetadataRequest,
    "record": RecordTextRequest,
    "sources": RecordRequest,
}


@dataclass(frozen=True)
class CompiledTask:
    route: str
    question_part: str
    dataset: str
    tool_name: str
    arguments: dict
    title_lookup: dict | None = None

    def bind_record(self, record_id: str) -> dict:
        """Bind a server-checked title result; executor verifies its version/hash."""
        model = _RECORD_MODELS.get(self.route)
        if model is None or self.title_lookup is None:
            raise ValueError("Only a pending title read accepts a record binding")
        return model.model_validate({**deepcopy(self.arguments), "record_id": record_id}).model_dump(
            mode="json", exclude_none=True
        )


@dataclass(frozen=True)
class CompiledIntent:
    status: str
    tasks: tuple[CompiledTask, ...] = ()
    message: str = ""
    coverage: tuple[dict, ...] = ()
    failure_reason: str = ""
    schema_version: str = field(default=INTENT_VERSION, init=False)


class IntentCompileError(ValueError):
    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


def intent_tool_definition():
    return {
        "type": "function", "name": "interpret_question", "strict": True,
        "description": "Describe every requested task and shared qualifier using literal user text; no SQL or answers.",
        "parameters": strict_schema(QuestionIntent.model_json_schema()),
    }


# Only these exact joining words/punctuation can be omitted by the interpreter.
# This is a syntactic coverage allowance, never an intent or entity parser.
_CONNECTIVES = {"and", "also", "then", "plus", "as", "well", "和", "及", "以及", "并且", "同时", "还有", "再"}


def _literal_coverage(intent: QuestionIntent, question: str):
    covered = [False] * len(question)
    spans = []
    previous_end = 0
    for task in intent.tasks:
        start = question.find(task.question_part, previous_end)
        if start < 0:
            raise IntentCompileError("intent_nonliteral_part", "Task parts must be verbatim, disjoint and in question order")
        end = start + len(task.question_part)
        spans.append({"kind": "task", "start": start, "end": end, "text": task.question_part})
        covered[start:end] = [True] * (end - start)
        previous_end = end
    qualifiers = [(item.question_part, "shared") for item in intent.shared_constraints]
    qualifiers.extend((text, "unresolved") for text in intent.unresolved_constraints)
    for text, kind in qualifiers:
        start = question.find(text)
        if start < 0 or question.find(text, start + 1) >= 0:
            raise IntentCompileError("intent_nonliteral_constraint", "Use an unambiguous literal span for each qualifier")
        end = start + len(text)
        spans.append({"kind": kind, "start": start, "end": end, "text": text})
        covered[start:end] = [True] * (end - start)
    gaps = []
    token = ""
    for index, char in enumerate(question + " "):
        if index < len(question) and not covered[index] and char.isalnum():
            token += char
        elif token:
            gaps.append(token.casefold())
            token = ""
    if any(word not in _CONNECTIVES for word in gaps):
        raise IntentCompileError("intent_uncovered_question", "The intent omitted substantive question text")
    return tuple(spans)


def _has_literal(value: str, question: str) -> bool:
    """Case-insensitive spelling with Latin/digit token boundaries, no aliases."""
    value, text = value.casefold(), question.casefold()
    start = text.find(value)
    latin = "abcdefghijklmnopqrstuvwxyz0123456789_"
    while start >= 0:
        end = start + len(value)
        left_ok = value[0] not in latin or start == 0 or text[start - 1] not in latin
        right_ok = value[-1] not in latin or end == len(text) or text[end] not in latin
        if left_ok and right_ok:
            return True
        start = text.find(value, start + 1)
    return False


def _check_literal_names(filters: FiltersRequest | None, question: str, base: Filters):
    if filters is None:
        return
    for dimension in ("publishers", "sponsors", "platforms", "accounts", "keywords", "labels", "record_ids"):
        trusted = {value.casefold() for value in getattr(base, dimension)}
        for value in getattr(filters, dimension) or []:
            if not _has_literal(value, question) and value.casefold() not in trusted:
                raise IntentCompileError("intent_nonliteral_filter", f"The {dimension} filter is neither literal user text nor an active value")


def _merge_constraints(filters: FiltersRequest | None, shared: list[SharedConstraint]):
    values = filters.model_dump(exclude_none=True) if filters else {}
    for constraint in shared:
        for key, value in (constraint.filters.model_dump(exclude_none=True) if constraint.filters else {}).items():
            current = values.get(key)
            if current is not None and current != value:
                if key == "date_from":
                    value = max(current, value)
                elif key == "date_to":
                    value = min(current, value)
                else:
                    raise IntentCompileError("intent_shared_scope_conflict", "A shared qualifier conflicts with a task's declared scope")
            values[key] = value
    return FiltersRequest.model_validate(values)


def _task_scope(requested: FiltersRequest | None, task_dataset: str, base: Filters):
    effective = task_dataset
    if base.dataset != "all" and effective != base.dataset:
        raise IntentCompileError("intent_dataset_conflict", "The requested collection is outside the active selection")
    filters = requested.model_copy(deep=True) if requested else FiltersRequest()
    if filters.dataset is not None and filters.dataset != effective:
        raise IntentCompileError("intent_dataset_conflict", "Task and request collections disagree")
    filters.dataset = effective
    if filters.include_inferred_dates is not None and filters.include_inferred_dates != base.include_inferred_dates:
        raise IntentCompileError("intent_date_basis_conflict", "A model cannot change the active publication-date basis")
    if filters.date_from and filters.date_to and filters.date_from > filters.date_to:
        raise IntentCompileError("intent_date_range_conflict", "The declared date range has no intersection")
    return effective, filters


def compile_intent(intent: QuestionIntent, question: str, base_filters: Filters) -> CompiledIntent:
    """Compile structural meaning; catalog performs canonicalization/narrowing.

    Metadata field selection and date/group/predicate interpretation are model
    declarations. Literal spans preserve their source, not semantic correctness.
    """
    intent = QuestionIntent.model_validate(intent)
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
        raise IntentCompileError("intent_question_invalid", "A question of 1-2000 characters is required")
    coverage = _literal_coverage(intent, question)
    if intent.status != "ready":
        return CompiledIntent(intent.status, message=intent.message or "", coverage=coverage)
    if len(intent.tasks) > 1 and any(task.route == "evidence" for task in intent.tasks):
        return CompiledIntent("unsupported", message="Separate evidence generation cannot be combined with other read tasks.",
                              coverage=coverage, failure_reason="intent_evidence_composite_unsupported")
    for constraint in intent.shared_constraints:
        if len(set(constraint.applies_to)) != len(constraint.applies_to) or any(index >= len(intent.tasks) for index in constraint.applies_to):
            raise IntentCompileError("intent_shared_target_invalid", "A shared qualifier must name distinct existing tasks")
        if (constraint.kind == "scope" and not constraint.content_condition
                and not (constraint.filters and constraint.filters.model_dump(exclude_none=True))):
            return CompiledIntent("clarify", message="A shared qualifier has not been mapped to a supported condition.",
                                  coverage=coverage, failure_reason="intent_shared_constraint_unrepresented")
        if constraint.content_condition and constraint.content_condition not in constraint.question_part:
            raise IntentCompileError("intent_nonliteral_condition", "Content predicates must be literal qualifier text")
        _check_literal_names(constraint.filters, question, base_filters)
    compiled = []
    for index, task in enumerate(intent.tasks):
        shared = [constraint for constraint in intent.shared_constraints if index in constraint.applies_to]
        request = task.request if isinstance(task, (StatisticsTask, EvidenceTask)) else None
        raw_filters = request.filters if request else task.filters
        _check_literal_names(raw_filters, question, base_filters)
        dataset, filters = _task_scope(_merge_constraints(raw_filters, shared), task.dataset, base_filters)
        if isinstance(task, StatisticsTask):
            if task.content_condition and task.content_condition not in task.question_part:
                raise IntentCompileError("intent_nonliteral_condition", "A content predicate must be literal task text")
            if task.content_condition or any(item.content_condition for item in shared):
                return CompiledIntent("unsupported", message="Content-conditioned totals require a separately reviewed membership dataset.",
                                      coverage=coverage, failure_reason="intent_content_statistics_unsupported")
            denominator = request.denominator_filters
            if request.measure == "share" and any(item.filters for item in shared):
                denominator = _merge_constraints(denominator, shared)
            _check_literal_names(denominator, question, base_filters)
            if denominator is not None:
                _, denominator = _task_scope(denominator, dataset, base_filters)
            validated = StatisticsRequest.model_validate({**request.model_dump(), "filters": filters,
                                                          "denominator_filters": denominator})
            args = validated.model_dump(mode="json", exclude_none=True)
            name, lookup = "record_statistics", None
        elif isinstance(task, EvidenceTask):
            if task.coverage != "retrieved_examples":
                return CompiledIntent("unsupported", message="Retrieved passages establish examples, not exhaustive content lists or counts.",
                                      coverage=coverage, failure_reason="intent_exhaustive_evidence_unsupported")
            if request.comparison_scopes:
                comparisons = []
                for item in request.comparison_scopes:
                    _check_literal_names(item.filters, question, base_filters)
                    _, scope = _task_scope(_merge_constraints(item.filters, shared), dataset, base_filters)
                    # Comparison reads use each group's own query. Declared common
                    # content conditions must reach those reads, not only the outer
                    # query that the comparison executor does not search.
                    comparisons.append(item.model_copy(update={
                        "filters": scope,
                        "query": question if any(entry.content_condition for entry in shared) else item.query,
                    }))
            else:
                comparisons = None
            # A shared content qualifier survives verbatim in retrieval as well.
            query = question if any(item.content_condition for item in shared) else request.query
            validated = SearchRequest.model_validate({**request.model_dump(), "filters": filters,
                                                       "comparison_scopes": comparisons, "query": query})
            args, name, lookup = validated.model_dump(mode="json", exclude_none=True), "search_records", None
        else:
            if any(item.content_condition for item in shared):
                return CompiledIntent("unsupported", message="Content conditions cannot select an exact record metadata read.",
                                      coverage=coverage, failure_reason="intent_record_content_condition_unsupported")
            locator = task.locator
            record_id = locator.record_id
            title = locator.title
            if title is not None and title not in task.question_part:
                raise IntentCompileError("intent_nonliteral_title", "Use the literal stored title from this task")
            if record_id is not None and not _has_literal(record_id, question) and record_id not in base_filters.record_ids:
                raise IntentCompileError("intent_untrusted_record", "A record ID must be stated or selected in the active UI")
            if record_id is not None and base_filters.record_ids and record_id not in base_filters.record_ids:
                raise IntentCompileError("intent_record_scope_conflict", "The named record is outside the selected IDs")
            if title is None and record_id is None and len(base_filters.record_ids) == 1:
                record_id = base_filters.record_ids[0]
            if title is None and record_id is None:
                return CompiledIntent("clarify", message="Select one record or supply its literal stored title.",
                                      coverage=coverage, failure_reason="intent_record_unresolved")
            args = {"filters": filters.model_dump(mode="json", exclude_none=True)}
            if isinstance(task, MetadataTask):
                args["fields"] = list(task.fields)
            elif isinstance(task, RecordTask):
                if task.source_observation_id is not None and not _has_literal(task.source_observation_id, question):
                    raise IntentCompileError("intent_nonliteral_observation", "Use a literal source observation ID")
                args.update({key: getattr(task, key) for key in ("source_observation_id", "body_start", "body_limit")
                             if getattr(task, key) is not None})
            model = _RECORD_MODELS[task.route]
            if record_id is not None:
                args = model.model_validate({**args, "record_id": record_id}).model_dump(mode="json", exclude_none=True)
                lookup = None
            else:
                # Validate non-ID options now; the temporary ID is never exposed.
                model.model_validate({**args, "record_id": "pending-title-binding"})
                lookup = FindRecordsRequest(title=title, filters=filters).model_dump(mode="json", exclude_none=True)
            name = {"metadata": "get_record_metadata", "record": "get_record", "sources": "get_record_sources"}[task.route]
        compiled.append(CompiledTask(task.route, task.question_part, dataset, name, args, lookup))
    return CompiledIntent("ready", tuple(compiled), coverage=coverage)
