"""Run the complete development-collectable offline gate and preserve its scope.

The customer holdout stays excluded. This is a Python workflow guard, not an OS
sandbox or a customer accuracy evaluation. New independent test modules are
discovered from ``tests`` on every invocation rather than selected by a whitelist.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import run_masked_checks as masking  # noqa: E402

MARKERS = "not integration and not live"
_COLLECTED = 0
_DESELECTED = 0
_COLLECTED_MODULES: set[str] = set()
_DENIED_OPERATIONS: list[dict[str, str]] = []
_COMPLETED = 0
_CURRENT_NODE_ID = ""
_PROGRESS_REPLACE_SHARING_FAILURES = 0
_SOCKETPAIR_CONTEXT = ContextVar("stdlib_socketpair_context", default=False)
_ORIGINAL_SOCKETPAIR = socket.socketpair
_SOCKET_TYPE = socket.socket
_SOCKETPAIR_CONNECTS = 0


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def progress_payload(node_id: str, completed: int, phase: str) -> dict:
    # Remove parameter IDs before splitting: a parameter may itself contain ::.
    identity = node_id.split("[", 1)[0].split("::")
    return {"at_utc": datetime.now(timezone.utc).isoformat(), "phase": phase,
            "module": identity[0], "function": identity[-1] if len(identity) > 1 else "",
            "completed_test_cases": completed}


def write_progress(phase: str) -> None:
    global _PROGRESS_REPLACE_SHARING_FAILURES
    report = os.environ.get("OBS_PROJECT_CHECKS_REPORT_DIR")
    if report:
        directory = Path(report)
        pending = directory / "progress.pending.json"
        write_json(pending, progress_payload(_CURRENT_NODE_ID, _COMPLETED, phase))
        try:
            pending.replace(directory / "progress.json")
        except PermissionError as exc:
            # A Windows reader without delete sharing can hold the old snapshot.
            # Keep it valid and retry at the next update; telemetry is cosmetic.
            if getattr(exc, "winerror", None) not in {5, 32}:
                raise
            _PROGRESS_REPLACE_SHARING_FAILURES += 1


def test_inventory(root: Path = ROOT) -> dict:
    """Inspect names only; never open protected test modules to count their cases."""
    modules = sorted({path for pattern in ("test_*.py", "*_test.py")
                      for path in (root / "tests").rglob(pattern) if path.is_file()})
    excluded = [path.relative_to(root).as_posix() for path in modules
                if masking.protected_path(path)]
    allowed = [path.relative_to(root).as_posix() for path in modules
               if not masking.protected_path(path)]
    reasons = {path: masking.test_exclusion_reason(path) for path in excluded}
    dependent = [path for path in excluded if reasons[path]["kind"] == "dependent_legacy"]
    direct = [path for path in excluded if reasons[path]["kind"] == "direct_holdout"]
    return {
        "candidate_module_count": len(modules),
        "development_collectable_module_count": len(allowed),
        "excluded_module_count": len(excluded),
        "excluded_module_files": excluded,
        "excluded_module_reasons": reasons,
        "direct_holdout_module_count": len(direct),
        "direct_holdout_module_files": direct,
        "dependent_legacy_module_count": len(dependent),
        "dependent_legacy_module_files": dependent,
        "development_collectable_module_files": allowed,
        "excluded_test_case_count": None,
        "excluded_case_count_reason": "Protected module contents are not read during development.",
        "protected_exact_test_names": sorted(name for name in masking.DENIED_NAMES
                                             if name.startswith("test_")),
        "protected_exact_source_names": sorted(name for name in masking.DENIED_NAMES
                                               if name.endswith(".py") and not name.startswith("test_")),
        "protected_test_prefixes": list(masking.DENIED_TEST_PREFIXES),
        "dependent_legacy_exclusion_rules": dict(masking.DEPENDENT_LEGACY_TESTS),
    }


def offline_environment(environ=None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    for name in list(env):
        if (name.startswith("OBS_")
                or name.startswith("PG") or name.endswith("_API_KEY")
                or name in {"OPENAI_BASE_URL", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH"}):
            env.pop(name)
    env.update(PYTHON_DOTENV_DISABLED="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    return env


def check_commands(report: Path, scope: dict, root: Path = ROOT) -> list[tuple[str, list[str]]]:
    ignored = scope["excluded_module_files"]
    pytest = [sys.executable, str(root / "scripts/run_masked_checks.py"),
              "--report-dir", str(report / "pytest"), "--", "tests", "-q", "-m", MARKERS,
              "-p", "scripts.run_project_checks", "-o", "faulthandler_timeout=60",
              *("--ignore=" + path for path in ignored)]
    ruff = [sys.executable, "-m", "ruff", "check", "src", "tests",
            "scripts/run_project_checks.py", "scripts/run_masked_checks.py", "--force-exclude"]
    if ignored:
        ruff.extend(["--extend-exclude", ",".join(ignored)])
    commands = [("pytest", pytest), ("ruff", ruff)]
    commands.extend(("javascript_" + path.stem, ["node", "--check", str(path)])
                    for path in sorted((root / "src/observatory/assets").rglob("*.js")))
    return commands


def run_step(name: str, command: list[str], report: Path, env: dict[str, str]) -> dict:
    print(f"[{name}] starting", flush=True)
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, errors="replace", check=False)
        exit_code, output = completed.returncode, completed.stdout
    except OSError as exc:
        exit_code, output = 127, f"{type(exc).__name__}: {exc}\n"
    (report / (name + ".log")).write_text(output, encoding="utf-8")
    print(output, end="" if output.endswith("\n") else "\n", flush=True)
    return {"name": name, "command": command, "exit_code": exit_code,
            "elapsed_seconds": round(time.monotonic() - started, 3), "log": name + ".log"}


def junit_counts(path: Path) -> dict[str, int]:
    suites = ET.parse(path).getroot().iter("testsuite")
    totals = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
    for suite in suites:
        for name in totals:
            totals[name] += int(suite.get(name, "0"))
    totals["passed"] = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    return totals


def verify_pytest_reports(report: Path, pytest_exit_code: int) -> tuple[dict, list[str]]:
    details, errors = {}, []
    for name in ("mask_receipt", "collection", "offline_guard"):
        try:
            details[name] = json.loads((report / "pytest" / (name + ".json")).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"{name} report missing or invalid: {type(exc).__name__}")
    mask = details.get("mask_receipt", {})
    if mask.get("customer_questions_loaded") is not False or mask.get("denied_events") != []:
        errors.append("mask receipt does not confirm zero denied development reads")
    if mask.get("exit_code") != pytest_exit_code:
        errors.append("mask receipt exit code differs from the pytest process")
    if details.get("offline_guard", {}).get("denied_operations") != []:
        errors.append("offline guard denied a network or database operation")
    try:
        details["junit_counts"] = junit_counts(report / "pytest/junit.xml")
    except (OSError, ValueError, ET.ParseError) as exc:
        errors.append(f"JUnit report missing or invalid: {type(exc).__name__}")
    return details, errors


def _stdlib_socketpair(*args, **kwargs):
    token = _SOCKETPAIR_CONTEXT.set(True)
    try:
        return _ORIGINAL_SOCKETPAIR(*args, **kwargs)
    finally:
        _SOCKETPAIR_CONTEXT.reset(token)


def internal_socketpair_connect(values) -> bool:
    if not _SOCKETPAIR_CONTEXT.get() or len(values) != 2:
        return False
    connection, address = values
    if (not isinstance(connection, _SOCKET_TYPE) or connection.type != socket.SOCK_STREAM
            or connection.proto != 0 or not isinstance(address, tuple) or len(address) != 2
            or type(address[1]) is not int or not 0 < address[1] < 65536):
        return False
    expected = {socket.AF_INET: "127.0.0.1", socket.AF_INET6: "::1"}
    return connection.family in expected and address[0] == expected[connection.family]


def guard_network_operation(event, values, denied) -> bool:
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        if event == "socket.connect" and internal_socketpair_connect(values):
            return True
        denied.append({"kind": "network", "event": event})
        raise PermissionError("The offline development gate refuses real Python network operations.")
    return False


def _deny_python_network(event, values) -> None:
    global _SOCKETPAIR_CONNECTS
    if guard_network_operation(event, values, _DENIED_OPERATIONS):
        _SOCKETPAIR_CONNECTS += 1


def _deny_database(*_args, **_kwargs):
    _DENIED_OPERATIONS.append({"kind": "database", "event": "psycopg.connect"})
    raise PermissionError("The offline development gate refuses real PostgreSQL connections.")


def pytest_configure(config):
    """Activated only by the gate's explicit ``-p`` option, before module collection."""
    import psycopg

    socket.socketpair = _stdlib_socketpair
    sys.addaudithook(_deny_python_network)
    # Tests may replace these functions with their own fake connection objects.
    # libpq uses native sockets, so the Python socket audit hook alone is insufficient.
    psycopg.connect = _deny_database
    psycopg.Connection.connect = _deny_database
    psycopg.AsyncConnection.connect = _deny_database


