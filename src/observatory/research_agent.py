"""Bounded model-first selection of the Observatory's read-only research tools.

The language model chooses a tool and its arguments; it never supplies the
displayed count or a generated replacement for a database/graph result. The
same catalog is exposed by the optional MCP server. Function calling is the
web application's transport, so no remote MCP server or account is required.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .budget import LimitReached, price
from .models import Filters
from .prompts import RESEARCH_SYSTEM as SYSTEM
from .question_policy import (
    effective_dataset_scope,
    entity_dates_used_as_filters,
    metadata_content_clarification,
    question_contract,
    statistics_request_preserves_question,
)

POLICY_VERSION = "research-tools-v14"
MAX_INPUT_BYTES = 60_000
MAX_ARGUMENT_BYTES = 8_000
MAX_INTERMEDIATE_BYTES = 16_000
MAX_RESULT_BYTES = 120_000

TERMINAL_ROUTES = {
    "record_statistics": "statistics",
    "search_records": "evidence",
    "get_graph_neighborhood": "graph",
    "get_record_sources": "sources",
    "get_record": "record",
    "get_record_metadata": "metadata",
    "get_claims_matches": "claims",
}
INTERMEDIATE_TOOLS = {"resolve_entity", "get_graph_schema", "find_records"}

# The compound renderer combines checked read data, not generated content.
# Evidence remains a single search_records read followed by grounded generation.
PLAN_ROUTES = Literal["statistics", "graph", "sources", "record", "metadata", "claims"]


class _ResearchTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question_part: str = Field(min_length=1, max_length=2_000)
    dataset: Literal["native", "social", "all"]
    route: PLAN_ROUTES


class _ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tasks: list[_ResearchTask] = Field(min_length=2, max_length=3)


PLAN_TOOL = {
    "type": "function", "name": "set_research_plan", "strict": True,
    "description": "Declare 2–3 distinct source/database read tasks before executing them. Supported routes are statistics, graph, sources, record, metadata and claims. Evidence search is a single final read, not a compound-plan route. Each question_part is verbatim user text; all parts must cover the question. No generated answer text or counts.",
    "parameters": _ResearchPlan.model_json_schema(),
}


def _validate_plan(args, question, scope):
    plan = _ResearchPlan.model_validate(args)
    covered = [False] * len(question)
    seen = set()
    previous_end = 0
    for task in plan.tasks:
        start = question.find(task.question_part)
        if start < 0 or task.question_part in seen or scope.dataset not in {"all", task.dataset}:
            raise ValueError("Plan parts must preserve the literal question and active dataset")
        end = start + len(task.question_part)
        if any(covered[start:end]):
            raise ValueError("Plan question parts must not overlap")
        if start < previous_end:
            raise ValueError("Plan parts must preserve question order")
        previous_end = end
        explicit = question_contract(task.question_part).get("explicit_datasets", [])
        if explicit and task.dataset != ("all" if len(explicit) > 1 else explicit[0]):
            raise ValueError("Plan dataset differs from the stated collection")
        seen.add(task.question_part)
        for index in range(start, start + len(task.question_part)):
            covered[index] = True
    unused = ''.join(char if not covered[index] else ' ' for index, char in enumerate(question))
    unused = re.sub(r"\b(?:and|also|then|as|well|plus)\b|以及|并且|同时|还有|和|及", "", unused, flags=re.I)
    if any(char.isalnum() for char in unused):
        raise ValueError("Plan omitted part of the question")
    return plan.tasks


def _step_definitions(definitions, contract, tasks, completed, selected_record=None, trusted_record_id=None):
    """Expose only established workflow choices, without guessing an intent.

    Unknown single questions keep the full catalog. A validated plan supplies
    the current route; a strong original-field contract cannot become a search.
    The executor independently checks the advertised name before any read.
    """
    allowed = None
    if tasks:
        route = tasks[len(completed)].route
        allowed = {name for name, value in TERMINAL_ROUTES.items() if value == route}
        allowed.update({"resolve_entity", "find_records", "request_clarification"})
        if route == "graph":
            allowed.add("get_graph_schema")
    elif contract.get("compound_read_request"):
        allowed = {"set_research_plan", "resolve_entity", "find_records",
                   "get_graph_schema", "request_clarification"}
    elif contract.get("original_metadata_only"):
        allowed = {"find_records", "resolve_entity", "get_record_metadata", "request_clarification"}
    return [item for item in definitions if (allowed is None or item["name"] in allowed)
            and not (selected_record and item["name"] == "find_records")
            and not (item["name"] == "get_record_metadata" and not (selected_record or trusted_record_id))]


def _selected_record(result):
    """An exact title selection is evidence for an ID, not a model suggestion."""
    records = result.get("records") or []
    if result.get("total_candidates") != 1 or len(records) != 1:
        raise ValueError("A unique title result needs exactly one bound source record")
    record = records[0]
    if not all(record.get(key) for key in ("record_id", "dataset", "version_id", "body_hash")):
        raise ValueError("A title selection requires record, dataset, version and body hash")
    return {key: record[key] for key in ("record_id", "dataset", "version_id", "body_hash")}


def _selection_current(result, selected):
    """Compare the current record identity separately from selected observations."""
    record = result.get("record") or {}
    return all(record.get(key) == value for key, value in selected.items())


def research_failure_status(reason):
    """Keep failed execution distinct from an unresolved reading task."""
    if reason == "research_budget_limit":
        return "limited"
    if reason in {
        "research_context_limit", "research_model_configuration", "research_budget_unavailable",
        "research_provider_unavailable", "research_response_incomplete", "research_tool_call_invalid",
        "research_tool_configuration", "research_tool_unavailable", "research_tool_invalid_request",
        "research_intent_invalid",
        "research_tool_invalid_result", "research_clarification_invalid", "research_output_invalid",
        "statistics_scope_validation_unavailable", "research_title_binding_invalid",
        "research_record_binding_mismatch", "research_record_version_changed",
        "research_plan_result_scope_mismatch", "research_result_scope_mismatch",
        "research_plan_result_predicate_missing", "statistics_result_scope_missing",
        "research_step_limit", "research_intermediate_limit", "research_plan_interrupted",
    }:
        return "service_unavailable"
    return None


def _complete_plan(run, tasks, completed, reason="", *, failure_status=None):
    clarification = run.result.get("message", "") if run.result.get("status") == "clarify" else ""
    run.route = "composite"
    run.failure_reason = reason
    run.result = {"status": "ok", "kind": "composite", "complete": len(completed) == len(tasks) and not reason,
                  "parts": completed, "pending_parts": [task.model_dump() for task in tasks[len(completed):]],
                  "failure_reason": reason,
                  "failure_status": failure_status or research_failure_status(reason),
                  "failure_message": clarification,
                  "completion_basis": "declared_literal_question_parts_and_checked_tool_results"}
    return run


def _result_dataset(result):
    return ((result.get("filters") or {}).get("dataset")
            or (result.get("record") or {}).get("dataset"))


def _result_preserves_active_filters(result_filters, base_filters, requested_filters):
    """Check returned filters directly; clipping must not hide a widened read."""
    if not isinstance(result_filters, dict):
        return False
    for dimension in ("publishers", "sponsors", "platforms", "accounts", "keywords", "labels", "record_ids"):
        active = base_filters.get(dimension) or []
        actual = result_filters.get(dimension) or []
        if not isinstance(actual, list) or not all(isinstance(value, str) for value in actual):
            return False
        if active and (not actual or not set(actual).issubset(active)):
            return False
        if dimension in {"platforms", "keywords", "labels", "record_ids"}:
            expected = requested_filters.get(dimension) or active
            if set(actual) != set(expected):
                return False
    lower, upper = result_filters.get("date_from"), result_filters.get("date_to")
    if any(value is not None and not isinstance(value, str) for value in (lower, upper)):
        return False
    if base_filters.get("date_from") and (not lower or lower < base_filters["date_from"]):
        return False
    if base_filters.get("date_to") and (not upper or upper > base_filters["date_to"]):
        return False
    if lower and upper and lower > upper:
        return False
    if (not base_filters.get("include_unknown_dates", True)
            and result_filters.get("include_unknown_dates", True)):
        return False
    if result_filters.get("include_inferred_dates", base_filters.get("include_inferred_dates")) != base_filters.get("include_inferred_dates"):
        return False
    presence = base_filters.get("date_presence", "any")
    return presence == "any" or result_filters.get("date_presence") == presence


class _Clarification(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    message: str = Field(min_length=1, max_length=500)
    reason: Literal[
        "ambiguous_entity", "missing_context", "unsupported_analysis", "compound_question"
    ]


CLARIFICATION_TOOL = {
    "type": "function",
    "name": "request_clarification",
    "description": (
        "Stop and ask one concise clarification in the question's language when a "
        "necessary scope or entity is ambiguous, or no responsive source-grounded read is available. "
        "A missing detail alone does not block reading supported requested parts with explicit limits. Never "
        "include guessed counts, findings, URLs or claims."
    ),
    "parameters": _Clarification.model_json_schema(),
    "strict": True,
}


@dataclass
class ResearchRun:
    """Only tool-derived data is promoted to a final research route."""

    route: Literal[
        "statistics", "evidence", "graph", "sources", "record", "metadata", "claims", "content_matches", "composite",
        "clarify", "unavailable", "limited",
    ]
    result: dict[str, Any] = field(default_factory=dict)
    original_question: str = ""
    base_filters: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    failure_reason: str = ""
    question_contract: dict[str, Any] = field(default_factory=dict)
    interpretation: dict[str, Any] | None = None

    def audit(self):
        audit = {
            "policy": POLICY_VERSION,
            "transport": "responses_function_calling",
            "original_question": self.original_question,
            "base_filters": self.base_filters,
            "route": self.route,
            "cost_usd": self.cost_usd,
            "model_calls": self.model_calls,
            "tools": self.tool_trace,
            "failure_reason": self.failure_reason,
            "question_contract": self.question_contract,
        }
        if self.interpretation is not None:
            audit["interpretation"] = self.interpretation
        return audit


class _CallFailure(Exception):
    def __init__(self, reason, *, cost=0.0, audit=None, limited=False):
        self.reason = reason
        self.cost = cost
        self.audit = audit
        self.limited = limited


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _dump(value):
    if isinstance(value, dict):
        return value
    return value.model_dump(exclude_none=True)


def _invalid_constant(_value):
    raise ValueError("Non-finite JSON arguments are not supported")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON argument keys")
        result[key] = value
    return result


def _source_refs(result):
    refs = result.get("source_refs") or result.get("data_refs")
    if refs:
        return refs
    rows = result.get("records") or ([result["record"]] if result.get("record") else [])
    return [{key: row[key] for key in ("record_id", "version_id", "dataset") if key in row}
            for row in rows if isinstance(row, dict)]


def _require_strict_objects(schema):
    """Fail locally when a transport schema could quietly become best effort."""
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            props = schema.get("properties", {})
            if (
                schema.get("additionalProperties") is not False
                or set(schema.get("required", [])) != set(props)
            ):
                raise ValueError("Function schemas must require every property")
        for value in schema.values():
            _require_strict_objects(value)
    elif isinstance(schema, list):
        for value in schema:
            _require_strict_objects(value)


def _usage(response):
    usage = _field(response, "usage")
    if usage is None:
        raise ValueError("Provider omitted usage")
    input_tokens = _field(usage, "input_tokens")
    output_tokens = _field(usage, "output_tokens")
    details = _field(usage, "input_tokens_details")
    cached = _field(details, "cached_tokens", 0) or 0
    writes = _field(details, "cache_write_tokens", 0) or 0
    if any(type(n) is not int or n < 0 for n in (input_tokens, output_tokens, cached, writes)):
        raise ValueError("Invalid provider usage")
    if cached + writes > input_tokens:
        raise ValueError("Invalid cached token usage")
    return input_tokens, output_tokens, cached, writes, _dump(usage)


class ResearchAgent:
    def __init__(
        self, rag, catalog, *, entity_context=None, reference_date=None,
        max_steps=4, max_tool_calls=4, max_output_tokens=900,
        intent_enabled=False,
    ):
        if not 1 <= max_steps <= 4 or not 1 <= max_tool_calls <= 4:
            raise ValueError("Research agent permits at most four steps and tool calls")
        if not 100 <= max_output_tokens <= 1_600:
            raise ValueError("Research output token limit is outside app bounds")
        self.rag = rag
        self.catalog = catalog
        self.entity_context = entity_context
        self.reference_date = reference_date
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.max_output_tokens = max_output_tokens
        self.intent_enabled = intent_enabled

    def _definitions(self):
        # Held-out question-specific tools are never sent to the general planner,
        # including during evaluation of one query. No full test catalog is used.
        definitions = [item for item in self.catalog.definitions()
                       if item.get("name") != "get_content_matches"]
        names = set()
        allowed = set(TERMINAL_ROUTES) | INTERMEDIATE_TOOLS
        for definition in definitions:
            name = definition.get("name")
            if (
                definition.get("type") != "function"
                or definition.get("strict") is not True
                or name not in allowed
                or name in names
            ):
                raise ValueError("Unexpected research tool definition")
            _require_strict_objects(definition["parameters"])
            names.add(name)
        return [*definitions, PLAN_TOOL, CLARIFICATION_TOOL]

    def _dispatch(self, inputs, definitions, visitor, step, *, policy=POLICY_VERSION):
        model = self.rag.settings.generation_model
        # A UTF-8-byte tokenizer upper bound plus schema/message overhead. This
        # covers the entire conversation resent with store=False on every turn.
        serialized = _json({"input": inputs, "tools": definitions})
        upper_input = len(serialized.encode("utf-8")) + 4_096
        if upper_input > MAX_INPUT_BYTES:
            raise _CallFailure("research_context_limit")
        try:
            estimate = price(model, upper_input, self.max_output_tokens) * Decimal("1.25")
            create = self.rag.client.responses.create
        except Exception:
            raise _CallFailure("research_model_configuration") from None
        try:
            reservation = self.rag.budget.reserve(estimate, visitor, "generation", model)
        except LimitReached:
            raise _CallFailure("research_budget_limit", limited=True) from None
        except Exception:
            raise _CallFailure("research_budget_unavailable") from None

        audit = {
            "step": step, "reservation_id": reservation, "requested_model": model,
            "policy": policy,
            "prompt_sha256": _digest(serialized),
            "tool_schema_sha256": _digest(_json(definitions)),
            "state": "uncertain",
        }
        settled = False
        try:
            response = create(
                model=model, input=inputs, tools=definitions,
                tool_choice="required", parallel_tool_calls=False,
                max_output_tokens=self.max_output_tokens,
                reasoning={"effort": "none"}, store=False,
            )
            i, o, cached, writes, usage = _usage(response)
            actual = price(model, i, o, cached, writes)
            audit.update(
                response_id=_field(response, "id"),
                provider_model=_field(response, "model"),
                usage=usage, cost_usd=float(actual),
            )
            self.rag.budget.settle(
                reservation, actual,
                {**usage, "observatory_request": {
                    "stage": "research_agent", "policy": policy,
                    "step": step, "prompt_sha256": audit["prompt_sha256"],
                    "tool_schema_sha256": audit["tool_schema_sha256"],
                    "provider_model": audit["provider_model"],
                    "response_id": audit["response_id"],
                }},
            )
            settled = True
            audit["state"] = "settled"
            return response, audit, float(actual)
        except Exception as exc:
            # A malformed result with known usage is still paid. Only unknown
            # dispatch/accounting outcomes retain their reservation as exposure.
            if not settled:
                try:
                    self.rag.budget.uncertain(reservation, type(exc).__name__)
                except Exception:
                    pass
            try:
                exposure = self.rag.budget.reservation_cost(reservation)
            except Exception:
                exposure = float(estimate)
            audit.update(error_type=type(exc).__name__, cost_usd=exposure)
            raise _CallFailure("research_provider_unavailable", cost=exposure, audit=audit) from None

    def _run_intent(self, question, base_filters, visitor, progress=None):
        """One interpretation call; the server then performs bounded typed reads."""
        from .intent_execution import execute_intent
        from .prompts import QUESTION_INTENT_SYSTEM
        from .question_intent import compile_intent, intent_tool_definition

        run = ResearchRun(route="unavailable", original_question=question,
                          base_filters=base_filters.model_dump(mode="json"))
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2_000:
            run.failure_reason = "research_question_invalid"
            return run
        try:
            definition = intent_tool_definition()
            _require_strict_objects(definition["parameters"])
            context = self.entity_context
            if context is None and hasattr(self.catalog, "entity_context"):
                context = self.catalog.entity_context()
            payload = {"question": question, "active_scope": run.base_filters,
                       "entity_context": context or {},
                       "reference_date": str(self.reference_date) if self.reference_date else None}
            inputs = [{"role": "system", "content": QUESTION_INTENT_SYSTEM},
                      {"role": "user", "content": _json(payload)}]
        except Exception:
            run.failure_reason = "research_tool_configuration"
            return run
        try:
            if progress:
                progress("interpreting")
            response, audit, cost = self._dispatch(
                inputs, [definition], visitor, 1, policy="question-intent-v1",
            )
            run.model_calls.append(audit)
            run.cost_usd += cost
        except _CallFailure as exc:
            run.failure_reason = exc.reason
            run.route = "limited" if exc.limited else "unavailable"
            run.cost_usd += exc.cost
            if exc.audit:
                run.model_calls.append(exc.audit)
            return run
        if _field(response, "status") != "completed":
            run.failure_reason = "research_response_incomplete"
            return run
        try:
            calls = [item for item in _field(response, "output", [])
                     if _field(item, "type") == "function_call"]
            if len(calls) != 1:
                raise ValueError("Expected one interpretation")
            call = calls[0]
            raw = _field(call, "arguments")
            if (_field(call, "name") != definition["name"]
                    or not isinstance(_field(call, "call_id"), str) or not _field(call, "call_id")
                    or not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_ARGUMENT_BYTES):
                raise ValueError("Invalid interpretation envelope")
            args = json.loads(raw, parse_constant=_invalid_constant, object_pairs_hook=_unique_object)
            if not isinstance(args, dict):
                raise ValueError("Interpretation must be an object")
            compiled = compile_intent(args, question, base_filters)
            run.interpretation = {"schema_version": "question-intent-v1", "status": compiled.status,
                                  "intent": args, "coverage": compiled.coverage,
                                  "compiled_tasks": len(compiled.tasks)}
        except (ValueError, TypeError, AttributeError, RecursionError):
            run.route, run.failure_reason = "unavailable", "research_intent_invalid"
            run.result = {"status": "unavailable", "message":
                "Question interpretation did not preserve a valid task and scope. No data read was performed."}
            return run
        if compiled.status != "ready":
            run.route, run.failure_reason = "clarify", "research_intent_unresolved"
            run.result = {"status": "clarify", "message": compiled.message}
            return run
        return execute_intent(run, compiled, self.catalog, progress)

    def run(self, question: str, base_filters: Filters, visitor: str, progress=None):
        if self.intent_enabled:
            return self._run_intent(question, base_filters, visitor, progress)
        run = ResearchRun(
            route="unavailable", original_question=question,
            base_filters=base_filters.model_dump(mode="json"),
        )
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2_000:
            run.failure_reason = "research_question_invalid"
            return run
        try:
            run.question_contract = question_contract(question)
            definitions = self._definitions()
            all_names = {item["name"] for item in definitions}
            context = self.entity_context
            if context is None and hasattr(self.catalog, "entity_context"):
                context = self.catalog.entity_context()
            payload = {
                "question": question, "active_scope": run.base_filters,
                "question_contract": run.question_contract,
                "entity_context": context or {},
                "reference_date": str(self.reference_date) if self.reference_date else None,
            }
            inputs = [{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": _json(payload)}]
        except Exception:
            run.failure_reason = "research_tool_configuration"
            return run

        seen_calls = set()
        tasks, completed = [], []
        selected_record = None
        trusted_record_id = base_filters.record_ids[0] if len(base_filters.record_ids) == 1 else None
        for step in range(1, self.max_steps + 1):
            try:
                current_question = tasks[len(completed)].question_part if tasks else question
                contract = question_contract(current_question) if tasks else run.question_contract
                step_definitions = _step_definitions(definitions, contract, tasks, completed, selected_record,
                                                     trusted_record_id)
                names = {item["name"] for item in step_definitions}
                if progress:
                    progress("interpreting")
                response, model_audit, cost = self._dispatch(inputs, step_definitions, visitor, step)
                run.model_calls.append(model_audit)
                run.cost_usd += cost
            except _CallFailure as exc:
                run.failure_reason = exc.reason
                run.route = "limited" if exc.limited else "unavailable"
                run.cost_usd += exc.cost
                if exc.audit:
                    run.model_calls.append(exc.audit)
                return _complete_plan(
                    run, tasks, completed, exc.reason,
                    failure_status="limited" if exc.limited else "service_unavailable",
                ) if tasks else run

            if _field(response, "status") != "completed":
                run.failure_reason = "research_response_incomplete"
                return _complete_plan(run, tasks, completed, run.failure_reason or "research_plan_interrupted") if tasks else run
            try:
                outputs = list(_field(response, "output", []))
                calls = [o for o in outputs if _field(o, "type") == "function_call"]
                if len(calls) != 1:
                    raise ValueError("Expected exactly one function call")
                call = calls[0]
                name, call_id = _field(call, "name"), _field(call, "call_id")
                raw = _field(call, "arguments")
                if name not in all_names or not isinstance(call_id, str) or not call_id:
                    raise ValueError("Unknown tool or missing call identity")
                if call_id in seen_calls or len(run.tool_trace) >= self.max_tool_calls:
                    raise ValueError("Repeated or excess tool call")
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_ARGUMENT_BYTES:
                    raise ValueError("Invalid argument payload")
                args = json.loads(raw, parse_constant=_invalid_constant, object_pairs_hook=_unique_object)
                if not isinstance(args, dict):
                    raise ValueError("Tool arguments must be an object")
                seen_calls.add(call_id)
            except (ValueError, TypeError, AttributeError, RecursionError):
                run.failure_reason = "research_tool_call_invalid"
                return _complete_plan(run, tasks, completed, run.failure_reason or "research_plan_interrupted") if tasks else run

            trace = {"step": step, "call_id": call_id, "tool": name, "arguments": args}
            run.tool_trace.append(trace)
            if name not in names:
                # Preserve a useful scope clarification for a known-but-unavailable
                # operation, instead of turning a model mistake into an outage.
                reason = ("original_source_record_unresolved" if name == "get_record_metadata"
                          and not (selected_record or trusted_record_id)
                          else "research_record_binding_mismatch" if selected_record and name == "find_records"
                          else "research_plan_required" if not tasks and contract.get("compound_read_request")
                          else "original_source_metadata_required" if contract.get("original_metadata_only")
                          else "research_plan_scope_mismatch")
                trace.update(status="clarify", blocked_by=reason)
                run.route, run.failure_reason = "clarify", reason
                run.result = {"status": "clarify", "message":
                    "The selected operation does not answer the current task. Preserve every question part and its source scope."}
                return _complete_plan(run, tasks, completed, reason) if tasks else run
            if name == "set_research_plan":
                try:
                    if tasks or any(item.get("tool") in TERMINAL_ROUTES for item in run.tool_trace[:-1]):
                        raise ValueError("A plan must precede final reads and cannot be replaced")
                    tasks = _validate_plan(args, question, base_filters)
                    selected_record = None
                    result = {"status": "ok", "tasks": [task.model_dump() for task in tasks]}
                    trace["status"] = "ok"
                    inputs.extend(_dump(item) for item in outputs)
                    inputs.append({"type": "function_call_output", "call_id": call_id, "output": _json(result)})
                    continue
                except ValueError:
                    trace["status"] = "invalid_request"
                    run.route, run.failure_reason = "clarify", "research_plan_invalid"
                    run.result = {"status": "clarify", "message":
                        "I could not form a valid reading plan that preserves every requested task, its wording, order and collection. Please clarify the separate read tasks."}
                    return _complete_plan(run, tasks, completed, run.failure_reason or "research_plan_interrupted") if tasks else run
            if name == "request_clarification":
                try:
                    clarification = _Clarification.model_validate(args)
                except ValueError:
                    trace["status"] = "invalid_request"
                    run.failure_reason = "research_clarification_invalid"
                    return _complete_plan(run, tasks, completed, run.failure_reason or "research_plan_interrupted") if tasks else run
                trace["status"] = "clarify"
                run.route = "clarify"
                run.result = {"status": "clarify", **clarification.model_dump()}
                return _complete_plan(run, tasks, completed, "research_plan_clarification") if tasks else run

            explicit = contract.get("explicit_datasets", [])
            supplied_dataset = effective_dataset_scope(
                (args.get("filters") or {}).get("dataset"), base_filters.dataset)
            expected_dataset = tasks[len(completed)].dataset if tasks else supplied_dataset
            if (name in TERMINAL_ROUTES and explicit
                    and supplied_dataset != ("all" if len(explicit) > 1 else explicit[0])):
                trace.update(status="clarify", blocked_by="explicit_collection_scope_missing")
                run.route, run.failure_reason = "clarify", "explicit_collection_scope_missing"
                run.result = {"status": "clarify", "message": "The read must preserve the collection named in your question and the active selection."}
                return _complete_plan(run, tasks, completed, run.failure_reason) if tasks else run
            if (not tasks and contract.get("compound_read_request") and name in TERMINAL_ROUTES):
                trace.update(status="clarify", blocked_by="compound_read_requires_plan")
                run.route, run.failure_reason = "clarify", "research_plan_required"
                run.result = {"status": "clarify", "message": "This question has multiple read tasks. A complete task plan is required; one result cannot stand in for all parts."}
                return run
            if name == "record_statistics" and contract.get("content_condition"):
                # Scope validation cannot detect a dropped content predicate:
                # a valid sponsor total still does not answer what its ads say.
                # The provider call remains settled/audited, but no unrelated
                # database total is read or published.
                trace.update(status="clarify", blocked_by="content_condition_requires_evidence")
                run.route = "clarify"
                run.result = {
                    "status": "clarify", "reason": "unsupported_analysis",
                    "message": metadata_content_clarification(question),
                }
                return _complete_plan(run, tasks, completed, "content_condition_requires_evidence") if tasks else run

            if (contract.get("original_metadata_only")
                    and name not in {"find_records", "resolve_entity", "get_record_metadata"}):
                trace.update(status="clarify", blocked_by="original_source_field_requires_metadata")
                run.route = "clarify"
                run.result = {"status": "clarify", "reason": "unsupported_analysis",
                              "message": "This question asks for a stored original-source field. Identify the record and read its original metadata; outside pages cannot replace it."}
                run.failure_reason = "original_source_metadata_required"
                return _complete_plan(run, tasks, completed, run.failure_reason) if tasks else run

            if entity_dates_used_as_filters(current_question, context or {}, args):
                trace.update(status="clarify", blocked_by="entity_name_is_not_date_scope")
                run.route, run.failure_reason = "clarify", "entity_name_is_not_date_scope"
                run.result = {"status": "clarify", "message": "The dates appear inside the stored entity name. Please specify a separate date range if you want one."}
                return _complete_plan(run, tasks, completed, run.failure_reason) if tasks else run

            validation_context = context or {}
            if name == "record_statistics" and hasattr(self.catalog, "statistics_validation_context"):
                try:
                    # Full namespaces stay on the server. The model's small,
                    # active-selection context cannot hide a dropped entity.
                    validation_context = self.catalog.statistics_validation_context()
                except Exception:
                    trace.update(status="unavailable", blocked_by="statistics_scope_validation_unavailable")
                    run.failure_reason = "statistics_scope_validation_unavailable"
                    return _complete_plan(run, tasks, completed, run.failure_reason) if tasks else run
            if name == "record_statistics" and not statistics_request_preserves_question(
                    current_question, validation_context, args, run.base_filters):
                trace.update(status="clarify", blocked_by="statistics_question_scope_missing")
                run.route = "clarify"
                run.failure_reason = "research_plan_predicate_missing" if tasks else "statistics_question_scope_missing"
                run.result = {"status": "clarify", "message":
                    "The statistics request omitted or changed a named source, date range, grouping or comparison. Clarify the complete scope before reading counts."}
                return _complete_plan(run, tasks, completed, run.failure_reason) if tasks else run

            if tasks and name in TERMINAL_ROUTES:
                expected = tasks[len(completed)]
                if TERMINAL_ROUTES[name] != expected.route or supplied_dataset != expected.dataset:
                    trace.update(status="invalid_request", blocked_by="research_plan_scope_mismatch")
                    return _complete_plan(run, tasks, completed, "research_plan_scope_mismatch")

            if name in {"get_record_metadata", "get_record", "get_record_sources"}:
                bound_id = selected_record["record_id"] if selected_record else trusted_record_id
                if bound_id and args.get("record_id") != bound_id:
                    trace.update(status="clarify", blocked_by="research_record_binding_mismatch")
                    run.route, run.failure_reason = "clarify", "research_record_binding_mismatch"
                    run.result = {"status": "clarify", "message":
                        "The source read changed the matched record. Select the intended record before continuing."}
                    return _complete_plan(run, tasks, completed, run.failure_reason) if tasks else run

            try:
                if progress:
                    progress("database")
                result = self.catalog.call(name, args)
                encoded = _json(result)
                if not isinstance(result, dict) or len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
                    raise ValueError("Invalid or oversized research result")
            except Exception:
                trace["status"] = "unavailable"
                run.failure_reason = "research_tool_unavailable"
                return _complete_plan(run, tasks, completed, run.failure_reason) if tasks else run

            status = result.get("status")
            trace.update(
                status=status,
                effective_filters=result.get("filters"),
                data_version=result.get("data_version"),
                statistics_version=result.get("statistics_version"),
                claims_version=result.get("claims_version"),
                publication_version=result.get("publication_version"),
                review_version=result.get("review_version"),
                definitions_sha256=result.get("definitions_sha256"),
                source_scope_sha256=result.get("source_scope_sha256"),
                question_id=(result.get("question") or {}).get("question_id"),
                data_refs=_source_refs(result),
                scope_notes=result.get("scope_notes", []),
                result_sha256=_digest(encoded),
            )
            if status in {"ambiguous", "not_found"} and name == "find_records":
                run.route, run.result = "clarify", {**result, "message": result.get("message") or
                    "The stored title has no unique match. Select an exact record; no outside page will substitute for its stored fields."}
                run.failure_reason = "original_source_record_unresolved"
                return _complete_plan(run, tasks, completed, run.failure_reason or "research_plan_interrupted") if tasks else run
            if status == "clarify":
                run.route, run.result = "clarify", result
                return _complete_plan(run, tasks, completed, "research_plan_clarification") if tasks else run
            if status != "ok":
                run.result = result
                run.failure_reason = "research_tool_" + (
                    status if status in {"unavailable", "invalid_request"} else "invalid_result"
                )
                return _complete_plan(run, tasks, completed, run.failure_reason) if tasks else run

            if name == "find_records":
                try:
                    selected_record = _selected_record(result)
                except (ValueError, TypeError, AttributeError):
                    trace["status"] = "invalid_result"
                    run.failure_reason = "research_title_binding_invalid"
                    return _complete_plan(run, tasks, completed, run.failure_reason) if tasks else run
                trace["selected_record"] = selected_record
            if (name in {"get_record_metadata", "get_record", "get_record_sources"} and selected_record
                    and not _selection_current(result, selected_record)):
                trace.update(status="invalid_result", blocked_by="research_record_version_changed")
                run.failure_reason = "research_record_version_changed"
                return _complete_plan(run, tasks, completed, run.failure_reason) if tasks else run

            if name in TERMINAL_ROUTES:
                run.route = TERMINAL_ROUTES[name]
                # Canonical source scope comes from the executor, never the
                # model's arguments. Search query is retrieval-only, not answer text.
                if run.route == "evidence":
                    result = {**result, "search_query": args.get("query", question)}
                if (tasks or name == "record_statistics" or explicit) and _result_dataset(result) != expected_dataset:
                    trace.update(status="invalid_result", blocked_by="research_result_scope_mismatch")
                    run.failure_reason = "research_plan_result_scope_mismatch" if tasks else "research_result_scope_mismatch"
                    return _complete_plan(run, tasks, completed, run.failure_reason) if tasks else run
                if name == "record_statistics" and (
                        not _result_preserves_active_filters(result.get("filters"), run.base_filters, args.get("filters") or {})
                        or not statistics_request_preserves_question(
                            current_question, validation_context,
                            {**args, "filters": result.get("filters") or {}}, run.base_filters)):
                    trace.update(status="invalid_result", blocked_by="statistics_result_scope_missing")
                    run.failure_reason = "research_plan_result_predicate_missing" if tasks else "statistics_result_scope_missing"
                    return _complete_plan(run, tasks, completed, run.failure_reason) if tasks else run
                if not tasks:
                    run.result = result
                    return _complete_plan(run, tasks, completed, run.failure_reason or "research_plan_interrupted") if tasks else run
                expected = tasks[len(completed)]
                completed.append({**expected.model_dump(), "result": result})
                selected_record = None
                if len(completed) == len(tasks):
                    return _complete_plan(run, tasks, completed)
                # The model gets bounded progress, not a growing source transcript.
                encoded = _json({"status": "ok", "completed_part": expected.model_dump(),
                                 "next_part": tasks[len(completed)].model_dump()})

            if step == self.max_steps or len(run.tool_trace) == self.max_tool_calls:
                run.failure_reason = "research_step_limit"
                return _complete_plan(run, tasks, completed, run.failure_reason) if tasks else run
            if len(encoded.encode("utf-8")) > MAX_INTERMEDIATE_BYTES:
                run.failure_reason = "research_intermediate_limit"
                return _complete_plan(run, tasks, completed, run.failure_reason or "research_plan_interrupted") if tasks else run
            try:
                # Preserve all Responses output items, including any reasoning
                # item needed by the next call. No provider conversation is stored.
                inputs.extend(_dump(item) for item in outputs)
                inputs.append({"type": "function_call_output", "call_id": call_id,
                               "output": encoded})
            except (ValueError, TypeError, AttributeError):
                run.failure_reason = "research_output_invalid"
                return _complete_plan(run, tasks, completed, run.failure_reason or "research_plan_interrupted") if tasks else run

        run.failure_reason = "research_step_limit"
        return _complete_plan(run, tasks, completed, run.failure_reason) if tasks else run
