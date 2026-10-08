"""Synthetic publication-boundary checks, not customer performance scores."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from plotly.utils import PlotlyJSONEncoder
from test_metric_report import _packet, _review

from observatory.evaluation_report import published_report
from observatory.evaluation_ui import evaluation_panel
from observatory.metric_report import MAX_JSON_BYTES


def _settings(tmp_path, packet=None):
    packet = packet if packet is not None else _review(_packet())
    data = json.dumps(packet).encode()
    path = tmp_path / "private-report.json"
    path.write_bytes(data)
    return SimpleNamespace(evaluation_report_path=str(path),
                           evaluation_report_sha256=hashlib.sha256(data).hexdigest(),
                           evaluation_plan_sha256=packet["plan_sha256"])


def _render(publication):
    return json.dumps(evaluation_panel(publication), cls=PlotlyJSONEncoder)


def test_no_report_is_the_default_and_does_not_require_any_file():
    assert published_report(SimpleNamespace()) == {"state": "pending", "report": None}


@pytest.mark.parametrize("missing", ["evaluation_report_path", "evaluation_report_sha256", "evaluation_plan_sha256"])
def test_partial_configuration_cannot_load_a_score(tmp_path, missing):
    settings = _settings(tmp_path)
    setattr(settings, missing, "")
    assert published_report(settings) == {"state": "unavailable", "report": None}


@pytest.mark.parametrize("mutation", ["file_changed", "missing_file", "plan_changed", "bad_schema", "oversized"])
def test_invalid_report_never_leaks_diagnostics_or_supplies_a_score(tmp_path, mutation):
    settings = _settings(tmp_path)
    path = tmp_path / "private-report.json"
    if mutation == "file_changed":
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "missing_file":
        path.unlink()
    elif mutation == "plan_changed":
        settings.evaluation_plan_sha256 = "b" * 64
    elif mutation == "bad_schema":
        data = b'{"secret":"DO_NOT_PUBLISH"}'
        path.write_bytes(data)
        settings.evaluation_report_sha256 = hashlib.sha256(data).hexdigest()
    else:
        data = b" " * (MAX_JSON_BYTES + 1)
        path.write_bytes(data)
        settings.evaluation_report_sha256 = hashlib.sha256(data).hexdigest()
    result = published_report(settings)
    assert result == {"state": "unavailable", "report": None}
    rendered = _render(result)
    assert "failed validation" in rendered
    assert str(path) not in rendered and "DO_NOT_PUBLISH" not in rendered
    assert "50.0%" not in rendered
    assert "Some measurements available" not in rendered


def test_public_projection_is_aggregate_only_and_cannot_establish_currentness(tmp_path):
    result = published_report(_settings(tmp_path))
    assert result["state"] == "available"
    report = result["report"]
    assert report["applicability"] == "frozen_run_only"
    assert report["client_acceptance"] == "not_established"
    serialized = json.dumps(report)
    for private in ("raw_packet", "synthetic-reviewer", "Synthetic reviewer", "synthetic-audit-record",
                    "synthetic-operator", "synthetic-producer", "synthetic-ad", "synthetic-team-policy"):
        assert private not in serialized
    assert report["reviewer_count"] == 1
    assert report["metrics"][0]["result"]["rate"] == 0.5
    rendered = _render(result)
    assert "50.0%" in rendered and "1 / 2" in rendered
    assert "has not been verified" in rendered
    assert "task acceptance pending" in rendered
    assert "independent_units" not in rendered


def test_pending_review_stays_pending_despite_hash_pinning(tmp_path):
    result = published_report(_settings(tmp_path, _packet()))
    assert result["state"] == "available"
    metric = result["report"]["metrics"][0]
    assert metric["result"] is None
    assert metric["coverage"]["pending_review_units"] == 2
    rendered = _render(result)
    assert "Not evaluated" in rendered and "Review or reference completion pending" in rendered
    assert "50.0%" not in rendered


def test_partial_result_is_only_shown_as_an_explicit_subset_with_failure_coverage(tmp_path):
    packet = _packet(pending=1)
    packet["cases"][1]["execution_status"] = "failed"
    result = published_report(_settings(tmp_path, _review(packet)))
    metric = result["report"]["metrics"][0]
    assert metric["result"] is None
    assert metric["coverage"]["failed_cases"] == 1
    rendered = _render(result)
    assert "Evaluated subset only" in rendered and "incomplete result" in rendered
    assert "Executed 2 / 3 cases; failed 1; invalid 0" in rendered
    assert "Reviewed 2 / 3 units; evaluated 2; pending 1" in rendered
    assert "50.0%" in rendered


@pytest.mark.parametrize("metric,values,expected", [
    ("M03", ({"expected_ids": [], "returned_ids": [], "gold_complete": True},), "Not applicable: no denominator"),
    ("M15", ({"latency_ms": 120, "cache_state": "uncached", "temperature_state": "warm",
               "timing_boundary": "submission_to_usable_result_or_terminal_failure"},), "P50 120.0 ms; P95 120.0 ms; n=1"),
    ("M16", ({"settled_usd": 0.004, "ledger_complete": True},), "$0.00400 per submitted task"),
])
def test_metric_units_are_rendered_without_turning_undefined_values_into_perfect_scores(tmp_path, metric, values, expected):
    packet = _review(_packet(metric, values=values))
    rendered = _render(published_report(_settings(tmp_path, packet)))
    assert expected in rendered
    assert "100.0%" not in rendered


def test_changed_candidate_after_review_is_rejected_even_with_new_file_pin(tmp_path):
    packet = _review(_packet())
    packet["metrics"][0]["samples"][0]["value"] = False
    assert published_report(_settings(tmp_path, packet))["state"] == "unavailable"


@pytest.mark.parametrize("metric", ["M03", "M04", "M06", "M07"])
def test_partial_set_rates_remain_visible_only_as_the_evaluated_subset(tmp_path, metric):
    values = ({"expected_ids": ["a", "b"], "returned_ids": ["a", "x"], "gold_complete": True},)
    packet = _review(_packet(metric, values=values, pending=1))
    publication = published_report(_settings(tmp_path, packet))
    assert publication["report"]["metrics"][0]["result"] is None
    rendered = _render(publication)
    assert "Evaluated subset only" in rendered
    assert "50.0%" in rendered and "1 / 2" in rendered
    assert "Not applicable: no denominator" not in rendered
