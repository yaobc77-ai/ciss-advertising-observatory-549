"""Offline arithmetic for evaluation results, without judging review authority.

These helpers do not run evaluation, approve gold, or establish semantic truth.
Callers must bind observations to an actual run, an agreed task definition, and
the corresponding reviewed sources. A Wilson interval describes binomial
sampling uncertainty under its assumptions; it does not make convenience or
development cases representative of users.
"""

from __future__ import annotations

import math


def binary_result(outcomes: list[bool | None]) -> dict:
    """Summarize one binary outcome per planned evaluation unit.

    ``None`` means pending review or execution, not a failure. The primary rate
    and Wilson 95% interval remain unknown while any planned result is pending.
    ``reviewed_rate`` is explicitly conditional on scored outcomes only. It
    must be shown with evaluated/planned coverage, not as the complete result.
    Failures from an executed, scorable task belong here as ``False``.
    """
    if not isinstance(outcomes, list):
        raise TypeError("outcomes must be a list of boolean or pending values")
    if any(value is not None and type(value) is not bool for value in outcomes):
        raise ValueError("outcomes accept only True, False or None; no coercion")

    planned = len(outcomes)
    pending = sum(value is None for value in outcomes)
    evaluated = planned - pending
    passed = sum(value is True for value in outcomes)
    reviewed_rate = passed / evaluated if evaluated else None
    complete = evaluated > 0 and pending == 0
    rate = reviewed_rate if complete else None
    interval = None
    if complete:
        z = 1.959963984540054
        variance = rate * (1 - rate) / evaluated
        correction = z * z / evaluated
        center = (rate + correction / 2) / (1 + correction)
        half_width = z * math.sqrt(variance + z * z / (4 * evaluated * evaluated)) / (1 + correction)
        interval = {"lower": max(0.0, center - half_width), "upper": min(1.0, center + half_width)}
    return {"passed": passed, "evaluated": evaluated, "planned": planned,
            "pending": pending, "rate": rate, "reviewed_rate": reviewed_rate,
            "wilson95": interval}


def _identity_set(values: list[str], name: str) -> set[str]:
    if not isinstance(values, list):
        raise TypeError(f"{name} must be a list of identity strings")
    if any(type(value) is not str or not value.strip() for value in values):
        raise ValueError(f"{name} contains a non-string or blank identity")
    # Identity is exact: no lowercasing, whitespace edits or alias guesses.
    return set(values)


def set_result(expected: list[str], returned: list[str], *, gold_complete: bool) -> dict:
    """Compare deduplicated record IDs or canonically encoded relationships.

    Incomplete reference lists cannot establish full-set membership errors or
    recall; every semantic result is unknown in that case. Empty complete sets
    may exactly match, but undefined precision/recall/F1 denominators stay null.
    This compares identities only; the caller supplies reviewed membership and
    relationship definitions, and checks relationship weights separately.
    """
    if type(gold_complete) is not bool:
        raise ValueError("gold_complete must be an explicit boolean")
    wanted = _identity_set(expected, "expected")
    actual = _identity_set(returned, "returned")
    result = {"gold_complete": gold_complete, "expected_unique": len(wanted),
              "returned_unique": len(actual), "tp": None, "fp": None, "fn": None,
              "precision": None, "recall": None, "f1": None, "exact_match": None}
    if not gold_complete:
        return result
    tp, fp, fn = len(wanted & actual), len(actual - wanted), len(wanted - actual)
    f1_denominator = 2 * tp + fp + fn
    result.update(tp=tp, fp=fp, fn=fn,
                  precision=tp / len(actual) if actual else None,
                  recall=tp / len(wanted) if wanted else None,
                  f1=2 * tp / f1_denominator if f1_denominator else None,
                  exact_match=wanted == actual)
    return result


def latency_summary(values: list[float]) -> dict:
    """Return median and nearest-rank P95 of finite, nonnegative milliseconds.

    Include the elapsed time of failures in the supplied observations. Callers
    report successful, failed, cached and uncached workloads separately where
    needed. P95 is the sorted value at ``ceil(0.95 * n)`` using one-based ranks;
    it is not an interpolated estimate of unobserved requests.
    """
    if not isinstance(values, list):
        raise TypeError("latencies must be a list of numeric millisecond values")
    observations = []
    for value in values:
        if type(value) not in (int, float):
            raise ValueError("latencies must be numeric, without coercion or booleans")
        try:
            numeric = float(value)
        except OverflowError as exc:
            raise ValueError("latencies must be finite nonnegative milliseconds") from exc
        if not math.isfinite(numeric) or numeric < 0:
            raise ValueError("latencies must be finite nonnegative milliseconds")
        observations.append(numeric)
    observations.sort()
    count = len(observations)
    if not count:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    midpoint = count // 2
    if count % 2:
        median = observations[midpoint]
    else:
        lower, upper = observations[midpoint - 1], observations[midpoint]
        median = lower + (upper - lower) / 2
    return {"count": count, "p50_ms": median,
            "p95_ms": observations[math.ceil(0.95 * count) - 1],
            "max_ms": observations[-1]}
