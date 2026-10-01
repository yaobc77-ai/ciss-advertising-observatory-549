"""Offline grading and receipt regressions; service/database/API are stubs."""

import copy
import importlib.util
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory.models import Filters

SCRIPT = Path(__file__).parents[1] / "scripts" / "run_selftest.py"
SPEC = importlib.util.spec_from_file_location("selftest_runner", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

COUNT = {"id": "S01", "expect": "count", "constraints": {"publisher": "The New York Times"}}
GROUP = {"id": "S06", "expect": "group", "group_by": "publisher", "constraints": {"sponsor": "exxonmobil"}}
SHARE = {"id": "S19", "expect": "share", "constraints": {"publisher": "CNBC"}, "denominator_constraints": {}}
NO_COUNT = {"id": "S15", "expect": "no_count"}
RETRIEVAL = {"id": "R01", "gold_phrase": "needle", "en": "en", "zh": "zh", "paraphrase": "paraphrase"}


def answer_for(case=COUNT, total=0):
    dimension = case.get("group_by")
    return SimpleNamespace(
        answer_mode="statistics", status="answered", failure_reason="", cost_usd=0.0,
        structured_result={
            "kind": {"publisher": "list_publishers", "sponsor": "list_sponsors"}.get(dimension, case["expect"]),
            "method": "database", "group_by": dimension + "s" if dimension else None,
            "filters": runner.expected_filters(case).model_dump(mode="json"),
            "collections": [{"dataset": "native", "total": total}], "groups": [],
        })


def share_answer(numerator=4, denominator=268):
    answer = answer_for(SHARE, numerator)
    answer.structured_result.update(
        denominator_filters=Filters(dataset="native").model_dump(mode="json"),
        denominator_basis="current_selection_before_question_targets")
    answer.structured_result["collections"][0].update(
        numerator=numerator, denominator=denominator,
        percentage=100 * numerator / denominator if denominator else None,
        percentage_status="defined" if denominator else "empty_selection")
    return answer


def share_gold(numerator=4, denominator=268):
    return {"numerator": numerator, "denominator": denominator,
            "percentage": 100 * numerator / denominator if denominator else None}


@pytest.mark.parametrize("status,failure", [
    ("service_unavailable", ""), ("limited", ""), ("insufficient_evidence", ""),
    ("answered", "rate_limited"),
])
def test_count_never_passes_unavailable_or_failed_answer(status, failure):
    answer = answer_for()
    answer.status, answer.failure_reason = status, failure
    assert not runner.judge(COUNT, 0, answer)


def test_zero_count_is_valid_only_with_correct_full_scope():
    answer = answer_for()
    assert runner.judge(COUNT, 0, answer)
    answer.structured_result["filters"]["publishers"] = ["CNBC"]
    assert not runner.judge(COUNT, 0, answer)


@pytest.mark.parametrize("key,value", [
    ("dataset", "social"), ("sponsors", ["bp"]), ("keywords", ["carbon"]),
    ("labels", ["greenwashing"]), ("platforms", ["Twitter"]), ("record_ids", ["r1"]),
    ("date_from", "2030-01-01"), ("include_unknown_dates", False),
    ("include_unknown_dates", "true"),
])
def test_unused_filter_changes_cannot_pass_on_equal_total(key, value):
    answer = answer_for()
    answer.structured_result["filters"][key] = value
    assert not runner.judge(COUNT, 0, answer)


def test_missing_or_extra_filter_keys_fail_instead_of_filling_defaults():
    answer = answer_for()
    del answer.structured_result["filters"]["labels"]
    assert not runner.judge(COUNT, 0, answer)
    answer = answer_for()
    answer.structured_result["filters"]["other"] = "unexpected"
    assert not runner.judge(COUNT, 0, answer)


@pytest.mark.parametrize("collections", [
    [], [{"dataset": "social", "total": 0}], [{"dataset": "native", "total": False}],
    [{"dataset": "native", "total": 0.0}], [{"dataset": "native", "total": -1}],
    [{"dataset": "native", "total": 0}, {"dataset": "native", "total": 0}],
    [None], None,
])
def test_malformed_duplicate_or_wrong_dataset_collections_fail(collections):
    answer = answer_for()
    answer.structured_result["collections"] = collections
    assert not runner.judge(COUNT, 0, answer)


def test_result_method_kind_and_dimension_are_required():
    for key, value in (("kind", "share"), ("method", "model"), ("group_by", "publishers")):
        answer = answer_for()
        answer.structured_result[key] = value
        assert not runner.judge(COUNT, 0, answer)


def test_group_requires_dataset_dimension_unique_names_and_matching_total():
    answer = answer_for(GROUP, 2)
    answer.structured_result["groups"] = [{"dataset": "native", "name": "CNBC", "count": 2}]
    assert runner.judge(GROUP, {"CNBC": 2}, answer)
    for change in ("dimension", "dataset", "duplicate", "total", "bool_count"):
        damaged = copy.deepcopy(answer)
        if change == "dimension":
            damaged.structured_result["group_by"] = "sponsors"
        elif change == "dataset":
            damaged.structured_result["groups"][0]["dataset"] = "social"
        elif change == "duplicate":
            damaged.structured_result["groups"] *= 2
        elif change == "total":
            damaged.structured_result["collections"][0]["total"] = 0
        else:
            damaged.structured_result["groups"][0]["count"] = True
        assert not runner.judge(GROUP, {"CNBC": 2}, damaged), change


@pytest.mark.parametrize("status,mode,failure,expected", [
    ("answered", "rag", "", True), ("answered", "tools", "", True),
    ("insufficient_evidence", "clarification", "", True),
    ("insufficient_evidence", "rag", "", False), ("limited", "tools", "", False),
    ("service_unavailable", "rag", "", False), ("answered", "rag", "provider_error", False),
    ("answered", "statistics", "", False),
])
def test_negative_cases_only_pass_available_nonstatistics_routes(status, mode, failure, expected):
    answer = SimpleNamespace(status=status, answer_mode=mode, failure_reason=failure, structured_result=None)
    assert runner.judge(NO_COUNT, None, answer) is expected


def test_count_disguised_as_tools_is_not_a_negative_case_pass():
    answer = answer_for()
    answer.answer_mode = "tools"
    assert not runner.judge(NO_COUNT, None, answer)


def test_percentage_cannot_be_replaced_by_count_or_distribution():
    assert runner.judge(SHARE, share_gold(), share_answer())
    assert not runner.judge(SHARE, share_gold(), answer_for(SHARE, 4))
    assert not runner.judge(SHARE, share_gold(), answer_for(GROUP, 268))


@pytest.mark.parametrize("key,value", [
    ("numerator", 5), ("denominator", 267), ("percentage", 4),
    ("percentage", True), ("percentage", float("nan")), ("percentage", float("inf")),
    ("percentage_status", "empty_selection"),
])
def test_percentage_checks_all_arithmetic_fields(key, value):
    answer = share_answer()
    answer.structured_result["collections"][0][key] = value
    assert not runner.judge(SHARE, share_gold(), answer)


def test_percentage_denominator_scope_and_basis_must_be_trusted_selection():
    answer = share_answer()
    answer.structured_result["denominator_filters"]["publishers"] = ["CNBC"]
    assert not runner.judge(SHARE, share_gold(), answer)
    answer = share_answer()
    answer.structured_result["denominator_basis"] = "retrieved_records"
    assert not runner.judge(SHARE, share_gold(), answer)


def test_empty_denominator_requires_null_instead_of_zero_percent():
    answer = share_answer(0, 0)
    assert runner.judge(SHARE, share_gold(0, 0), answer)
    answer.structured_result["collections"][0]["percentage"] = 0
    assert not runner.judge(SHARE, share_gold(0, 0), answer)


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.queries = []

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        return Cursor(next(self.replies))


def test_share_gold_independently_queries_numerator_and_denominator():
    conn = Connection([[(4,)], [(268,)]])
    assert runner.gold_statistics(conn, SHARE) == share_gold()
    numerator, denominator = conn.queries
    assert "r.dataset='native'" in numerator[0] and "countable" in numerator[0]
    assert numerator[1] == ["CNBC"] and denominator[1] == []
    assert "publisher" not in denominator[0]


def test_date_gold_excludes_unknown_dates_and_scope_says_so():
    case = {"expect": "count", "constraints": {"years": [2015, 2016]}}
    conn = Connection([[(0,)]])
    runner.gold_statistics(conn, case)
    assert "BETWEEN" in conn.queries[0][0]
    scope = runner.expected_filters(case)
    assert str(scope.date_from) == "2015-01-01" and str(scope.date_to) == "2016-12-31"
    assert scope.include_unknown_dates is False


def test_retrieval_gold_uses_native_countable_accepted_intervals():
    conn = Connection([[  # SQL excludes social, inactive, noncountable and nonretrievable records.
        ("included", "Needle tail", {}),
        ("navigation", "needle body", {"retrieval_ranges": [[7, 11]]}),
        ("outside_prefix", "body needle", {"retrieval_end": 4}),
        ("across_gap", "neeX dle", {"retrieval_ranges": [[0, 3], [5, 8]]}),
    ]])
    assert runner.gold_records(conn, "needle") == {"included"}
    sql = conn.queries[0][0]
    assert "r.active" in sql and "r.dataset='native'" in sql and "countable" in sql and "retrievable" in sql
    assert "chunks" not in sql


def test_invalid_source_intervals_fail_gold_instead_of_using_full_body():
    conn = Connection([[("bad", "needle", {"retrieval_ranges": [[0, 99]]})]])
    with pytest.raises(ValueError):
        runner.gold_records(conn, "needle")


def test_empty_retrieval_gold_skips_all_paid_variants(monkeypatch):
    monkeypatch.setattr(runner, "gold_records", lambda *_: set())
    rows = runner.pending_rows([RETRIEVAL], retrieval=True)
    runner.run_retrieval(object(), object(), object(), True, [RETRIEVAL], rows, lambda: None)
    assert rows[0]["run_status"] == "invalid_gold" and not rows[0]["pass"]
    assert all(state["status"] == "not_run" for state in rows[0]["variants"].values())


def test_retrieval_saves_completed_variant_and_embedding_exposure_before_exception(monkeypatch):
    monkeypatch.setattr(runner, "gold_records", lambda *_: {"gold"})
    calls = []

    def embed(texts, *, visitor, cost_sink):
        calls.append(texts[0])
        cost_sink.append(0.01)
        if texts[0] == "zh":
            raise ValueError("private DSN should not be saved")
        return [[0.1]]

    service = SimpleNamespace(rag=SimpleNamespace(embed=embed), db=SimpleNamespace(
        search=lambda *args, **kwargs: [SimpleNamespace(record_id="gold")]))
    rows = runner.pending_rows([RETRIEVAL], retrieval=True)
    snapshots = []
    with pytest.raises(ValueError):
        runner.run_retrieval(service, object(), SimpleNamespace(embedding_model="test"), True,
                             [RETRIEVAL], rows, lambda: snapshots.append(copy.deepcopy(rows)))
    assert calls == ["en", "zh"]
    assert rows[0]["en"] and not rows[0]["zh"] and not rows[0]["paraphrase"]
    assert rows[0]["variants"]["en"]["cost_usd"] == 0.01
    assert rows[0]["variants"]["zh"] == {"status": "failed", "cost_usd": 0.01, "error_type": "ValueError"}
    assert snapshots[-1] == rows and "private DSN" not in json.dumps(rows)


@pytest.mark.parametrize("ids", ["", "S01,S01", "S99", "S01,", "S01,R99"])
def test_selection_rejects_unknown_empty_and_duplicate_ids(ids):
    with pytest.raises(ValueError):
        runner.select_cases(ids)


def test_selection_is_bounded_and_keeps_case_file_order():
    statistics, retrieval = runner.select_cases("R01,S19,S01")
    assert [case["id"] for case in statistics] == ["S01", "S19"]
    assert [case["id"] for case in retrieval] == ["R01"]


def test_atomic_receipt_replaces_only_reserved_file_and_uses_lf(tmp_path):
    path, older = tmp_path / "current.json", tmp_path / "older.json"
    path.write_bytes(b"")
    older.write_bytes(b"keep")
    runner.atomic_receipt(path, {"status": "running"})
    runner.atomic_receipt(path, {"status": "failed"})
    assert json.loads(path.read_text())["status"] == "failed" and older.read_bytes() == b"keep"
    assert b"\r\n" not in path.read_bytes() and not list(tmp_path.glob("*.tmp"))


def test_failed_atomic_replace_retains_last_valid_receipt(tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    path.write_text('{"status":"running"}', encoding="utf-8")
    monkeypatch.setattr(runner.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("write failed")))
    with pytest.raises(OSError):
        runner.atomic_receipt(path, {"status": "failed"})
    assert json.loads(path.read_text())["status"] == "running" and not list(tmp_path.glob("*.tmp"))


@pytest.fixture
def stub_main(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    case_file = tmp_path / "cases.json"
    case_file.write_bytes(runner.CASE_BYTES)
    monkeypatch.setattr(runner, "CASE_FILE", case_file)
    settings = SimpleNamespace(generation_model="stub", embedding_model="stub", database_url="not-connected",
                               research_agent_enabled=True, requests_per_minute=5)
    monkeypatch.setattr(runner, "Settings", SimpleNamespace(from_env=lambda: settings))
    health = {"status": "ok", "data_version": "d1", "index_version": "i1", "active_profile": "sentence"}
    monkeypatch.setattr(runner, "Service", lambda _: SimpleNamespace(health=lambda: copy.deepcopy(health)))
    monkeypatch.setattr(runner.psycopg, "connect", lambda _: nullcontext(object()))
    monkeypatch.setattr(runner, "implementation_identity", lambda: {"code_commit": "stub", "source_sha256": {}})

    def complete(service, conn, cases, rows, checkpoint):
        for row in rows:
            row.update(run_status="completed", **{"pass": True})
            checkpoint()

    monkeypatch.setattr(runner, "run_rules", complete)
    return tmp_path, health


def read_receipt(root):
    paths = list((root / "outputs").glob("selftest-*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def test_main_success_binds_rubric_runner_implementation_and_index(stub_main):
    root, _ = stub_main
    assert runner.main(["--rules", "--ids", "S01"]) == 0
    report = read_receipt(root)
    assert report["run_status"] == "passed" and report["summary"]["rules"] == "1/1"
    assert len(report["runner_sha256"]) == len(report["cases_sha256"]) == 64
    assert report["health_start"] == report["health_end"] and report["implementation"]["code_commit"] == "stub"
    assert report["evaluation_scope"] == "self_made_development_checks"


def test_main_exception_retains_completed_and_pending_cases_returns_nonzero(stub_main, monkeypatch):
    root, _ = stub_main

    def fail(service, conn, cases, rows, checkpoint):
        rows[0].update(run_status="completed", **{"pass": True})
        rows[1]["run_status"] = "running"
        checkpoint()
        raise RuntimeError("postgres://private-password")

    monkeypatch.setattr(runner, "run_rules", fail)
    assert runner.main(["--rules", "--ids", "S01,S02,S03"]) == 1
    report = read_receipt(root)
    assert [row["run_status"] for row in report["rules"]] == ["completed", "failed", "not_run"]
    assert report["summary"]["rules"] == "1/3" and report["summary"]["rules_completed"] == 1
    assert report["error_type"] == "RuntimeError" and "private-password" not in json.dumps(report)


@pytest.mark.parametrize("changed", ["data_version", "index_version", "active_profile"])
def test_main_changed_data_or_index_invalidates_without_discarding_results(stub_main, monkeypatch, changed):
    root, health = stub_main

    def complete_and_change(service, conn, cases, rows, checkpoint):
        rows[0].update(run_status="completed", **{"pass": True})
        checkpoint()
        health[changed] += "changed"

    monkeypatch.setattr(runner, "run_rules", complete_and_change)
    assert runner.main(["--rules", "--ids", "S01"]) == 1
    report = read_receipt(root)
    assert report["rules"][0]["pass"] and report["run_status"] == "failed"
    assert report["failure_code"] == ("data_changed" if changed == "data_version" else "index_changed")


def test_main_grading_failure_has_nonzero_exit(stub_main, monkeypatch):
    root, _ = stub_main
    monkeypatch.setattr(runner, "run_rules", lambda service, conn, cases, rows, checkpoint:
                        rows[0].update(run_status="completed", **{"pass": False}))
    assert runner.main(["--rules", "--ids", "S01"]) == 1
    assert read_receipt(root)["summary"]["rules"] == "0/1"


def test_main_case_bytes_change_invalidates_completed_run(stub_main, monkeypatch):
    root, _ = stub_main

    def changed(service, conn, cases, rows, checkpoint):
        rows[0].update(run_status="completed", **{"pass": True})
        runner.CASE_FILE.write_bytes(runner.CASE_BYTES + b"\n")

    monkeypatch.setattr(runner, "run_rules", changed)
    assert runner.main(["--rules", "--ids", "S01"]) == 1
    assert read_receipt(root)["failure_code"] == "evaluation_artifacts_changed"


def test_main_missing_index_identity_fails_before_database_access(stub_main, monkeypatch):
    root, health = stub_main
    health["index_version"] = None
    monkeypatch.setattr(runner.psycopg, "connect", lambda _: pytest.fail("must not open database"))
    assert runner.main(["--rules", "--ids", "S01"]) == 1
    assert read_receipt(root)["rules"][0]["run_status"] == "not_run"


def test_main_end_service_unavailable_never_passes_on_equal_versions(stub_main, monkeypatch):
    root, health = stub_main

    def unavailable(service, conn, cases, rows, checkpoint):
        rows[0].update(run_status="completed", **{"pass": True})
        health["status"] = "unavailable"

    monkeypatch.setattr(runner, "run_rules", unavailable)
    assert runner.main(["--rules", "--ids", "S01"]) == 1
    report = read_receipt(root)
    assert report["data_version_unchanged"] and report["index_unchanged"]
    assert report["failure_code"] == "end_service_unavailable" and report["run_status"] == "failed"


def test_per_case_receipt_keeps_observed_scope_and_denominator_omits_body_and_url():
    answer = share_answer()
    answer.structured_result.update(records=[{"body": "private body", "url": "private url"}])
    answer.structured_result["filters"]["publishers"] = ["Forbes"]
    row = runner.pending_rows([SHARE])[0]
    runner.record_answer(row, SHARE, share_gold(), answer)
    assert row["filters"]["publishers"] == ["Forbes"] and not row["checks"]["scope"]
    assert row["denominator_filters"] == Filters(dataset="native").model_dump(mode="json")
    assert row["denominator_basis"] == "current_selection_before_question_targets"
    assert row["kind"] == "share" and row["method"] == "database" and row["group_by"] is None
    assert "private body" not in json.dumps(row) and "private url" not in json.dumps(row)


def test_paid_guard_and_inapplicable_ids_fail_before_configuration(monkeypatch):
    monkeypatch.setattr(runner, "Settings", SimpleNamespace(from_env=lambda: pytest.fail("must not read settings")))
    for args in (["--agent", "--ids", "S01"], ["--rules", "--ids", "R01"]):
        with pytest.raises(SystemExit) as exc:
            runner.main(args)
        assert exc.value.code == 2
