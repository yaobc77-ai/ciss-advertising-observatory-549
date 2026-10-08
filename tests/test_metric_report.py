"""Synthetic contract evidence; these tests are not customer performance results."""

import json
from copy import deepcopy

import pytest

from observatory.evaluation_scorecard import DEFINITION_VERSION
from observatory.metric_report import (
    MAX_JSON_BYTES,
    VERSION_FIELDS,
    MetricReportInvalid,
    load_metric_report,
    metric_plan_sha256,
    observation_sha256,
    source_bindings_sha256,
    summarize_metric_report,
    validate_metric_report,
)


def _digest(character="a"):
    return character * 64


def _freeze(packet):
    packet["plan_sha256"] = metric_plan_sha256(packet)
    return packet


def _packet(metric_id="M01", values=(True, False), pending=0, *, method="source_reading", required=1):
    versions = {field: _digest() for field in VERSION_FIELDS}
    versions["active_profile"] = "synthetic-test-profile"
    cases, plan, samples = [], [], []
    for number in range(len(values) + pending):
        value = values[number] if number < len(values) else None
        case_id = f"synthetic-case-{number}"
        executed = number < len(values)
        sources = [{"record_id": "synthetic-ad", "dataset": "native", "version_id": "synthetic-version",
                    "body_sha256": _digest("b"), "payload_sha256": _digest("c")}]
        case = {"case_id": case_id, "dataset": "native", "task_type": "synthetic-task", "producer_id": "synthetic-producer",
                "execution_status": "succeeded" if executed else "pending", "executed_at": "2026-10-05T10:01:00+00:00" if executed else None,
                "answer_sha256": _digest("d") if executed else None, "reference_sha256": _digest("e"), "source_bindings": sources}
        cases.append(case)
        plan.append({"case_id": case_id, "unit_ids": ["synthetic-unit"]})
        if executed:
            samples.append({"sample_id": f"synthetic-sample-{number}", "case_id": case_id, "unit_id": "synthetic-unit",
                            "answer_sha256": case["answer_sha256"], "reference_sha256": case["reference_sha256"],
                            "source_bindings_sha256": source_bindings_sha256(sources), "value": value})
    packet = {"format_version": 1, "definition_version": DEFINITION_VERSION, "report_id": "synthetic-report",
              "created_at": "2026-10-05T12:00:00+00:00", "versions": versions, "plan_sha256": _digest(),
              "sampling_description": "Synthetic deterministic contract fixture; no execution or human review is claimed.",
              "limitations": ["This fixture is not a production evaluation."],
              "run": {"run_id": "synthetic-run", "operator_id": "synthetic-operator", "artifact_sha256": _digest("f"),
                      "started_at": "2026-10-05T10:00:00+00:00", "ended_at": "2026-10-05T10:02:00+00:00",
                      "versions_start": deepcopy(versions), "versions_end": deepcopy(versions)},
              "cases": cases, "metrics": [{"metric_id": metric_id, "dataset": "native", "task_type": "synthetic-task",
                                              "citation_source_type": "article_quote" if metric_id == "M10" else None,
                                              "review_policy": {"required_reviewers": required, "method": method,
                                                                "policy_record": "synthetic-team-policy", "independent_units": False},
                                              "unit_plan": plan, "samples": samples}], "reviews": []}
    return _freeze(packet)


def _review(packet, *, reviewer="synthetic-reviewer", name="Synthetic reviewer", decision="agree",
            role="independent_reviewer", reviewed_at="2026-10-05T11:00:00+00:00", sample_ids=None):
    bindings = []
    for metric in packet["metrics"]:
        for sample in metric["samples"]:
            if sample_ids is not None and sample["sample_id"] not in sample_ids:
                continue
            binding = {key: sample[key] for key in ("sample_id", "case_id", "unit_id", "answer_sha256", "reference_sha256", "source_bindings_sha256")}
            binding.update(run_id=packet["run"]["run_id"], value_sha256=observation_sha256(sample["value"]),
                           metric_key=[metric[key] for key in ("metric_id", "dataset", "task_type", "citation_source_type")], decision=decision)
            bindings.append(binding)
    packet["reviews"].append({"review_id": f"synthetic-review-{len(packet['reviews'])}", "reviewer_id": reviewer, "reviewer_name": name,
                              "role": role, "independence_declared": True, "method": packet["metrics"][0]["review_policy"]["method"],
                              "reviewed_at": reviewed_at, "definition_version": packet["definition_version"], "versions": deepcopy(packet["versions"]),
                              "evidence_record": "synthetic-audit-record", "bindings": bindings})
    return packet


