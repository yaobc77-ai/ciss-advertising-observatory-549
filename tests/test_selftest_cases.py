"""Offline CI half of eval/selftest: case format and rule-planner scopes.

The database-backed half (gold counts by independent SQL, agent and retrieval
runs) is scripts/run_selftest.py. Known rule-planner gaps are strict xfails, so
fixing one fails this test until its case is updated from "gap" to "pass".
"""

import importlib.util
import json
from pathlib import Path

import pytest

from observatory.models import Filters
from observatory.structured_queries import plan_question

SCRIPT = Path(__file__).parents[1] / "scripts" / "run_selftest.py"
SPEC = importlib.util.spec_from_file_location("selftest_case_runner", SCRIPT)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)

CASES = json.loads((Path(__file__).parents[1] / "eval" / "selftest" / "cases.json").read_text(encoding="utf-8"))
FACETS = {**{k: CASES["facets_snapshot"][k] for k in ("publishers", "sponsors")},
          "platforms": [], "keywords": [], "labels": []}


def test_case_ids_are_unique_and_well_formed():
    ids = [c["id"] for c in CASES["statistics"]] + [c["id"] for c in CASES["retrieval"]]
    assert len(ids) == len(set(ids))
    for case in CASES["statistics"]:
        assert case["expect"] in {"count", "group", "share", "no_count"} and case["rules"] in {"pass", "gap"}
        assert ("constraints" in case) == (case["expect"] != "no_count")
        assert (case["expect"] == "group") == ("group_by" in case)
        assert (case["expect"] == "share") == ("denominator_constraints" in case)
        assert "alternatives" not in case
        constraints = case.get("constraints", {})
        assert constraints.get("publisher") in (None, *FACETS["publishers"])
        assert constraints.get("sponsor") in (None, *FACETS["sponsors"])
    for case in CASES["retrieval"]:
        assert all(case[key].strip() for key in ("gold_phrase", "en", "zh", "paraphrase"))


def planned(case):
    plan = plan_question(case["question"], Filters(dataset="native"), FACETS)
    if case["expect"] == "no_count":
        return plan is None or plan.status != "ready"
    if plan is None or plan.status != "ready":
        return False
    dimension = case.get("group_by")
    kind = {"publisher": "list_publishers", "sponsor": "list_sponsors"}.get(dimension, case["expect"])
    matches = (plan.kind == kind and plan.group_by == (dimension + "s" if dimension else None) and
               RUNNER.same_scope(plan.filters.model_dump(mode="json"), RUNNER.expected_filters(case)))
    if case["expect"] == "share":
        matches = matches and plan.denominator_filters is not None and RUNNER.same_scope(
            plan.denominator_filters.model_dump(mode="json"), RUNNER.expected_filters(case, "denominator_constraints"))
    return matches


@pytest.mark.parametrize("case", [c for c in CASES["statistics"] if c["rules"] == "pass"], ids=lambda c: c["id"])
def test_rule_planner_scopes_supported_questions(case):
    assert planned(case)


@pytest.mark.parametrize("case", [
    pytest.param(c, marks=pytest.mark.xfail(strict=True, reason="known rule-planner gap"))
    for c in CASES["statistics"] if c["rules"] == "gap"
], ids=lambda c: c["id"])
def test_rule_planner_known_gaps(case):
    assert planned(case)
