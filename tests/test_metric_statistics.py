"""Arithmetic cannot promote incomplete reviews into published quality scores."""

import math

import pytest

from observatory.metric_statistics import binary_result, latency_summary, set_result


def test_no_binary_tasks_is_not_perfect_quality():
    assert binary_result([]) == {"passed": 0, "evaluated": 0, "planned": 0,
                                 "pending": 0, "rate": None, "reviewed_rate": None,
                                 "wilson95": None}


def test_pending_outcome_blocks_complete_rate_and_interval():
    result = binary_result([True, False, None])
    assert (result["passed"], result["evaluated"], result["planned"], result["pending"]) == (1, 2, 3, 1)
    assert result["rate"] is None and result["wilson95"] is None
    assert result["reviewed_rate"] == 0.5


def test_all_pending_has_no_conditional_rate_either():
    result = binary_result([None, None])
    assert result["evaluated"] == 0 and result["pending"] == 2
    assert result["rate"] is None and result["reviewed_rate"] is None


def test_complete_binary_result_has_wilson_interval():
    result = binary_result([True] * 5 + [False] * 5)
    assert result["rate"] == result["reviewed_rate"] == 0.5
    assert result["wilson95"]["lower"] == pytest.approx(0.236593090512564)
    assert result["wilson95"]["upper"] == pytest.approx(0.763406909487436)


@pytest.mark.parametrize("outcome,edge", [(True, "upper"), (False, "lower")])
def test_all_success_or_failure_does_not_make_interval_zero_width(outcome, edge):
    result = binary_result([outcome] * 8)
    assert result["rate"] == int(outcome)
    assert result["wilson95"][edge] == pytest.approx(int(outcome))
    assert result["wilson95"]["upper"] > result["wilson95"]["lower"]


@pytest.mark.parametrize("value", [1, 0, 1.0, "pass", "false", {}, []])
def test_binary_rejects_coerced_or_nonboolean_results(value):
    with pytest.raises(ValueError, match="no coercion"):
        binary_result([True, value])


@pytest.mark.parametrize("function,args", [(binary_result, ((),)),
                                           (latency_summary, ((),)),
                                           (set_result, ((), [])),
                                           (set_result, ([], ()))])
def test_input_contract_requires_lists(function, args):
    with pytest.raises(TypeError):
        function(*args, **({"gold_complete": True} if function is set_result else {}))


def test_complete_sets_deduplicate_identity_without_hiding_errors():
    result = set_result(["A", "A", "B"], ["B", "C", "C"], gold_complete=True)
    assert result == {"gold_complete": True, "expected_unique": 2, "returned_unique": 2,
                      "tp": 1, "fp": 1, "fn": 1, "precision": 0.5,
                      "recall": 0.5, "f1": 0.5, "exact_match": False}


def test_complete_exact_set_ignores_order_and_duplicates():
    result = set_result(["A", "B"], ["B", "A", "B"], gold_complete=True)
    assert result["exact_match"] is True
    assert result["precision"] == result["recall"] == result["f1"] == 1


def test_identity_comparison_does_not_guess_aliases_or_trim_ids():
    result = set_result(["A", "A "], ["a"], gold_complete=True)
    assert (result["tp"], result["fp"], result["fn"]) == (0, 1, 2)
    assert result["precision"] == result["recall"] == result["f1"] == 0


@pytest.mark.parametrize("expected,returned", [(["A"], ["A", "B"]), ([], [])])
def test_partial_gold_does_not_score_semantic_membership(expected, returned):
    result = set_result(expected, returned, gold_complete=False)
    assert result["expected_unique"] == len(set(expected))
    assert result["returned_unique"] == len(set(returned))
    for field in ("tp", "fp", "fn", "precision", "recall", "f1", "exact_match"):
        assert result[field] is None


def test_empty_complete_sets_match_without_inventing_precision_or_recall():
    result = set_result([], [], gold_complete=True)
    assert result["exact_match"] is True
    assert result["tp"] == result["fp"] == result["fn"] == 0
    assert result["precision"] is None and result["recall"] is None and result["f1"] is None


@pytest.mark.parametrize("expected,returned,precision,recall", [
    (["A"], [], None, 0), ([], ["A"], 0, None),
])
def test_one_empty_set_preserves_defined_and_undefined_denominators(expected, returned, precision, recall):
    result = set_result(expected, returned, gold_complete=True)
    assert result["exact_match"] is False
    assert result["precision"] == precision and result["recall"] == recall and result["f1"] == 0


@pytest.mark.parametrize("value", ["", "  ", None, 1, True])
@pytest.mark.parametrize("side", ["expected", "returned"])
def test_identity_lists_reject_blank_and_nonstring_members(value, side):
    values = {"expected": ["A"], "returned": ["A"]}
    values[side].append(value)
    with pytest.raises(ValueError, match="blank identity"):
        set_result(**values, gold_complete=True)


@pytest.mark.parametrize("value", [1, 0, None, "true"])
def test_gold_completeness_is_explicit_boolean(value):
    with pytest.raises(ValueError, match="explicit boolean"):
        set_result([], [], gold_complete=value)


def test_no_latency_observations_has_no_quantiles():
    assert latency_summary([]) == {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}


def test_latency_median_and_nearest_rank_p95_are_distinct():
    result = latency_summary(list(range(20, 0, -1)))
    assert result == {"count": 20, "p50_ms": 10.5, "p95_ms": 19.0, "max_ms": 20.0}


def test_small_latency_sample_p95_is_an_observed_value():
    assert latency_summary([200, 0, 25]) == {"count": 3, "p50_ms": 25.0,
                                            "p95_ms": 200.0, "max_ms": 200.0}


def test_single_latency_observation_is_preserved():
    assert latency_summary([500]) == {"count": 1, "p50_ms": 500.0, "p95_ms": 500.0, "max_ms": 500.0}


def test_finite_large_latency_median_does_not_overflow():
    result = latency_summary([1e308, 1e308])
    assert math.isfinite(result["p50_ms"]) and result["p50_ms"] == 1e308


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), float("-inf"),
                                   True, False, "10", None, 10 ** 1000])
def test_invalid_latency_does_not_become_zero_or_disappear(value):
    with pytest.raises(ValueError):
        latency_summary([10, value])
