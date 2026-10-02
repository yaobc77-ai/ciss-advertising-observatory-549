"""Shared progress log between the Claude Code and Codex agents.

Both agents read and write one append-only log, so every result one of them
produces is visible to the other at its next step:

  python scripts/agent_sync.py status            # unread updates, claims, git state
  python scripts/agent_sync.py read              # show unread updates and mark them read
  python scripts/agent_sync.py post result "U19 fixed: 22 undated ads" --files src/x.py --commit HEAD
  python scripts/agent_sync.py claim src/observatory/research_tools.py --reason "share denominator"
  python scripts/agent_sync.py release src/observatory/research_tools.py

The log lives outside version control in one fixed directory (default: the main
project's .coordination/, override with OBS_COORD_DIR), so copies and clones of
the repository share it and appends never cause merge conflicts. Commits and
pushes are posted automatically by .githooks. See docs/AGENT_PROTOCOL.md.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAIN_PROJECT = Path("D:/Projects/549 native ads")
KINDS = ("start", "progress", "result", "commit", "push", "deploy", "paid_run", "data_change",
         "decision_needed", "decision", "handoff", "blocked", "claim", "release", "note")
AGENTS = ("claude", "codex", "human")
STALE_LOCK_SECONDS = 60


def coord_dir() -> Path:
    override = os.environ.get("OBS_COORD_DIR")
    if override:
        return Path(override)
    if MAIN_PROJECT.exists():
        return MAIN_PROJECT / ".coordination"
    return Path(__file__).resolve().parents[1] / ".coordination"


def current_agent(explicit: str | None = None) -> str:
    """AGENT_NAME wins; otherwise detect the running tool from its environment."""
    name = (explicit or os.environ.get("AGENT_NAME") or "").strip().lower()
    if name:
        return name
    if os.environ.get("CLAUDECODE"):
        return "claude"
    if any(key.upper().startswith("CODEX") for key in os.environ):
        return "codex"
    return "human"


@contextmanager
def locked(directory: Path):
    """Cross-process lock for appends and claim edits (works on Windows)."""
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".lock"
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    deadline = time.monotonic() + 15
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, token.encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise SystemExit(f"Coordination lock busy: {lock}")
            time.sleep(0.1)
    try:
        yield
    finally:
        # A holder that outlived the stale timeout must not remove a newer lock.
        try:
            if lock.read_text(encoding="utf-8") == token:
                lock.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


def git(*args: str, cwd: Path | None = None) -> str:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=20).stdout.rstrip()
    except (OSError, subprocess.SubprocessError):
        return ""


def repo_root() -> Path:
    root = git("rev-parse", "--show-toplevel")
    return Path(root) if root else Path.cwd()


def events(directory: Path) -> list[dict]:
    path = directory / "events.jsonl"
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # A torn line never hides later entries.
    return result


def append(directory: Path, event: dict) -> dict:
    event = {"id": uuid.uuid4().hex[:12], "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             **event}
    with locked(directory):
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        render_status(directory)
    return event


def load_claims(directory: Path) -> list[dict]:
    path = directory / "claims.json"
    if not path.exists():
        return []
    now = datetime.now(timezone.utc)
    claims = json.loads(path.read_text(encoding="utf-8"))
    return [c for c in claims if datetime.fromisoformat(c["expires"]) > now]


def save_claims(directory: Path, claims: list[dict]) -> None:
    tmp = directory / "claims.json.tmp"
    tmp.write_text(json.dumps(claims, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, directory / "claims.json")


def cursor_path(directory: Path, agent: str) -> Path:
    return directory / "cursors" / f"{agent}.json"


def unread(directory: Path, agent: str) -> list[dict]:
    path = cursor_path(directory, agent)
    last = json.loads(path.read_text(encoding="utf-8")).get("last_id") if path.exists() else None
    all_events = events(directory)
    if last:
        ids = [e.get("id") for e in all_events]
        all_events = all_events[ids.index(last) + 1:] if last in ids else all_events
    return [e for e in all_events if e.get("agent") != agent]


def mark_read(directory: Path, agent: str, upto: list[dict] | None = None) -> None:
    """Advance the cursor only to the last event that was actually shown."""
    all_events = upto if upto is not None else events(directory)
    if all_events:
        path = cursor_path(directory, agent)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_id": all_events[-1]["id"],
                                    "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}),
                        encoding="utf-8")


def format_event(e: dict) -> str:
    extra = []
    if e.get("commit"):
        extra.append(f"commit {e['commit'][:9]}")
    if e.get("repo"):
        extra.append(e["repo"])
    if e.get("cost_usd") is not None:
        extra.append(f"${e['cost_usd']}")
    if e.get("files"):
        extra.append(f"{len(e['files'])} files")
    tail = f"  [{', '.join(extra)}]" if extra else ""
    line = f"{e['ts'][:16].replace('T', ' ')}  {e.get('agent', '?'):6s} {e.get('kind', '?'):15s} {e.get('summary', '')}{tail}"
    for key in ("needs", "evidence"):
        for item in e.get(key) or []:
            line += f"\n{'':40s}{key}: {item}"
    return line


def render_status(directory: Path) -> None:
    """Human-readable STATUS.md regenerated after every write."""
    all_events = events(directory)
    lines = ["# Agent coordination status", "",
             f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}. "
             "Source of truth: events.jsonl. Do not edit by hand.", ""]
    open_decisions = []
    decided = {e.get("refs") and e["refs"][0] for e in all_events if e.get("kind") == "decision"}
    for e in all_events:
        if e.get("kind") == "decision_needed" and e["id"] not in decided:
            open_decisions.append(e)
    lines += ["## Open decisions for the user", ""]
    lines += [f"- `{e['id']}` ({e['agent']}): {e['summary']}" for e in open_decisions] or ["- None"]
    lines += ["", "## Active claims", ""]
    claims = load_claims(directory)
    lines += [f"- {c['agent']}: `{c['pattern']}` until {c['expires'][:16]} - {c.get('reason', '')}" for c in claims] or ["- None"]
    lines += ["", "## Latest per agent", ""]
    for agent in sorted({e.get("agent") for e in all_events}):
        last = [e for e in all_events if e.get("agent") == agent][-1]
        lines.append(f"- {agent}: {last['ts'][:16]} {last['kind']} - {last.get('summary', '')}")
    lines += ["", "## Recent events", "", "```"]
    lines += [format_event(e) for e in all_events[-25:]] + ["```", ""]
    (directory / "STATUS.md").write_text("\n".join(lines), encoding="utf-8")


def git_state() -> list[str]:
    root = repo_root()
    lines = [f"repo {root}  branch {git('branch', '--show-current') or '?'}"]
    counts = git("rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if counts:
        behind, ahead = counts.split()
        lines.append(f"upstream: {ahead} ahead, {behind} behind")
        if int(ahead):
            lines += ["  unpushed: " + entry for entry in git("log", "--oneline", "@{upstream}..HEAD").splitlines()]
    dirty = [entry[3:] for entry in git("status", "--porcelain").splitlines() if entry.strip()]
    lines.append(f"uncommitted paths: {len(dirty)}")
    return lines + [f"  {p}" for p in dirty[:20]]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", help="Override agent name (default: detected)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    read = sub.add_parser("read")
    read.add_argument("--all", action="store_true", help="Show the last 30 events, not only unread")
    post = sub.add_parser("post")
    post.add_argument("kind", choices=KINDS)
    post.add_argument("summary")
    post.add_argument("--files", nargs="*", default=[])
    post.add_argument("--commit")
    post.add_argument("--evidence", nargs="*", default=[], help="Report or output paths")
    post.add_argument("--needs", nargs="*", default=[], help="What the other agent or user must do")
    post.add_argument("--cost", type=float, dest="cost_usd")
    post.add_argument("--refs", nargs="*", default=[], help="Related event ids")
    post.add_argument("--quiet", action="store_true")
    claim = sub.add_parser("claim")
    claim.add_argument("pattern", help="Path or glob, relative to the repository root")
    claim.add_argument("--reason", default="")
    claim.add_argument("--hours", type=float, default=4)
    release = sub.add_parser("release")
    release.add_argument("pattern")
    args = parser.parse_args(argv)

    directory = coord_dir()
    directory.mkdir(parents=True, exist_ok=True)
    agent = current_agent(args.agent)

    if args.command == "post":
        commit = args.commit
        if commit:
            commit = git("rev-parse", commit) or commit
        event = {"agent": agent, "kind": args.kind, "summary": args.summary,
                 "repo": str(repo_root()), "branch": git("branch", "--show-current"),
                 "commit": commit, "files": args.files, "evidence": args.evidence,
                 "needs": args.needs, "cost_usd": args.cost_usd, "refs": args.refs}
        event = append(directory, {k: v for k, v in event.items() if v not in (None, [], "")})
        if not args.quiet:
            print(f"posted {event['id']}: {format_event(event)}")
        return 0

    if args.command in ("claim", "release"):
        with locked(directory):
            claims = load_claims(directory)
            if args.command == "claim":
                others = [c for c in claims if c["agent"] != agent and
                          (fnmatch.fnmatch(args.pattern, c["pattern"]) or fnmatch.fnmatch(c["pattern"], args.pattern))]
                if others:
                    print("Already claimed by another agent; coordinate first:")
                    for c in others:
                        print(f"  {c['agent']}: {c['pattern']} until {c['expires'][:16]} - {c.get('reason', '')}")
                    return 2
                expires = datetime.now(timezone.utc) + timedelta(hours=args.hours)
                claims = [c for c in claims if not (c["agent"] == agent and c["pattern"] == args.pattern)]
                claims.append({"agent": agent, "pattern": args.pattern, "reason": args.reason,
                               "expires": expires.isoformat(timespec="seconds")})
            else:
                claims = [c for c in claims if not (c["agent"] == agent and c["pattern"] == args.pattern)]
            save_claims(directory, claims)
        append(directory, {"agent": agent, "kind": args.command, "summary": f"{args.pattern} {getattr(args, 'reason', '')}".strip()})
        print(f"{args.command}ed {args.pattern}")
        return 0

    snapshot = events(directory)
    pending = unread(directory, agent)
    if args.command == "read":
        shown = snapshot[-30:] if args.all else pending
        print("\n".join(format_event(e) for e in shown) or "No unread updates from other agents.")
        mark_read(directory, agent, upto=snapshot)
        return 0

    # status
    print(f"Agent: {agent}   coordination: {directory}")
    print(f"\nUnread from other agents: {len(pending)}")
    for e in pending[-15:]:
        print("  " + format_event(e).replace("\n", "\n  "))
    claims = load_claims(directory)
    print(f"\nActive claims: {len(claims)}")
    for c in claims:
        print(f"  {c['agent']}: {c['pattern']} until {c['expires'][:16]} - {c.get('reason', '')}")
    failed = Path(git("rev-parse", "--absolute-git-dir") or ".git") / "agent-sync-failed.log"
    if failed.exists() and failed.read_text(encoding="utf-8").strip():
        print(f"\nWARNING: hook posts failed; repost these entries, then delete {failed}:")
        print("  " + failed.read_text(encoding="utf-8").strip().replace("\n", "\n  "))
    print("\nGit:")
    for line in git_state():
        print("  " + line)
    print("\nRun 'read' to mark these updates as read.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
