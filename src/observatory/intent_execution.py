"""Execute compiled question tasks through the existing read-only catalog.

This adapter does not interpret text, generate counts, or dispatch a model.
Every accepted result retains the catalog's exact scope and source bindings.
"""

from copy import deepcopy

from .research_tools import METADATA_FIELDS, TOOLS, FiltersRequest, ScopeConflict

_ROUTES = {
    "statistics": "record_statistics",
    "evidence": "search_records",
    "metadata": "get_record_metadata",
    "record": "get_record",
    "sources": "get_record_sources",
}
MAX_INTENT_READS = 6


def _same_json(actual, expected):
    """Compare complete JSON shapes without bool/int coercion or defaults."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _same_json(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _same_json(value, target) for value, target in zip(actual, expected)
        )
    return actual == expected


def _expected_scope(catalog, name, arguments):
    request = TOOLS[name][0].model_validate(arguments)
    filters = catalog.narrow(getattr(request, "filters", None))
    denominator = None
    if name == "record_statistics" and request.measure == "share":
        denominator = catalog.base_filters
        if request.denominator_filters is not None:
            denominator = catalog.narrow(request.denominator_filters)
            filters = catalog.narrow(request.filters, base=denominator)
    return request, filters, denominator


def _collections(rows, dataset):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2:
        raise ValueError("Statistics need separate loaded collections")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("A collection count must be an object")
        current, total = row.get("dataset"), row.get("total")
        if current not in {"native", "social"} or current in seen or dataset not in {"all", current}:
            raise ValueError("Collection units must remain distinct within scope")
        if type(total) is not int or total < 0:
            raise ValueError("A count must be a nonnegative integer")
        if current == "social" and "count_unit" in row and row["count_unit"] not in {
            "platform_canonical_original_post_url", "source_record",
        }:
            raise ValueError("An explicit count unit must retain an existing record unit")
        for field in ("retrievable", "unknown_dates", "source_unknown_dates", "inferred_dates"):
            if field in row and (type(row[field]) is not int or not 0 <= row[field] <= total):
                raise ValueError("Invalid subset count")
        seen.add(current)
    return seen


def _statistics_result(result, request, filters, denominator, catalog):
    """Validate primitive counts and the compiled operation's result shape."""
    if result.get("method") != "database":
        raise ValueError("Statistics require program-computed database results")
    group = request.group_by or "none"
    expected_kind = (
        "compare_periods" if request.periods else
        "social_historical_labels" if group == "social_historical_labels" else
        "top_years" if group == "years" and request.ranking == "highest" else
        "list_years" if group == "years" else
        "share" if request.measure == "share" else
        {"none": "count", "publishers": "list_publishers", "sponsors": "list_sponsors",
         "platforms": "list_platforms", "accounts": "list_accounts"}[group]
    )
    if result.get("kind") != expected_kind or result.get("group_by") != (
        None if request.periods or group == "none" else group
    ):
        raise ValueError("The result substituted another statistical operation")
    datasets = _collections(result.get("collections"), filters.dataset)
    groups = result.get("groups")
    if not isinstance(groups, list):
        raise ValueError("Statistics groups must be an array")
    seen_groups = set()
    for row in groups:
        if (not isinstance(row, dict) or row.get("dataset") not in datasets
                or not isinstance(row.get("name"), str)
                or type(row.get("count")) is not int or row["count"] < 0):
            raise ValueError("Invalid collection-specific group count")
        identity = row["dataset"], row["name"]
        if identity in seen_groups:
            raise ValueError("Duplicate statistical group")
        seen_groups.add(identity)
    if group == "none" and groups:
        raise ValueError("A total or period result cannot include group counts")
    if denominator is not None:
        expected_basis = ("question_comparison_group" if request.denominator_filters is not None
                          else "current_selection_before_question_targets")
        if (not _same_json(result.get("denominator_filters"), denominator.model_dump(mode="json"))
                or result.get("denominator_basis") != expected_basis):
            raise ValueError("The share denominator changed")
        for row in result["collections"]:
            n, d = row.get("numerator"), row.get("denominator")
            percentage = row.get("percentage")
            if (type(n) is not int or type(d) is not int or not 0 <= n <= d
                    or row["total"] != n or isinstance(percentage, bool)
                    or percentage != (100 * n / d if d else None)
                    or row.get("percentage_status") != ("defined" if d else "empty_selection")):
                raise ValueError("Invalid independently computed share")
    if group == "years" or request.periods:
        if result.get("ranking") != (request.ranking or "all"):
            raise ValueError("The requested ranking changed")
    if request.periods:
        periods = result.get("periods")
        if not isinstance(periods, list) or len(periods) != len(request.periods):
            raise ValueError("A comparison requires every declared period")
        for actual, period in zip(periods, request.periods):
            expected = catalog.narrow(FiltersRequest(
                date_from=period.date_from, date_to=period.date_to, include_unknown_dates=False,
            ), base=filters)
            if (not isinstance(actual, dict) or actual.get("label") != period.label
                    or not _same_json(actual.get("filters"), expected.model_dump(mode="json"))
                    or _collections(actual.get("collections"), filters.dataset) != datasets):
                raise ValueError("A comparison period changed its scope or collection units")


