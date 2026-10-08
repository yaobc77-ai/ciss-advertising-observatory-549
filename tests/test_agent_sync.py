"""Shared agent log: two agents see each other's updates, claims and cursors."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "agent_sync.py"
SPEC = importlib.util.spec_from_file_location("agent_sync", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.setenv("OBS_COORD_DIR", str(tmp_path / "coord"))

    def fixture_git(*args):
        if args == ("rev-parse", "--show-toplevel"):
            return str(tmp_path)
        if args == ("branch", "--show-current"):
            return "synthetic-fixture-branch"
        raise AssertionError("Unexpected Git command in coordination-only fixture")

    monkeypatch.setattr(sync, "git", fixture_git)
    for key in ("AGENT_NAME", "CLAUDECODE"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path / "coord"


def run(agent, *args):
    return sync.main(["--agent", agent, *args])


def test_each_agent_reads_only_the_other_agents_new_updates(shared, capsys):
    run("codex", "post", "result", "multistep host frozen", "--quiet")
    run("claude", "post", "result", "U19 fixed", "--evidence", "outputs/run.json", "--quiet")
    assert [e["summary"] for e in sync.unread(shared, "claude")] == ["multistep host frozen"]
    assert [e["summary"] for e in sync.unread(shared, "codex")] == ["multistep host frozen", "U19 fixed"][1:]
    run("claude", "read")
    assert sync.unread(shared, "claude") == []
    run("codex", "post", "paid_run", "29-case batch", "--cost", "0.068", "--quiet")
    assert [e["summary"] for e in sync.unread(shared, "claude")] == ["29-case batch"]
    assert "29-case batch" in (shared / "STATUS.md").read_text(encoding="utf-8")


def test_claim_by_another_agent_blocks_overlapping_paths(shared, capsys):
    assert run("codex", "claim", "src/observatory/research_*.py", "--reason", "multistep") == 0
    assert run("claude", "claim", "src/observatory/research_tools.py") == 2
    assert "codex" in capsys.readouterr().out
    assert run("claude", "claim", "src/observatory/language.py") == 0
    assert run("codex", "release", "src/observatory/research_*.py") == 0
    assert run("claude", "claim", "src/observatory/research_tools.py") == 0


def test_open_decisions_stay_listed_until_decided(shared):
    run("claude", "post", "decision_needed", "Keep model-chosen share denominator?", "--quiet")
    asked = sync.events(shared)[-1]["id"]
    assert "Keep model-chosen" in (shared / "STATUS.md").read_text(encoding="utf-8").split("## Active claims")[0]
    run("human", "post", "decision", "Approved", "--refs", asked, "--quiet")
    assert "Keep model-chosen" not in (shared / "STATUS.md").read_text(encoding="utf-8").split("## Active claims")[0]


def test_a_torn_line_does_not_hide_later_events(shared):
    run("codex", "post", "note", "first", "--quiet")
    with (shared / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{"broken": \n')
    run("codex", "post", "note", "second", "--quiet")
    assert [e["summary"] for e in sync.events(shared)] == ["first", "second"]


def test_agent_detection_order(monkeypatch):
    for key in list(__import__("os").environ):
        if key.upper().startswith("CODEX") or key in ("AGENT_NAME", "CLAUDECODE"):
            monkeypatch.delenv(key, raising=False)
    assert sync.current_agent() == "human"
    monkeypatch.setenv("CODEX_HOME", "x")
    assert sync.current_agent() == "codex"
    monkeypatch.setenv("CLAUDECODE", "1")
    assert sync.current_agent() == "claude"
    monkeypatch.setenv("AGENT_NAME", "Codex")
    assert sync.current_agent() == "codex"


def test_events_carry_no_empty_fields(shared):
    run("claude", "post", "note", "plain", "--quiet")
    event = json.loads((shared / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert {"id", "ts", "agent", "kind", "summary"} <= set(event)
    assert all(value not in (None, [], "") for value in event.values())


def test_read_never_marks_events_appended_after_the_snapshot(shared, monkeypatch, capsys):
    run("codex", "post", "note", "seen", "--quiet")
    original = sync.unread

    def unread_then_append(directory, agent):
        result = original(directory, agent)
        sync.append(directory, {"agent": "codex", "kind": "note", "summary": "arrived during read"})
        return result

    monkeypatch.setattr(sync, "unread", unread_then_append)
    run("claude", "read")
    monkeypatch.setattr(sync, "unread", original)
    assert [e["summary"] for e in sync.unread(shared, "claude")] == ["arrived during read"]


def test_a_stale_holder_does_not_remove_a_newer_lock(shared):
    shared.mkdir(parents=True, exist_ok=True)
    with sync.locked(shared):
        lock = shared / ".lock"
        lock.write_text("newer-holder", encoding="utf-8")  # taken over after a timeout
    assert lock.read_text(encoding="utf-8") == "newer-holder"