def test_partial_review_cannot_publish_a_final_score_or_omit_failed_attempts():
    packet = _packet(pending=1)
    packet["cases"][1]["execution_status"] = "failed"
    report = summarize_metric_report(_review(packet))
    metric = report["metrics"][0]
    assert report["status"] == metric["status"] == "partial"
    assert metric["result"] is None
    assert metric["coverage"] == {"planned_cases": 3, "executed_cases": 2, "pending_cases": 1, "failed_cases": 1, "invalid_cases": 0,
                                  "planned_units": 3, "observed_units": 2, "reviewed_units": 2, "evaluated_units": 2,
                                  "pending_units": 1, "pending_review_units": 1}
    assert metric["reviewed_subset_result"]["reviewed_rate"] == 0.5
    assert metric["reviewed_subset_result"]["rate"] is None
    assert metric["reviewed_subset_result"]["wilson95"] is None
    assert report["client_acceptance"] == "not_established"
    assert report["semantic_truth"] == "not_established_by_validation"


def test_one_batched_receipt_review_is_supported_by_the_frozen_team_policy():
    packet = _review(_packet("M02", method="receipt_audit"))
    result = summarize_metric_report(packet)["metrics"][0]
    assert result["status"] == "complete"
    assert result["result"]["rate"] == 0.5
    assert result["result"]["wilson95"] is None
    assert summarize_metric_report(packet)["review_policy_authority"] == "team_methodology_declaration"


def test_all_unexecuted_or_unreviewed_is_pending_and_has_no_substitute_zero_score():
    for packet in (_packet(values=(), pending=2), _packet(), _packet(values=())):
        metric = summarize_metric_report(packet)["metrics"][0]
        assert metric["status"] == "pending"
        assert metric["result"] is None and metric["reviewed_subset_result"] is None


@pytest.mark.parametrize("metric_id", ["M01", "M14"])
def test_an_operational_failure_cannot_be_relabelled_success(metric_id):
    packet = _packet(metric_id, values=(True,))
    packet["cases"][0]["execution_status"] = "failed"
    with pytest.raises(MetricReportInvalid, match="Failed execution"):
        validate_metric_report(packet)


def test_timeout_is_not_an_unnecessary_refusal():
    packet = _packet("M12", values=(True,))
    packet["cases"][0]["execution_status"] = "failed"
    with pytest.raises(MetricReportInvalid, match="Operational failure"):
        validate_metric_report(packet)


@pytest.mark.parametrize("field", sorted(VERSION_FIELDS))
def test_each_version_and_active_retrieval_profile_must_remain_stable(field):
    packet = _packet()
    packet["run"]["versions_end"][field] = "different-profile" if field == "active_profile" else _digest("b")
    with pytest.raises(MetricReportInvalid, match="Versions changed"):
        validate_metric_report(packet)


def test_internal_consistency_does_not_claim_currentness_or_preexecution_plan_verification():
    packet = _review(_packet())
    report = summarize_metric_report(packet)
    assert report["current_versions_checked"] is False
    assert report["externally_frozen_plan_checked"] is False
    report = summarize_metric_report(packet, expected_versions=deepcopy(packet["versions"]), expected_plan_sha256=packet["plan_sha256"])
    assert report["current_versions_checked"] and report["externally_frozen_plan_checked"]
    expected = deepcopy(packet["versions"])
    expected["prompt_version"] = _digest("b")
    with pytest.raises(MetricReportInvalid, match="expected current versions"):
        validate_metric_report(packet, expected_versions=expected)


def test_removing_a_failed_case_cannot_match_an_externally_frozen_plan():
    packet = _review(_packet())
    frozen_digest = packet["plan_sha256"]
    packet["cases"].pop()
    packet["metrics"][0]["unit_plan"].pop()
    packet["metrics"][0]["samples"].pop()
    packet["reviews"][0]["bindings"].pop()
    _freeze(packet)
    with pytest.raises(MetricReportInvalid, match="externally frozen"):
        validate_metric_report(packet, expected_plan_sha256=frozen_digest)


def test_declared_stratum_cannot_drop_an_executed_or_pending_case():
    packet = _packet(pending=1)
    packet["metrics"][0]["unit_plan"].pop()
    _freeze(packet)
    with pytest.raises(MetricReportInvalid, match="retain every case"):
        validate_metric_report(packet)


