"""Pure offline contract for reviewed customer metrics, without publication authority.

All scores are recomputed from bounded observations. Exact review bindings and
declared independence are checked, but neither hashes nor reviewer declarations
authenticate execution, review authority, source meaning, or client acceptance.
No existing evaluator output is implicitly promoted into this contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from datetime import datetime
from decimal import Decimal

from observatory.evaluation_scorecard import DEFINITION_VERSION, METRICS
from observatory.metric_statistics import binary_result, latency_summary, set_result

FORMAT_VERSION = 1
VERSION_FIELDS = frozenset({"app_version", "prompt_version", "data_version",
                            "source_data_version", "index_version", "question_version",
                            "rubric_version", "active_profile"})
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_CASES = 2000
MAX_METRIC_GROUPS = 128
MAX_SAMPLES = 10000
MAX_REVIEWS = 10000
MAX_IDENTITIES = 10000
_BINARY_METRICS = {"M01", "M02", "M05", "M08", "M09", "M10", "M11", "M12", "M13", "M14"}
_SET_METRICS = {"M03", "M04", "M06", "M07"}
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class MetricReportInvalid(ValueError):
    """The report is malformed, inconsistent, or insufficiently bound."""


def _require(condition, message):
    if not condition:
        raise MetricReportInvalid(message)


def _object(value, fields, name):
    _require(type(value) is dict and set(value) == set(fields), f"{name}: unexpected or missing fields")


def _text(value, name, *, limit=512):
    _require(type(value) is str and bool(value.strip()) and len(value) <= limit,
             f"{name}: nonblank bounded string required")


def _identity(value, name):
    _text(value, name, limit=256)
    _require(value == value.strip(), f"{name}: identity cannot contain surrounding whitespace")


def _hash(value, name):
    _require(type(value) is str and bool(_HASH.fullmatch(value)), f"{name}: lowercase SHA-256 required")


def _list(value, name, limit):
    _require(type(value) is list and len(value) <= limit, f"{name}: bounded list required")


def _identities(value, name, limit=MAX_IDENTITIES):
    _list(value, name, limit)
    for identifier in value:
        _identity(identifier, name)
    _require(len(set(value)) == len(value), f"{name}: duplicate identity")


def _date(value, name):
    _text(value, name, limit=64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MetricReportInvalid(f"{name}: ISO 8601 timestamp required") from exc
    _require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
             f"{name}: timestamp must include a timezone")
    return parsed


def _versions(value, name):
    _object(value, VERSION_FIELDS, name)
    for field, version in value.items():
        if field == "active_profile":
            _identity(version, f"{name}.{field}")
        else:
            _hash(version, f"{name}.{field}")


def _canonical_bytes(value):
    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
        raise MetricReportInvalid("Only finite JSON values are permitted") from exc
    _require(len(encoded) <= MAX_JSON_BYTES, "Report exceeds the bounded JSON size")
    return encoded


def source_bindings_sha256(source_bindings: list[dict]) -> str:
    """Hash exact, ordered source-version declarations; no source is fetched."""
    return hashlib.sha256(_canonical_bytes(source_bindings)).hexdigest()


def observation_sha256(value) -> str:
    """Bind a review decision to the exact raw candidate, without scoring it."""
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def metric_plan_sha256(payload: dict) -> str:
    """Hash the declared case and unit plan for external frozen-plan comparison.

    This helper does not validate or approve the plan. Task eligibility is frozen
    before execution; generated statement/citation inventories can only be frozen
    after generation and before review. An external digest detects later changes.
    """
    try:
        plan = {"definition_version": payload["definition_version"], "versions": payload["versions"],
                "sampling_description": payload["sampling_description"],
                "cases": [{key: row[key] for key in ("case_id", "dataset", "task_type", "producer_id")}
                          for row in payload["cases"]],
                "metrics": [{key: row[key] for key in ("metric_id", "dataset", "task_type",
                                                     "citation_source_type", "review_policy", "unit_plan")}
                            for row in payload["metrics"]]}
    except (TypeError, KeyError) as exc:
        raise MetricReportInvalid("Missing fields in metric plan") from exc
    return hashlib.sha256(_canonical_bytes(plan)).hexdigest()


def _sources(rows):
    _list(rows, "source_bindings", MAX_IDENTITIES)
    seen = set()
    for row in rows:
        _object(row, {"record_id", "dataset", "version_id", "body_sha256", "payload_sha256"}, "source binding")
        for field in ("record_id", "version_id"):
            _identity(row[field], f"source.{field}")
        _require(row["dataset"] in {"native", "social", "external"}, "source.dataset: invalid collection")
        for field in ("body_sha256", "payload_sha256"):
            _hash(row[field], f"source.{field}")
        identity = (row["dataset"], row["record_id"])
        _require(identity not in seen, "source_bindings: duplicate source identity")
        seen.add(identity)


def _value(metric_id, value):
    if value is None:
        return
    if metric_id in _BINARY_METRICS:
        _require(type(value) is bool, "Binary samples require explicit booleans")
    elif metric_id in _SET_METRICS:
        _object(value, {"expected_ids", "returned_ids", "gold_complete"}, "set sample")
        _identities(value["expected_ids"], "expected_ids")
        _identities(value["returned_ids"], "returned_ids")
        _require(type(value["gold_complete"]) is bool, "gold_complete requires an explicit boolean")
    elif metric_id == "M15":
        _object(value, {"latency_ms", "cache_state", "temperature_state", "timing_boundary"}, "latency sample")
        _number(value["latency_ms"], "latency_ms", 1e12)
        _require(value["cache_state"] in {"cached", "uncached", "unknown"}, "Invalid cache state")
        _require(value["temperature_state"] in {"cold", "warm", "unknown"}, "Invalid temperature state")
        _require(value["timing_boundary"] == "submission_to_usable_result_or_terminal_failure",
                 "M15 requires the complete user timing boundary")
    else:
        _object(value, {"settled_usd", "ledger_complete"}, "cost sample")
        _number(value["settled_usd"], "settled_usd", 1e9)
        _require(type(value["ledger_complete"]) is bool, "ledger_complete requires an explicit boolean")


def _number(value, name, maximum):
    _require(type(value) in (int, float), f"{name}: numeric value required, without coercion")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    _require(finite and 0 <= value <= maximum, f"{name}: finite bounded nonnegative value required")


def _validate_metric_report(payload, expected_versions, expected_plan_sha256):
    """Return a detached validated packet, or raise ``MetricReportInvalid``.

    Supplied expectations are external comparison inputs, not customer approval.
    Without them a internally consistent historical packet remains unverified
    against the current system and the plan frozen before execution.
    """
    _canonical_bytes(payload)
    _object(payload, {"format_version", "definition_version", "report_id", "created_at", "versions",
                      "plan_sha256", "sampling_description", "limitations", "run", "cases", "metrics", "reviews"},
            "report")
    _require(type(payload["format_version"]) is int and payload["format_version"] == FORMAT_VERSION,
             "Unsupported report format")
    _require(payload["definition_version"] == DEFINITION_VERSION, "Metric definition version drift")
    _identity(payload["report_id"], "report_id")
    created = _date(payload["created_at"], "created_at")
    _versions(payload["versions"], "versions")
    if expected_versions is not None:
        _versions(expected_versions, "expected_versions")
        _require(payload["versions"] == expected_versions, "Report differs from expected current versions")
    _text(payload["sampling_description"], "sampling_description", limit=4000)
    _list(payload["limitations"], "limitations", 100)
    _require(bool(payload["limitations"]), "At least one explicit scope limitation is required")
    for limitation in payload["limitations"]:
        _text(limitation, "limitation", limit=2000)
    run = payload["run"]
    _object(run, {"run_id", "operator_id", "artifact_sha256", "started_at", "ended_at",
                  "versions_start", "versions_end"}, "run")
    for field in ("run_id", "operator_id"):
        _identity(run[field], f"run.{field}")
    _hash(run["artifact_sha256"], "run.artifact_sha256")
    started, ended = _date(run["started_at"], "run.started_at"), _date(run["ended_at"], "run.ended_at")
    _require(started <= ended <= created, "Run/report dates are out of order")
    for boundary in ("versions_start", "versions_end"):
        _versions(run[boundary], f"run.{boundary}")
        _require(run[boundary] == payload["versions"], "Versions changed during the evaluated run")
    _list(payload["cases"], "cases", MAX_CASES)
    cases, frozen_sources = {}, {}
    for case in payload["cases"]:
        _object(case, {"case_id", "dataset", "task_type", "producer_id", "execution_status",
                       "executed_at", "answer_sha256", "reference_sha256", "source_bindings"}, "case")
        for field in ("case_id", "task_type", "producer_id"):
            _identity(case[field], f"case.{field}")
        _require(case["case_id"] not in cases, "Duplicate case ID")
        _require(case["dataset"] in {"native", "social", "cross"}, "case.dataset: invalid collection")
        _require(case["execution_status"] in {"pending", "succeeded", "failed", "invalid"}, "Invalid execution status")
        _sources(case["source_bindings"])
        for source in case["source_bindings"]:
            _require(source["dataset"] == "external" or case["dataset"] in {source["dataset"], "cross"},
                     "Case source belongs to a different dataset")
            identity = (source["dataset"], source["record_id"])
            _require(identity not in frozen_sources or frozen_sources[identity] == source,
                     "Source versions or hashes drift between cases in the same run")
            frozen_sources[identity] = source
        if case["reference_sha256"] is not None:
            _hash(case["reference_sha256"], "reference_sha256")
        if case["execution_status"] == "pending":
            _require(case["answer_sha256"] is None and case["executed_at"] is None,
                     "Pending case cannot claim an executed answer")
        else:
            _hash(case["answer_sha256"], "answer_sha256")
            _require(started <= _date(case["executed_at"], "executed_at") <= ended,
                     "Case execution date is outside its run")
        cases[case["case_id"]] = case
    _list(payload["metrics"], "metrics", MAX_METRIC_GROUPS)
    _require(bool(payload["metrics"]), "A report must declare at least one metric stratum")
    groups, samples, sample_groups = set(), {}, {}
    planned_count = 0
    for metric in payload["metrics"]:
        _object(metric, {"metric_id", "dataset", "task_type", "citation_source_type", "review_policy", "unit_plan", "samples"}, "metric")
        _require(metric["metric_id"] in {row["id"] for row in METRICS}, "Unknown metric ID")
        key = _metric_key(metric)
        _require(key not in groups, "Duplicate metric stratum")
        groups.add(key)
        _identity(metric["task_type"], "metric.task_type")
        _require(metric["dataset"] in {"native", "social", "cross"}, "metric.dataset: invalid collection")
        source_type = metric["citation_source_type"]
        _require((metric["metric_id"] == "M10" and source_type in {"article_quote", "structured_result", "external_page"})
                 or (metric["metric_id"] != "M10" and source_type is None), "M10 must be reported separately by citation source type")
        policy = metric["review_policy"]
        _object(policy, {"required_reviewers", "method", "policy_record", "independent_units"}, "review policy")
        _require(type(policy["required_reviewers"]) is int and 1 <= policy["required_reviewers"] <= 5,
                 "Review policy requires 1 to 5 named independent reviewers")
        _require(policy["method"] in {"source_reading", "receipt_audit"}, "Invalid review method")
        _require(policy["method"] != "receipt_audit" or metric["metric_id"] in {"M02", "M10", "M15", "M16"},
                 "Semantic metrics require source-reading review, rather than only receipt audit")
        _identity(policy["policy_record"], "policy_record")
        _require(type(policy["independent_units"]) is bool, "independent_units requires an explicit boolean")
        _require(not policy["independent_units"] or metric["metric_id"] in {"M01", "M02", "M05", "M11", "M12", "M13"},
                 "Clustered statement, citation, set or rubric units cannot claim a binomial interval")
        _list(metric["unit_plan"], "unit_plan", MAX_CASES)
        planned, declared_cases = set(), set()
        for plan in metric["unit_plan"]:
            _object(plan, {"case_id", "unit_ids"}, "unit plan")
            _require(plan["case_id"] in cases and plan["case_id"] not in declared_cases, "Unknown or duplicate planned case")
            declared_cases.add(plan["case_id"])
            _identities(plan["unit_ids"], "unit_ids")
            planned.update((plan["case_id"], unit) for unit in plan["unit_ids"])
            if metric["metric_id"] in _SET_METRICS | {"M15", "M16"}:
                _require(len(plan["unit_ids"]) == 1, "Set, latency and cost metrics require one sample per planned case")
            if metric["metric_id"] in {"M01", "M02", "M05", "M11", "M12", "M13"}:
                _require(len(plan["unit_ids"]) == 1, "Task metrics require one outcome per planned case")
        eligible = {identifier for identifier, case in cases.items()
                    if (case["dataset"], case["task_type"]) == (metric["dataset"], metric["task_type"])}
        _require(declared_cases == eligible, "Metric plan must retain every case in its dataset/task stratum")
        planned_count += len(planned)
        _require(planned_count <= MAX_SAMPLES, "Too many planned metric samples")
        _list(metric["samples"], "samples", MAX_SAMPLES)
        observed = set()
        for sample in metric["samples"]:
            _object(sample, {"sample_id", "case_id", "unit_id", "answer_sha256", "reference_sha256", "source_bindings_sha256", "value"}, "sample")
            _identity(sample["sample_id"], "sample_id")
            pair = (sample["case_id"], sample["unit_id"])
            _require(pair in planned and pair not in observed, "Unknown or duplicate observed unit")
            observed.add(pair)
            _require(sample["sample_id"] not in samples, "Duplicate sample ID")
            case = cases[sample["case_id"]]
            _require(case["execution_status"] != "pending", "Pending case cannot supply observed samples")
            _require(sample["answer_sha256"] == case["answer_sha256"] and sample["reference_sha256"] == case["reference_sha256"],
                     "Sample answer/reference binding differs from the case")
            _require(sample["source_bindings_sha256"] == source_bindings_sha256(case["source_bindings"]),
                     "Sample source-version binding differs from the case")
            _value(metric["metric_id"], sample["value"])
            _require(case["execution_status"] != "invalid" or sample["value"] is None, "Invalid execution cannot be scored")
            if case["execution_status"] == "failed" and metric["metric_id"] in {"M01", "M14"}:
                _require(sample["value"] in (None, False), "Failed execution cannot pass task or required-point coverage")
            if case["execution_status"] == "failed" and metric["metric_id"] == "M12":
                _require(sample["value"] in (None, False), "Operational failure is not an unnecessary refusal")
            samples[sample["sample_id"]], sample_groups[sample["sample_id"]] = sample, metric
    _hash(payload["plan_sha256"], "plan_sha256")
    _require(payload["plan_sha256"] == metric_plan_sha256(payload), "Declared metric plan hash differs")
    if expected_plan_sha256 is not None:
        _hash(expected_plan_sha256, "expected_plan_sha256")
        _require(payload["plan_sha256"] == expected_plan_sha256, "Report differs from the externally frozen metric plan")
    _list(payload["reviews"], "reviews", MAX_REVIEWS)
    review_ids, reviewer_names, reviewer_ids_by_name, reviewer_units = set(), {}, {}, set()
    dates_by_sample = {}
    for review in payload["reviews"]:
        _object(review, {"review_id", "reviewer_id", "reviewer_name", "role", "independence_declared", "method",
                         "reviewed_at", "definition_version", "versions", "evidence_record", "bindings"}, "review")
        for field in ("review_id", "reviewer_id", "evidence_record"):
            _identity(review[field], field)
        _text(review["reviewer_name"], "reviewer_name", limit=256)
        _require(review["review_id"] not in review_ids, "Duplicate review ID")
        review_ids.add(review["review_id"])
        reviewer_id, name = review["reviewer_id"], review["reviewer_name"].strip().casefold()
        _require(reviewer_id not in reviewer_names or reviewer_names[reviewer_id] == name, "Reviewer identity changes its name")
        _require(name not in reviewer_ids_by_name or reviewer_ids_by_name[name] == reviewer_id, "Same reviewer name cannot inflate independent review counts")
        reviewer_names[reviewer_id], reviewer_ids_by_name[name] = name, reviewer_id
        _require(review["role"] in {"independent_reviewer", "adjudicator"} and review["independence_declared"] is True,
                 "Named review must explicitly declare independence")
        _require(reviewer_id != run["operator_id"], "Run operator cannot independently review their run")
        _require(review["definition_version"] == payload["definition_version"] and review["versions"] == payload["versions"],
                 "Review belongs to different metric or system versions")
        reviewed_at = _date(review["reviewed_at"], "reviewed_at")
        _require(ended <= reviewed_at <= created, "Review date must follow the run and precede the report")
        _list(review["bindings"], "review bindings", MAX_SAMPLES)
        _require(bool(review["bindings"]), "Review must bind at least one observed sample")
        for binding in review["bindings"]:
            _object(binding, {"sample_id", "metric_key", "run_id", "case_id", "unit_id", "answer_sha256", "reference_sha256",
                              "source_bindings_sha256", "value_sha256", "decision"}, "review binding")
            _require(binding["sample_id"] in samples, "Review binds an unknown sample")
            sample, metric = samples[binding["sample_id"]], sample_groups[binding["sample_id"]]
            case = cases[sample["case_id"]]
            _require(reviewer_id != case["producer_id"], "Answer/reference producer cannot independently review its result")
            _require(binding["run_id"] == run["run_id"] and all(binding[field] == sample[field] for field in
                     ("case_id", "unit_id", "answer_sha256", "reference_sha256", "source_bindings_sha256")),
                     "Review run/case/answer/reference/source binding differs")
            _require(binding["metric_key"] == list(_metric_key(metric)) and
                     binding["value_sha256"] == observation_sha256(sample["value"]),
                     "Review metric or candidate-value binding differs")
            _require(sample["value"] is not None and sample["reference_sha256"] is not None and case["execution_status"] != "invalid",
                     "Review cannot approve an absent value, reference, or invalid execution")
            _require(review["method"] == metric["review_policy"]["method"], "Review method differs from frozen policy")
            _require(binding["decision"] in {"agree", "disagree"}, "Review decision must retain agreement or disagreement")
            reviewer_unit = (reviewer_id, binding["sample_id"])
            _require(reviewer_unit not in reviewer_units, "Duplicate reviewer/sample decision or self-adjudication")
            reviewer_units.add(reviewer_unit)
            dates_by_sample.setdefault(binding["sample_id"], []).append((review["role"], reviewed_at))
    for dates in dates_by_sample.values():
        independent_dates = [date for role, date in dates if role == "independent_reviewer"]
        for role, date in dates:
            if role == "adjudicator":
                _require(bool(independent_dates) and date >= max(independent_dates),
                         "Adjudication must follow the independent decisions it resolves")
    return deepcopy(payload)


def validate_metric_report(payload: dict, *, expected_versions: dict | None = None,
                           expected_plan_sha256: str | None = None) -> dict:
    """Return a detached validated packet or raise ``MetricReportInvalid``.

    Expected versions and plan digest are external comparison inputs. They do
    not establish execution provenance, approved review authority or acceptance.
    """
    try:
        return _validate_metric_report(payload, expected_versions, expected_plan_sha256)
    except (TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise MetricReportInvalid("Malformed report field type or structure") from exc


def _metric_key(metric):
    return tuple(metric[field] for field in ("metric_id", "dataset", "task_type", "citation_source_type"))


def _review_complete(sample, policy, reviews):
    if sample is None or sample["value"] is None or sample["reference_sha256"] is None:
        return False
    independent = [decision for role, decision in reviews if role == "independent_reviewer"]
    adjudication = [decision for role, decision in reviews if role == "adjudicator"]
    if len(independent) < policy["required_reviewers"]:
        return False
    if "disagree" in adjudication:
        return False
    if "disagree" not in independent:
        return True
    return bool(adjudication) and all(decision == "agree" for decision in adjudication)


def summarize_metric_report(payload: dict, *, expected_versions: dict | None = None,
                            expected_plan_sha256: str | None = None) -> dict:
    """Derive per-stratum results and retain a detached raw packet for audit.

    ``complete`` describes coverage of the declared plan only. Primary results
    stay null until every planned unit is reviewed and usable. Conditional subset
    arithmetic always carries counts and never establishes customer acceptance.
    """
    packet = validate_metric_report(payload, expected_versions=expected_versions,
                                    expected_plan_sha256=expected_plan_sha256)
    cases = {case["case_id"]: case for case in packet["cases"]}
    review_decisions = {}
    for review in packet["reviews"]:
        for binding in review["bindings"]:
            review_decisions.setdefault(binding["sample_id"], []).append((review["role"], binding["decision"]))
    summaries = []
    for metric in packet["metrics"]:
        by_unit = {(sample["case_id"], sample["unit_id"]): sample for sample in metric["samples"]}
        planned = [(plan["case_id"], unit) for plan in metric["unit_plan"] for unit in plan["unit_ids"]]
        scored, pending, reviewed = [], 0, 0
        for unit in planned:
            sample = by_unit.get(unit)
            complete = _review_complete(sample, metric["review_policy"],
                                        review_decisions.get(sample["sample_id"], []) if sample else [])
            reviewed += int(complete)
            if complete and metric["metric_id"] in _SET_METRICS:
                complete = sample["value"]["gold_complete"]
            if complete and metric["metric_id"] == "M16":
                complete = sample["value"]["ledger_complete"]
            if complete:
                scored.append(sample)
            else:
                pending += 1
        case_ids = [plan["case_id"] for plan in metric["unit_plan"]]
        complete = bool(planned) and pending == 0 and all(cases[c]["execution_status"] in {"succeeded", "failed"} for c in case_ids)
        status = "complete" if complete else ("partial" if scored else "pending")
        subset = _arithmetic(metric, scored, pending, cases)
        if not complete and metric["metric_id"] in _BINARY_METRICS:
            subset["rate"], subset["wilson95"] = None, None
        coverage = {"planned_cases": len(case_ids), "executed_cases": sum(cases[c]["execution_status"] != "pending" for c in case_ids),
                    "pending_cases": sum(cases[c]["execution_status"] == "pending" for c in case_ids),
                    "failed_cases": sum(cases[c]["execution_status"] == "failed" for c in case_ids),
                    "invalid_cases": sum(cases[c]["execution_status"] == "invalid" for c in case_ids),
                    "planned_units": len(planned), "observed_units": len(by_unit), "reviewed_units": reviewed,
                    "evaluated_units": len(scored), "pending_units": pending, "pending_review_units": len(planned) - reviewed}
        summaries.append({**{key: deepcopy(metric[key]) for key in ("metric_id", "dataset", "task_type", "citation_source_type", "review_policy")},
                          "status": status, "coverage": coverage,
                          "result": subset if complete else None, "reviewed_subset_result": subset if scored else None})
    return {"report_id": packet["report_id"], "definition_version": packet["definition_version"],
            "created_at": packet["created_at"], "versions": deepcopy(packet["versions"]),
            "run_id": packet["run"]["run_id"], "evaluation_started_at": packet["run"]["started_at"],
            "evaluation_ended_at": packet["run"]["ended_at"], "plan_sha256": packet["plan_sha256"],
            "sampling_description": packet["sampling_description"], "limitations": deepcopy(packet["limitations"]),
            "review_records": [{**{key: review[key] for key in ("review_id", "reviewer_id", "reviewer_name", "role",
                                                                 "independence_declared", "method", "reviewed_at", "evidence_record")},
                                "bound_sample_count": len(review["bindings"])} for review in packet["reviews"]],
            "current_versions_checked": expected_versions is not None,
            "externally_frozen_plan_checked": expected_plan_sha256 is not None,
            "status": "complete" if all(row["status"] == "complete" for row in summaries) else
                      ("partial" if any(row["coverage"]["evaluated_units"] for row in summaries) else "pending"),
            "review_policy_authority": "team_methodology_declaration",
            "client_acceptance": "not_established", "semantic_truth": "not_established_by_validation",
            "notice": "Coverage and arithmetic of declared evidence only; execution, reviewer authority, source meaning and acceptance require separate verification.",
            "metrics": summaries, "raw_packet": packet}


def _arithmetic(metric, samples, pending, cases):
    identifier = metric["metric_id"]
    if identifier in _BINARY_METRICS:
        result = binary_result([sample["value"] for sample in samples] + [None] * pending)
        if not metric["review_policy"]["independent_units"]:
            result["wilson95"] = None
        return result
    if identifier in _SET_METRICS:
        rows = [set_result(sample["value"]["expected_ids"], sample["value"]["returned_ids"], gold_complete=True)
                for sample in samples]
        tp, fp, fn = (sum(row[field] for row in rows) for field in ("tp", "fp", "fn"))
        denominator = tp + (fp if identifier in {"M03", "M06"} else fn)
        return {"numerator": tp, "denominator": denominator, "rate": tp / denominator if denominator else None,
                "tp": tp, "fp": fp, "fn": fn, "aggregation": "micro_over_case_identity_pairs"}
    if identifier == "M15":
        rows = [{"execution_status": cases[sample["case_id"]]["execution_status"], **sample["value"]} for sample in samples]
        groups = {}
        for row in rows:
            key = (row["execution_status"], row["cache_state"], row["temperature_state"])
            groups.setdefault(key, []).append(row["latency_ms"])
        return {"timing_boundary": "submission_to_usable_result_or_terminal_failure",
                "strata": [{"execution_status": key[0], "cache_state": key[1], "temperature_state": key[2], **latency_summary(values)}
                           for key, values in sorted(groups.items())]}
    settled = sum((Decimal(str(sample["value"]["settled_usd"])) for sample in samples), Decimal(0))
    return {"settled_usd": float(settled), "submitted_tasks": len(samples),
            "usd_per_submitted_task": float(settled / len(samples)) if samples else None}


def load_metric_report(text: str | bytes, *, expected_versions: dict | None = None,
                       expected_plan_sha256: str | None = None) -> dict:
    """Strictly decode bounded JSON and return its derived, validated report."""
    _require(type(text) in (str, bytes), "Report JSON must be text or bytes")
    try:
        size = len(text.encode("utf-8")) if type(text) is str else len(text)
        _require(size <= MAX_JSON_BYTES, "Report exceeds the bounded JSON size")

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                _require(key not in result, "Duplicate JSON key")
                result[key] = value
            return result

        def reject_constant(_value):
            raise MetricReportInvalid("Nonfinite JSON value")

        payload = json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except MetricReportInvalid:
        raise
    except (UnicodeError, ValueError, OverflowError, RecursionError) as exc:
        raise MetricReportInvalid("Malformed report JSON") from exc
    return summarize_metric_report(payload, expected_versions=expected_versions,
                                   expected_plan_sha256=expected_plan_sha256)