def execute_intent(run, compiled, catalog, progress=None):
    """Return a ResearchRun after at most six deterministic catalog reads."""
    # ResearchAgent lazily imports this module, so keep its helpers lazy too.
    from .research_agent import (
        MAX_ARGUMENT_BYTES,
        MAX_RESULT_BYTES,
        _complete_plan,
        _digest,
        _json,
        _ResearchTask,
        _selected_record,
        _selection_current,
        _source_refs,
    )

    tasks = list(compiled.tasks)
    completed, versions = [], {}
    if compiled.status != "ready":
        run.route, run.failure_reason = "clarify", compiled.failure_reason or "research_plan_clarification"
        run.result = {"status": "clarify", "message": compiled.message}
        return run
    if (not 1 <= len(tasks) <= 3 or any(_ROUTES.get(task.route) != task.tool_name for task in tasks)
            or len(tasks) > 1 and any(task.route == "evidence" for task in tasks)):
        run.route, run.failure_reason = "unavailable", "research_tool_configuration"
        return run
    try:
        plans = [_ResearchTask(question_part=task.question_part, dataset=task.dataset, route=task.route)
                 for task in tasks] if len(tasks) > 1 else []
    except ValueError:
        run.route, run.failure_reason = "unavailable", "research_tool_configuration"
        return run
    reads = 0

    def finish(reason, result=None, route="unavailable"):
        run.route, run.failure_reason = route, reason
        run.result = result or {}
        if plans:
            message = run.result.get("message", "")
            _complete_plan(run, plans, completed, reason)
            if message:
                run.result["failure_message"] = message
        return run

    def read(name, arguments):
        nonlocal reads
        reads += 1
        trace = {"step": reads, "call_id": f"intent-read-{reads}", "tool": name,
                 "arguments": deepcopy(arguments), "transport": "compiled_intent_catalog"}
        run.tool_trace.append(trace)
        if reads > MAX_INTENT_READS:
            trace["status"] = "invalid_request"
            return None, None, None, "research_step_limit", "unavailable"
        try:
            if not isinstance(arguments, dict) or len(_json(arguments).encode("utf-8")) > MAX_ARGUMENT_BYTES:
                raise ValueError("Invalid compiled argument payload")
            request, filters, denominator = _expected_scope(catalog, name, arguments)
            trace["expected_filters"] = filters.model_dump(mode="json")
        except ScopeConflict as exc:
            trace["status"] = "clarify"
            return {"status": "clarify", "message": str(exc)}, None, None, "research_plan_clarification", "clarify"
        except Exception:
            trace["status"] = "invalid_request"
            return None, None, None, "research_tool_call_invalid", "unavailable"
        try:
            if progress:
                progress("database")
            result = catalog.call(name, deepcopy(arguments))
        except Exception:
            trace["status"] = "unavailable"
            return None, None, None, "research_tool_unavailable", "unavailable"
        try:
            if not isinstance(result, dict):
                raise ValueError("Tool result must be an object")
            encoded = _json(result)
            if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
                raise ValueError("Tool result exceeds the bounded result size")
            trace.update(status=result.get("status"), effective_filters=result.get("filters"),
                         data_version=result.get("data_version"), statistics_version=result.get("statistics_version"),
                         data_refs=_source_refs(result), scope_notes=result.get("scope_notes", []),
                         result_sha256=_digest(encoded))
        except Exception:
            trace["status"] = "invalid_result"
            return None, None, None, "research_tool_invalid_result", "unavailable"
        status = result.get("status")
        if name == "find_records" and status in {"ambiguous", "not_found"}:
            return {**result, "status": "clarify"}, None, None, "original_source_record_unresolved", "clarify"
        if status == "clarify":
            return result, None, None, "research_plan_clarification", "clarify"
        if status != "ok":
            reason = "research_tool_" + (status if status in {"unavailable", "invalid_request"} else "invalid_result")
            return result, None, None, reason, "unavailable"
        if result.get("failure_reason") or result.get("reason"):
            trace.update(status="invalid_result", blocked_by="research_tool_invalid_result")
            return result, None, None, "research_tool_invalid_result", "unavailable"
        if not _same_json(result.get("filters"), filters.model_dump(mode="json")):
            trace.update(status="invalid_result", blocked_by="research_result_scope_mismatch")
            return result, None, None, "research_result_scope_mismatch", "unavailable"
        data = result.get("data_version")
        stats = result.get("statistics_version")
        if (not isinstance(data, str) or not data or data == "unavailable"
                or stats is not None and (not isinstance(stats, str) or not stats or stats == "unavailable")
                or name == "record_statistics" and stats is None
                or versions and (data != versions["data"] or stats != versions["statistics"])):
            trace.update(status="invalid_result", blocked_by="research_record_version_changed")
            return result, None, None, "research_record_version_changed", "unavailable"
        versions.update(data=data, statistics=stats)
        return result, request, (filters, denominator), "", ""

    for task in tasks:
        selected = None
        arguments = deepcopy(task.arguments)
        if task.title_lookup is not None:
            result, _, _, reason, route = read("find_records", task.title_lookup)
            if reason:
                return finish(reason, result, route)
            try:
                selected = _selected_record(result)
                if (type(result.get("total_candidates")) is not int
                        or any(not isinstance(value, str) or not value for value in selected.values())
                        or selected["dataset"] not in {"native", "social"}
                        or task.dataset not in {"all", selected["dataset"]}):
                    raise ValueError("Invalid title record binding")
                arguments = task.bind_record(selected["record_id"])
                run.tool_trace[-1]["selected_record"] = selected
            except Exception:
                run.tool_trace[-1]["status"] = "invalid_result"
                return finish("research_title_binding_invalid")
        result, request, scope, reason, route = read(task.tool_name, arguments)
        if reason:
            return finish(reason, result, route)
        if scope[0].dataset != task.dataset:
            run.tool_trace[-1].update(status="invalid_result", blocked_by="research_result_scope_mismatch")
            return finish("research_result_scope_mismatch", result)
        if selected is not None and not _selection_current(result, selected):
            run.tool_trace[-1].update(status="invalid_result", blocked_by="research_record_version_changed")
            return finish("research_record_version_changed", result)
        if task.route in {"metadata", "record", "sources"}:
            record = result.get("record") or {}
            if (not isinstance(record, dict) or record.get("record_id") != arguments.get("record_id")
                    or record.get("dataset") not in {"native", "social"}
                    or scope[0].dataset not in {"all", record.get("dataset")}
                    or any(not isinstance(record.get(key), str) or not record[key]
                           for key in ("record_id", "dataset", "version_id", "body_hash"))):
                return finish("research_record_binding_mismatch", result)
        if task.route == "statistics":
            try:
                _statistics_result(result, request, *scope, catalog)
            except Exception:
                run.tool_trace[-1].update(status="invalid_result", blocked_by="research_tool_invalid_result")
                return finish("research_tool_invalid_result", result)
        elif task.route == "metadata":
            fields = result.get("original_fields")
            expected = request.fields or METADATA_FIELDS
            if (not isinstance(fields, dict) or set(fields) != set(expected)
                    or any(not isinstance(value, dict) or not isinstance(value.get("status"), str)
                           or not value["status"] for value in fields.values())):
                run.tool_trace[-1].update(status="invalid_result", blocked_by="research_tool_invalid_result")
                return finish("research_tool_invalid_result", result)
        elif task.route == "evidence":
            result = {**result, "search_query": arguments["query"]}
        if not plans:
            run.route, run.result, run.failure_reason = task.route, result, ""
            return run
        completed.append({"question_part": task.question_part, "dataset": task.dataset,
                          "route": task.route, "result": result})
    return _complete_plan(run, plans, completed)