@pytest.mark.parametrize("field,new_value", [
    ("run_id", "old-run"), ("case_id", "old-case"), ("unit_id", "old-statement"),
    ("answer_sha256", _digest("b")), ("reference_sha256", _digest("b")),
    ("source_bindings_sha256", _digest("b")), ("value_sha256", _digest("b")),
    ("metric_key", ["M09", "native", "synthetic-task", None]),
])
def test_review_decisions_cannot_be_reused_for_a_different_artifact_or_metric(field, new_value):
    packet = _review(_packet())
    packet["reviews"][0]["bindings"][0][field] = new_value
    with pytest.raises(MetricReportInvalid, match="binding differs"):
        validate_metric_report(packet)


def test_changing_a_candidate_after_review_invalidates_the_review_even_with_the_same_answer_hash():
    packet = _review(_packet())
    packet["metrics"][0]["samples"][0]["value"] = False
    with pytest.raises(MetricReportInvalid, match="candidate-value binding"):
        validate_metric_report(packet)


def test_changing_the_answer_requires_new_sample_and_review_bindings():
    packet = _review(_packet())
    packet["cases"][0]["answer_sha256"] = _digest("b")
    with pytest.raises(MetricReportInvalid, match="Sample answer/reference"):
        validate_metric_report(packet)


def test_same_source_identity_cannot_silently_drift_across_cases():
    packet = _packet()
    packet["cases"][1]["source_bindings"][0]["version_id"] = "changed-original"
    with pytest.raises(MetricReportInvalid, match="drift between cases"):
        validate_metric_report(packet)


@pytest.mark.parametrize("reviewer", ["synthetic-operator", "synthetic-producer"])
def test_operator_or_producer_cannot_supply_independent_review(reviewer):
    with pytest.raises(MetricReportInvalid, match="cannot independently review"):
        validate_metric_report(_review(_packet(), reviewer=reviewer))


def test_duplicated_reviewer_decisions_or_renamed_aliases_cannot_fill_a_two_reviewer_policy():
    packet = _review(_packet(required=2))
    _review(packet)
    with pytest.raises(MetricReportInvalid, match="Duplicate reviewer/sample"):
        validate_metric_report(packet)
    packet = _review(_packet(required=2))
    _review(packet, reviewer="alias-id", name="SYNTHETIC REVIEWER")
    with pytest.raises(MetricReportInvalid, match="inflate independent review"):
        validate_metric_report(packet)


def test_double_coding_disagreement_remains_pending_until_a_distinct_later_adjudication():
    packet = _review(_packet(required=2))
    assert summarize_metric_report(packet)["metrics"][0]["result"] is None
    _review(packet, reviewer="second", name="Second synthetic reviewer", decision="disagree")
    assert summarize_metric_report(packet)["metrics"][0]["result"] is None
    _review(packet, reviewer="third", name="Synthetic adjudicator", role="adjudicator", reviewed_at="2026-10-05T11:30:00+00:00")
    assert summarize_metric_report(packet)["metrics"][0]["result"]["rate"] == 0.5
    assert any(binding["decision"] == "disagree" for review in packet["reviews"] for binding in review["bindings"])


def test_an_adjudication_cannot_precede_the_decisions_or_override_itself():
    packet = _review(_packet(required=2))
    _review(packet, reviewer="second", name="Second synthetic reviewer", decision="disagree")
    _review(packet, reviewer="third", name="Synthetic adjudicator", role="adjudicator", reviewed_at="2026-10-05T10:30:00+00:00")
    with pytest.raises(MetricReportInvalid, match="Adjudication must follow"):
        validate_metric_report(packet)


@pytest.mark.parametrize("metric_id", ["M03", "M04", "M06", "M07"])
def test_incomplete_membership_gold_remains_pending_even_when_every_review_agrees(metric_id):
    value = {"expected_ids": ["ad-1"], "returned_ids": ["ad-1"], "gold_complete": False}
    metric = summarize_metric_report(_review(_packet(metric_id, values=(value,))))["metrics"][0]
    assert metric["coverage"]["reviewed_units"] == 1 and metric["coverage"]["evaluated_units"] == 0
    assert metric["coverage"]["pending_units"] == 1 and metric["coverage"]["pending_review_units"] == 0
    assert metric["result"] is None and metric["reviewed_subset_result"] is None


