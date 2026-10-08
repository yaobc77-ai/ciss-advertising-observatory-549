"""Offline preparation stays separate from database import and model calls."""

import json
from pathlib import Path

import pytest

from observatory import cli, social_archive


def test_prepare_social_dispatches_before_runtime_configuration(monkeypatch, tmp_path, capsys):
    received = []

    def prepare(source, *, out, report):
        received.append((source, out, report))
        return {"source_records": 2, "publication": "not_imported"}

    def forbidden(*args, **kwargs):
        pytest.fail("Offline preparation must not initialize database settings or connections")

    monkeypatch.setattr(social_archive, "prepare_social_archive", prepare)
    monkeypatch.setattr(cli.Settings, "from_env", forbidden)
    monkeypatch.setattr(cli, "Database", forbidden)
    source, out, report = (tmp_path / name for name in ("source.zip", "posts.jsonl", "audit.json"))
    monkeypatch.setattr("sys.argv", [
        "observatory", "prepare-social", str(source), "--out", str(out), "--report", str(report),
    ])

    cli.main()

    assert received == [(source, out, report)]
    assert all(isinstance(value, Path) for value in received[0])
    assert json.loads(capsys.readouterr().out) == {
        "source_records": 2, "publication": "not_imported",
    }
    assert not out.exists()


def test_prepare_social_requires_separate_audit_output(monkeypatch, tmp_path):
    out = tmp_path / "posts.jsonl"
    monkeypatch.setattr("sys.argv", [
        "observatory", "prepare-social", str(tmp_path / "source.json"), "--out", str(out),
    ])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert not out.exists()