def pytest_itemcollected(item):
    global _COLLECTED
    _COLLECTED += 1
    _COLLECTED_MODULES.add(item.path.relative_to(ROOT).as_posix())


def pytest_deselected(items):
    global _DESELECTED
    _DESELECTED += len(items)


def pytest_collection_finish(session):
    report = os.environ.get("OBS_PROJECT_CHECKS_REPORT_DIR")
    if report:
        write_json(Path(report) / "collection.json", {
            "collected_test_cases": _COLLECTED,
            "marker_deselected_test_cases": _DESELECTED,
            "selected_test_cases": len(session.items),
            "collected_module_count": len(_COLLECTED_MODULES),
            "collected_module_files": sorted(_COLLECTED_MODULES),
            "selected_module_files": sorted({item.path.relative_to(ROOT).as_posix() for item in session.items}),
        })


def pytest_runtest_logstart(nodeid, location):
    global _CURRENT_NODE_ID
    _CURRENT_NODE_ID = nodeid
    write_progress("running")


def pytest_runtest_logfinish(nodeid, location):
    global _COMPLETED
    _COMPLETED += 1
    if _COMPLETED % 10 == 0:
        write_progress("between_tests")


def pytest_sessionfinish(session, exitstatus):
    report = os.environ.get("OBS_PROJECT_CHECKS_REPORT_DIR")
    if report:
        write_progress("finished")
        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        stats = terminal.stats if terminal is not None else {}
        write_json(Path(report) / "offline_guard.json", {
            "denied_operations": _DENIED_OPERATIONS,
            "scope": "Python sockets and ordinary psycopg connect APIs; no OS sandbox",
            "stdlib_socketpair_connect_count": _SOCKETPAIR_CONNECTS,
            "progress_replace_sharing_failure_count": _PROGRESS_REPLACE_SHARING_FAILURES,
            "outcome_counts": {name: len(stats.get(name, [])) for name in
                               ("passed", "failed", "error", "skipped", "xfailed", "xpassed")},
        })


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, required=True,
                        help="New unprotected directory inside this checkout; existing receipts are not overwritten.")
    args = parser.parse_args(argv)
    report = args.report_dir.resolve()
    if ROOT not in report.parents or masking.protected_path(report):
        parser.error("Use a new unprotected report directory inside this checkout.")
    report.mkdir(parents=True, exist_ok=False)
    scope = test_inventory()
    scope.update(marker_expression=MARKERS, purpose="development",
                 scope="All development-collectable tests; protected history and live/integration cases excluded.",
                 limitations=["Excluded core-module cases are not validated by this gate.",
                              "Engineering checks do not establish model accuracy or customer acceptance.",
                              "Python guards are workflow controls, not an operating-system sandbox."])
    write_json(report / "scope.json", scope)
    print(f"Development scope: {scope['development_collectable_module_count']} candidate modules; "
          f"{scope['excluded_module_count']} protected modules excluded (see scope.json).", flush=True)
    env = offline_environment()
    env["OBS_PROJECT_CHECKS_REPORT_DIR"] = str(report / "pytest")
    steps = [run_step(name, command, report, env) for name, command in check_commands(report, scope)]
    details, errors = verify_pytest_reports(report, steps[0]["exit_code"])
    exit_code = int(any(step["exit_code"] != 0 for step in steps) or bool(errors))
    summary = {"at_utc": datetime.now(timezone.utc).isoformat(), "exit_code": exit_code,
               "steps": steps, "pytest": details, "receipt_errors": errors,
               "excluded_module_count": scope["excluded_module_count"],
               "direct_holdout_module_count": scope["direct_holdout_module_count"],
               "dependent_legacy_module_count": scope["dependent_legacy_module_count"],
               "scope_report": "scope.json", "customer_accuracy": None, "human_accuracy": None}
    write_json(report / "summary.json", summary)
    print(f"Gate {'passed' if exit_code == 0 else 'failed'}; receipts: {report}", flush=True)
    for error in errors:
        print(f"Receipt error: {error}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