def test_set_micro_average_keeps_same_ad_separate_across_questions_and_does_not_guess_empty_perfection():
    value = {"expected_ids": ["ad-1", "ad-2"], "returned_ids": ["ad-1", "ad-3"], "gold_complete": True}
    metric = summarize_metric_report(_review(_packet("M06", values=(value, value))))["metrics"][0]
    assert metric["result"] == {"numerator": 2, "denominator": 4, "rate": 0.5, "tp": 2, "fp": 2, "fn": 2,
                                "aggregation": "micro_over_case_identity_pairs"}
    empty = {"expected_ids": [], "returned_ids": [], "gold_complete": True}
    result = summarize_metric_report(_review(_packet("M07", values=(empty,))))["metrics"][0]["result"]
    assert result["rate"] is None and result["denominator"] == 0


def test_duplicate_set_members_are_rejected_instead_of_hiding_inventory_errors():
    value = {"expected_ids": ["ad-1", "ad-1"], "returned_ids": ["ad-1"], "gold_complete": True}
    with pytest.raises(MetricReportInvalid, match="duplicate identity"):
        validate_metric_report(_packet("M07", values=(value,)))


def test_statement_units_cannot_claim_a_binomial_interval_or_only_receipt_audit():
    packet = _review(_packet("M08"))
    assert summarize_metric_report(packet)["metrics"][0]["result"]["wilson95"] is None
    packet["metrics"][0]["review_policy"]["independent_units"] = True
    _freeze(packet)
    with pytest.raises(MetricReportInvalid, match="Clustered"):
        validate_metric_report(packet)
    with pytest.raises(MetricReportInvalid, match="Semantic metrics"):
        validate_metric_report(_packet("M08", method="receipt_audit"))


def test_binary_interval_is_only_available_when_task_independence_is_explicitly_declared():
    packet = _review(_packet())
    packet["metrics"][0]["review_policy"]["independent_units"] = True
    _freeze(packet)
    assert summarize_metric_report(packet)["metrics"][0]["result"]["wilson95"] is not None


def test_pending_case_with_an_unknown_zero_unit_inventory_still_blocks_a_final_score():
    packet = _review(_packet("M08", pending=1))
    packet["metrics"][0]["unit_plan"][-1]["unit_ids"] = []
    _freeze(packet)
    metric = summarize_metric_report(packet)["metrics"][0]
    assert metric["status"] == "partial" and metric["result"] is None
    assert metric["reviewed_subset_result"]["rate"] is None


@pytest.mark.parametrize("source_type", [None, "combined", "unknown"])
def test_citation_source_types_cannot_be_combined_or_omitted(source_type):
    packet = _packet("M10", method="receipt_audit")
    packet["metrics"][0]["citation_source_type"] = source_type
    _freeze(packet)
    with pytest.raises(MetricReportInvalid, match="separately by citation source type"):
        validate_metric_report(packet)


def _timing(milliseconds):
    return {"latency_ms": milliseconds, "cache_state": "uncached", "temperature_state": "warm",
            "timing_boundary": "submission_to_usable_result_or_terminal_failure"}


def test_latency_preserves_failures_and_cache_states_as_separate_scopes():
    packet = _packet("M15", values=(_timing(20), _timing(100)), method="receipt_audit")
    packet["cases"][1]["execution_status"] = "failed"
    metric = summarize_metric_report(_review(packet))["metrics"][0]
    strata = {row["execution_status"]: row for row in metric["result"]["strata"]}
    assert strata["failed"]["p95_ms"] == 100
    assert strata["succeeded"]["p50_ms"] == 20
    assert metric["coverage"]["failed_cases"] == 1


def test_server_only_timing_cannot_be_claimed_as_user_end_to_end_waiting():
    value = _timing(20)
    value["timing_boundary"] = "server_call"
    with pytest.raises(MetricReportInvalid, match="complete user timing boundary"):
        validate_metric_report(_packet("M15", values=(value,), method="receipt_audit"))


def test_cost_uses_submitted_failed_tasks_and_cannot_publish_incomplete_ledger_as_final():
    values = ({"settled_usd": 0.1, "ledger_complete": True}, {"settled_usd": 0.2, "ledger_complete": True})
    packet = _packet("M16", values=values, method="receipt_audit")
    packet["cases"][1]["execution_status"] = "failed"
    result = summarize_metric_report(_review(packet))["metrics"][0]["result"]
    assert result == {"settled_usd": 0.3, "submitted_tasks": 2, "usd_per_submitted_task": 0.15}
    values = ({"settled_usd": 0.1, "ledger_complete": True}, {"settled_usd": 0.2, "ledger_complete": False})
    report = summarize_metric_report(_review(_packet("M16", values=values, method="receipt_audit")))
    assert report["metrics"][0]["result"] is None
    assert report["metrics"][0]["coverage"]["pending_units"] == 1
    assert report["raw_packet"]["metrics"][0]["samples"][1]["value"]["settled_usd"] == 0.2


