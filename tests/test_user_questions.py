"""Offline checks of the realistic user-question set (no database or API)."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SET = json.loads((ROOT / "eval" / "user_questions" / "cases.json").read_text(encoding="utf-8"))
SELFTEST = json.loads((ROOT / "eval" / "selftest" / "cases.json").read_text(encoding="utf-8"))
SNAPSHOT = SELFTEST["facets_snapshot"]
CASES = SET["cases"]


def normal(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


def test_ids_fields_and_gold_types_are_well_formed():
    assert len({c["id"] for c in CASES}) == len(CASES)
    for case in CASES:
        assert case["pd"] in {f"Q{i}" for i in range(1, 7)}
        assert case["persona"] in SET["personas"]
        assert case["source"] in {"new", "client_seen"}
        assert case["gold"]["type"] in SET["gold_types"]
        assert case["must_say"] and all(item.strip() for item in case["must_say"])
        assert (case["expect"] == "behavior") == (case["gold"]["type"] == "behavior")


def test_every_research_question_is_covered():
    covered = {c["pd"] for c in CASES}
    assert covered == {f"Q{i}" for i in range(1, 7)}
    # Keep README, reports and the frozen set in agreement (client_seen U01, U08, U09, U36).
    assert [c["id"] for c in CASES if c["source"] == "client_seen"] == ["U01", "U08", "U09", "U36"]
    assert sum(c["source"] == "new" for c in CASES) == 34


def test_gold_entities_are_real_source_values():
    for case in CASES:
        specs = [case["gold"], *case["gold"].get("sets", []),
                 case["gold"].get("numerator", {}), case["gold"].get("denominator", {})]
        for spec in specs:
            assert set(spec.get("publisher", [])) <= set(SNAPSHOT["publishers"])
            assert set(spec.get("sponsor", [])) <= set(SNAPSHOT["sponsors"])


def test_new_questions_do_not_reuse_development_wording():
    development = {normal(c["question"]) for c in SELFTEST["statistics"]}
    for case in CASES:
        if case["source"] == "new":
            assert normal(case["question"]) not in development, case["id"]
