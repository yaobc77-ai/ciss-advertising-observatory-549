"""Fresh synthetic checks for gate boundaries; no customer or historical fixtures."""

import json
import os
from pathlib import Path

import pytest

from scripts import run_project_checks as checks


def test_inventory_lists_protected_names_without_reading_contents(tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    paths = {tests / name for name in (
        "test_question_policy.py", "test_content_dummy.py", "test_new_core_independent.py",
    )}
    monkeypatch.setattr(Path, "rglob", lambda self, pattern: iter(paths) if pattern == "test_*.py" else iter(()))
    original_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda self: self in paths or original_is_file(self))

    def refuse_read(*args, **kwargs):
        raise AssertionError("Counting test module names must not read their contents.")

    monkeypatch.setattr(Path, "read_text", refuse_read)
    monkeypatch.setattr(Path, "read_bytes", refuse_read)
    result = checks.test_inventory(tmp_path)
    assert result["candidate_module_count"] == 3
    assert result["excluded_module_count"] == 2
    assert result["development_collectable_module_files"] == ["tests/test_new_core_independent.py"]
    assert result["excluded_test_case_count"] is None


def test_new_independent_module_enters_gate_without_a_whitelist(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_new_core_independent.py").touch()
    first = checks.test_inventory(tmp_path)
    (tmp_path / "tests/test_another_core_independent.py").touch()
    second = checks.test_inventory(tmp_path)
    assert first["development_collectable_module_count"] == 1
    assert second["development_collectable_module_count"] == 2


def test_inventory_separates_observed_dependent_exclusions_without_module_reads(tmp_path, monkeypatch):
    tests = tmp_path / "tests"
    paths = {tests / name for name in (
        "test_question_policy.py", "test_content_dummy.py", "test_new_core_independent.py",
        *checks.masking.DEPENDENT_LEGACY_TESTS,
    )}
    monkeypatch.setattr(Path, "rglob", lambda self, pattern: iter(paths) if pattern == "test_*.py" else iter(()))
    original_is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda self: self in paths or original_is_file(self))

    def refuse_read(*args, **kwargs):
        raise AssertionError("Exclusion reasons must not open legacy modules or catalogs.")

    monkeypatch.setattr(Path, "read_text", refuse_read)
    monkeypatch.setattr(Path, "read_bytes", refuse_read)
    result = checks.test_inventory(tmp_path)
    assert result["excluded_module_count"] == 11
    assert result["direct_holdout_module_count"] == 2
    assert result["dependent_legacy_module_count"] == 9
    assert result["development_collectable_module_files"] == ["tests/test_new_core_independent.py"]
    assert len(result["protected_exact_test_names"]) == 7
    assert len(result["protected_test_prefixes"]) == 7
    assert result["protected_exact_source_names"] == [
        "prepare_content_review.py", "prepare_rag_training.py", "prepare_statistical_validity.py",
    ]
    assert result["excluded_test_case_count"] is None
    for name, reason in checks.masking.DEPENDENT_LEGACY_TESTS.items():
        assert result["excluded_module_reasons"]["tests/" + name] == {
            "kind": "dependent_legacy", "reason": reason,
        }


def test_dependent_legacy_exclusions_are_shared_by_pytest_and_ruff(tmp_path):
    excluded = ["tests/" + name for name in checks.masking.DEPENDENT_LEGACY_TESTS]
    commands = dict(checks.check_commands(tmp_path, {"excluded_module_files": excluded}, tmp_path))
    assert all("--ignore=" + path in commands["pytest"] for path in excluded)
    assert ",".join(excluded) in commands["ruff"]


def test_offline_environment_removes_authority_and_external_pytest_options():
    env = checks.offline_environment({
        "OBS_DATABASE_URL": "synthetic-main", "OBS_TEST_DATABASE_URL": "synthetic-test",
        "OBS_EVALUATION_REPORT_PATH": "synthetic-private", "OPENAI_API_KEY": "synthetic-key",
        "PGSERVICE": "synthetic-service", "PYTEST_ADDOPTS": "-m live",
        "OBS_RELEASE_WHEEL": "dist/synthetic.whl", "PATH": "synthetic-path",
    })
    assert not ({"OBS_DATABASE_URL", "OBS_TEST_DATABASE_URL", "OBS_EVALUATION_REPORT_PATH",
                 "OPENAI_API_KEY", "PGSERVICE", "PYTEST_ADDOPTS"} & env.keys())
    assert "OBS_RELEASE_WHEEL" not in env
    assert env["PATH"] == "synthetic-path"
    assert env["PYTHON_DOTENV_DISABLED"] == "1"
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_commands_use_full_tests_masking_and_matching_lint_exclusions(tmp_path):
    assets = tmp_path / "src/observatory/assets"
    assets.mkdir(parents=True)
    (assets / "new.js").touch()
    report = tmp_path / "reports"
    steps = checks.check_commands(report, {"excluded_module_files": ["tests/test_question_policy.py"]}, tmp_path)
    commands = dict(steps)
    assert "run_masked_checks.py" in commands["pytest"][1]
    assert "tests" in commands["pytest"]
    assert checks.MARKERS == "not integration and not live"
    assert "--ignore=tests/test_question_policy.py" in commands["pytest"]
    assert "scripts.run_project_checks" in commands["pytest"]
    assert "faulthandler_timeout=60" in commands["pytest"]
    assert "tests/test_question_policy.py" in commands["ruff"]
    assert commands["javascript_new"] == ["node", "--check", str(assets / "new.js")]