@pytest.mark.parametrize("field", ["overall_accuracy", "client_acceptance", "summary", "score"])
def test_input_cannot_supply_precomputed_scores_or_self_granted_acceptance(field):
    packet = _packet()
    packet[field] = 1.0
    with pytest.raises(MetricReportInvalid, match="unexpected or missing fields"):
        validate_metric_report(packet)


@pytest.mark.parametrize("target", ["case", "sample", "unit", "metric", "review"])
def test_duplicate_ids_or_units_are_rejected(target):
    packet = _review(_packet())
    if target == "case":
        packet["cases"].append(deepcopy(packet["cases"][0]))
    elif target == "sample":
        packet["metrics"][0]["samples"][1]["sample_id"] = packet["metrics"][0]["samples"][0]["sample_id"]
    elif target == "unit":
        packet["metrics"][0]["unit_plan"][0]["unit_ids"].append("synthetic-unit")
    elif target == "metric":
        packet["metrics"].append(deepcopy(packet["metrics"][0]))
    else:
        packet["reviews"].append(deepcopy(packet["reviews"][0]))
    with pytest.raises(MetricReportInvalid, match="[Dd]uplicate"):
        validate_metric_report(packet)


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"nested":{"a":1,"a":2}}'])
def test_duplicate_json_keys_cannot_hide_contradictory_fields(text):
    with pytest.raises(MetricReportInvalid, match="Duplicate JSON key"):
        load_metric_report(text)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_json_nonfinite_numbers_are_rejected_at_every_depth(token):
    packet = _packet("M15", values=(_timing(20),), method="receipt_audit")
    text = json.dumps(packet).replace('"latency_ms": 20', f'"latency_ms": {token}')
    with pytest.raises(MetricReportInvalid, match="[Nn]onfinite|finite JSON"):
        load_metric_report(text)


@pytest.mark.parametrize("value", [True, "20", -1, float("nan"), float("inf"), 10**1000])
def test_bad_numeric_measurements_cannot_be_coerced_or_silently_discarded(value):
    with pytest.raises(MetricReportInvalid):
        validate_metric_report(_packet("M15", values=(_timing(value),), method="receipt_audit"))


@pytest.mark.parametrize("mutation", [
    lambda p: p["cases"][0].update(executed_at="2026-10-05T09:59:00+00:00"),
    lambda p: p["reviews"][0].update(reviewed_at="2026-10-05T10:01:00+00:00"),
    lambda p: p.update(created_at="2026-10-05T12:00:00"),
    lambda p: p["reviews"][0].update(independence_declared=False),
    lambda p: p["reviews"][0]["versions"].update(rubric_version=_digest("b")),
    lambda p: p.update(definition_version="older-definitions"),
    lambda p: p["cases"][0].update(case_id=[]),
    lambda p: p["metrics"][0].update(metric_id=[]),
])
def test_malformed_dates_independence_versions_and_field_types_are_controlled_errors(mutation):
    packet = _review(_packet())
    mutation(packet)
    with pytest.raises(MetricReportInvalid):
        validate_metric_report(packet)


def test_oversized_or_malformed_json_is_rejected():
    for text in ('{"bad":', " " * (MAX_JSON_BYTES + 1), b"\xff", '{"a": "\\ud800"}', '{"a": ' + '9' * 5000 + '}'):
        with pytest.raises(MetricReportInvalid):
            load_metric_report(text)


def test_validated_raw_and_derived_copies_cannot_mutate_the_input_or_future_report():
    packet = _review(_packet())
    report = load_metric_report(json.dumps(packet))
    report["raw_packet"]["metrics"][0]["samples"][0]["value"] = False
    report["metrics"][0]["coverage"]["planned_units"] = 0
    assert packet["metrics"][0]["samples"][0]["value"] is True
    assert summarize_metric_report(packet)["metrics"][0]["result"]["rate"] == 0.5


def test_public_projection_preserves_scope_dates_and_declared_named_review_records():
    packet = _review(_packet())
    report = summarize_metric_report(packet)
    assert report["sampling_description"] == packet["sampling_description"]
    assert report["limitations"] == packet["limitations"]
    assert report["run_id"] == packet["run"]["run_id"]
    assert report["evaluation_ended_at"] == packet["run"]["ended_at"]
    assert report["review_records"][0]["reviewer_name"] == "Synthetic reviewer"
    assert report["review_records"][0]["bound_sample_count"] == 2
    assert "bindings" not in report["review_records"][0]
