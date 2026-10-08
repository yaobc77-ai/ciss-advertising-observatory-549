"""Public evaluation definitions cannot masquerade as measured acceptance."""

from observatory.evaluation_scorecard import METRICS, REQUIREMENTS, pending_scorecard


def test_each_customer_requirement_has_consistent_metric_coverage():
    metrics = {row["id"]: row for row in METRICS}
    requirements = {row["id"]: row for row in REQUIREMENTS}
    assert set(requirements) == {f"RQ{number}" for number in range(1, 7)}
    assert len(metrics) == len(METRICS)
    for identifier, row in requirements.items():
        assert set(row["metric_ids"]) == {
            key for key, metric in metrics.items() if identifier in metric["requirements"]
        }
        assert row["prerequisite"]
    assert all(set(metric["requirements"]) <= requirements.keys() for metric in METRICS)


def test_missing_independent_results_remain_unknown_and_unapproved():
    report = pending_scorecard()
    assert report["status"] == "not_evaluated"
    assert all(row["status"] == "not_evaluated" for row in report["requirements"])
    for metric in report["metrics"]:
        assert metric["result"] is None
        assert metric["numerator"] is None
        assert metric["denominator"] is None
        assert metric["status"] == "not_evaluated"
        for field in ("definition", "numerator_definition", "denominator_definition",
                      "formula", "unit", "method", "direction"):
            assert metric[field]
    assert "overall_pass" not in report


def test_a_rendered_copy_cannot_modify_future_public_scorecards():
    first = pending_scorecard()
    first["metrics"][0]["result"] = 1.0
    first["metrics"][0]["requirements"].clear()
    first["requirements"][0]["metric_ids"].clear()
    fresh = pending_scorecard()
    assert fresh["metrics"][0]["result"] is None
    assert fresh["metrics"][0]["requirements"]
    assert fresh["requirements"][0]["metric_ids"]