def test_progress_identity_never_includes_parameter_literals_or_nested_separators():
    payload = checks.progress_payload(
        "tests/test_fresh.py::SyntheticClass::test_boundary[synthetic-private::suffix]", 11, "running")
    assert payload["module"] == "tests/test_fresh.py"
    assert payload["function"] == "test_boundary"
    assert payload["completed_test_cases"] == 11
    assert "synthetic-private" not in json.dumps(payload)
    assert "suffix" not in json.dumps(payload)


def test_progress_receipt_has_only_generic_identity_count_and_state():
    payload = checks.progress_payload("tests/test_fresh.py::test_boundary[synthetic-private]", 0, "running")
    assert set(payload) == {"at_utc", "phase", "module", "function", "completed_test_cases"}
    assert payload["function"] == "test_boundary"


def test_progress_windows_reader_lock_defers_replacement_and_recovers(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows file sharing boundary")
    import _winapi

    monkeypatch.setenv("OBS_PROJECT_CHECKS_REPORT_DIR", str(tmp_path))
    monkeypatch.setattr(checks, "_PROGRESS_REPLACE_SHARING_FAILURES", 0)
    monkeypatch.setattr(checks, "_COMPLETED", 1)
    checks.write_progress("running")
    handle = _winapi.CreateFile(str(tmp_path / "progress.json"), _winapi.GENERIC_READ, 1, 0, 3, 0, 0)
    try:
        monkeypatch.setattr(checks, "_COMPLETED", 2)
        checks.write_progress("running")
        assert checks._PROGRESS_REPLACE_SHARING_FAILURES == 1
        assert json.loads((tmp_path / "progress.json").read_text())["completed_test_cases"] == 1
        assert json.loads((tmp_path / "progress.pending.json").read_text())["completed_test_cases"] == 2
    finally:
        _winapi.CloseHandle(handle)
    checks.write_progress("finished")
    assert json.loads((tmp_path / "progress.json").read_text())["completed_test_cases"] == 2
    assert not (tmp_path / "progress.pending.json").exists()


def test_progress_other_permission_errors_still_propagate(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_PROJECT_CHECKS_REPORT_DIR", str(tmp_path))

    def fail_replace(self, destination):
        raise PermissionError("synthetic unrelated permission failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(PermissionError, match="synthetic unrelated permission failure"):
        checks.write_progress("running")


def test_progress_pending_write_errors_still_propagate(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_PROJECT_CHECKS_REPORT_DIR", str(tmp_path))

    def fail_write(path, payload):
        error = PermissionError("synthetic pending-file permission failure")
        error.winerror = 5
        raise error

    monkeypatch.setattr(checks, "write_json", fail_write)
    with pytest.raises(PermissionError, match="synthetic pending-file permission failure"):
        checks.write_progress("running")


def test_junit_counts_preserve_failures_errors_and_skips(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text('<testsuites><testsuite tests="8" failures="2" errors="1" skipped="2"/>'
                      '<testsuite tests="3" failures="0" errors="0" skipped="1"/></testsuites>')
    assert checks.junit_counts(report) == {
        "tests": 11, "failures": 2, "errors": 1, "skipped": 3, "passed": 5,
    }


def make_receipts(tmp_path, *, denied_reads=None, denied_operations=None):
    pytest = tmp_path / "pytest"
    pytest.mkdir()
    checks.write_json(pytest / "mask_receipt.json", {
        "customer_questions_loaded": False, "denied_events": denied_reads or [], "exit_code": 0,
    })
    checks.write_json(pytest / "collection.json", {"selected_test_cases": 1})
    checks.write_json(pytest / "offline_guard.json", {"denied_operations": denied_operations or []})
    (pytest / "junit.xml").write_text('<testsuites><testsuite tests="1"/></testsuites>')


def test_successful_pytest_cannot_hide_a_denied_holdout_read(tmp_path):
    make_receipts(tmp_path, denied_reads=[{"event": "read_denied"}])
    details, errors = checks.verify_pytest_reports(tmp_path, 0)
    assert details["junit_counts"]["passed"] == 1
    assert "mask receipt does not confirm zero denied development reads" in errors


def test_successful_pytest_cannot_hide_a_network_or_database_attempt(tmp_path):
    make_receipts(tmp_path, denied_operations=[{"kind": "database"}])
    _, errors = checks.verify_pytest_reports(tmp_path, 0)
    assert "offline guard denied a network or database operation" in errors


def test_missing_receipts_fail_closed(tmp_path):
    _, errors = checks.verify_pytest_reports(tmp_path, 0)
    assert any("mask_receipt report missing" in error for error in errors)
    assert any("JUnit report missing" in error for error in errors)


def test_clean_receipts_are_parseable_and_exit_codes_must_match(tmp_path):
    make_receipts(tmp_path)
    _, errors = checks.verify_pytest_reports(tmp_path, 0)
    assert errors == []
    _, errors = checks.verify_pytest_reports(tmp_path, 1)
    assert "mask receipt exit code differs from the pytest process" in errors
    assert json.loads((tmp_path / "pytest/collection.json").read_text())["selected_test_cases"] == 1
