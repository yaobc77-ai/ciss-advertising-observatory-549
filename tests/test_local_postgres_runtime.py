"""Major upgrades must fail before opening incompatible or legacy cluster data."""

import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def pg_runtime(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import local_postgres as pg

    runtime = tmp_path / ".runtime"
    binary = runtime / "postgres18" / "Library" / "bin"
    binary.mkdir(parents=True)
    (binary / "postgres.exe").touch()
    monkeypatch.setattr(pg, "BIN", binary)
    monkeypatch.setattr(pg, "DATA", runtime / "pgdata18")
    monkeypatch.setattr(pg, "LEGACY_DATA", runtime / "pgdata")
    monkeypatch.setattr(pg, "MARKER", runtime / "postgres-cluster.json")
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout="postgres (PostgreSQL) 18.6\n", returncode=0)

    monkeypatch.setattr(pg, "run", command)
    return pg, calls


def write_cluster(pg, major="18", *, marker_data=None):
    pg.DATA.mkdir()
    (pg.DATA / "PG_VERSION").write_text(major + "\n", encoding="ascii")
    pg.MARKER.write_text(json.dumps({
        "data_directory": str(marker_data or pg.DATA), "port": pg.PORT,
    }), encoding="utf-8")


@pytest.mark.parametrize("version", ["postgres (PostgreSQL) 16.15", "postgres (PostgreSQL) 19.0", "unrecognized version"])
def test_non_pg18_binary_is_rejected_before_cluster_control(pg_runtime, monkeypatch, version):
    pg, calls = pg_runtime
    write_cluster(pg)

    def command(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=version, returncode=0)

    monkeypatch.setattr(pg, "run", command)
    with pytest.raises(RuntimeError, match="requires PostgreSQL 18"):
        pg.start()
    assert calls and all(Path(call[0]).name == "postgres.exe" for call in calls)


def test_old_data_cannot_be_opened_by_pg18(pg_runtime):
    pg, calls = pg_runtime
    write_cluster(pg, "16")
    with pytest.raises(RuntimeError, match="major versions differ"):
        pg.start()
    assert all(Path(call[0]).name == "postgres.exe" for call in calls)
    assert (pg.DATA / "PG_VERSION").read_text() == "16\n"


def test_legacy_cluster_never_becomes_a_second_empty_main_database(pg_runtime, monkeypatch):
    pg, calls = pg_runtime
    pg.LEGACY_DATA.mkdir()
    (pg.LEGACY_DATA / "PG_VERSION").write_text("16\n", encoding="ascii")
    monkeypatch.setattr(pg, "private_settings", lambda **kwargs: pytest.fail("Credentials were accessed"))
    with pytest.raises(RuntimeError, match="requires the reviewed PostgreSQL 18 migration"):
        pg.initialize()
    assert not pg.DATA.exists()
    assert all(Path(call[0]).name == "postgres.exe" for call in calls)


def test_marker_for_old_cluster_blocks_pg18_control(pg_runtime):
    pg, calls = pg_runtime
    write_cluster(pg, marker_data=pg.LEGACY_DATA)
    with pytest.raises(RuntimeError, match="ownership marker"):
        pg.is_running()
    assert calls == []


def test_matching_pg18_marker_and_data_allow_status_only(pg_runtime):
    pg, calls = pg_runtime
    write_cluster(pg)
    assert pg.is_running()
    assert [Path(call[0]).name for call in calls] == ["postgres.exe", "pg_ctl.exe"]
    assert calls[-1][1:] == ["status", "-D", str(pg.DATA)]


def test_missing_runtime_does_not_attempt_initialization(pg_runtime):
    pg, calls = pg_runtime
    (pg.BIN / "postgres.exe").unlink()
    with pytest.raises(RuntimeError, match="runtime is missing"):
        pg.initialize()
    assert not pg.DATA.exists()
    assert calls == []


@pytest.mark.parametrize("legacy_marker", [False, True])
def test_setup_preflight_resolves_project_junction_without_migrating(tmp_path, legacy_marker, monkeypatch):
    actual = tmp_path / "project real"
    actual.mkdir()
    alias = tmp_path / "project alias"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(actual), str(alias))
    else:
        alias.symlink_to(actual, target_is_directory=True)
    runtime = actual / ".runtime"
    data = runtime / ("pgdata" if legacy_marker else "pgdata18")
    data.mkdir(parents=True)
    (data / "PG_VERSION").write_text("16\n" if legacy_marker else "18\n", encoding="ascii")
    marker = runtime / "postgres-cluster.json"
    contents = json.dumps({"data_directory": str(data.resolve()), "port": 55432})
    marker.write_text(contents, encoding="utf-8")
    setup = (Path(__file__).resolve().parents[1] / "scripts" / "Setup-Postgres.ps1").read_text(encoding="utf-8")
    preflight = re.search(r"@'\n(.*?)\n'@ \| & \$taskPython -X utf8 - \$taskRuntime", setup, re.DOTALL)
    assert preflight is not None
    monkeypatch.setattr(sys, "argv", ["preflight", str(alias / ".runtime")])
    code = compile(preflight[1], "Setup-Postgres.ps1:preflight", "exec")
    if legacy_marker:
        with pytest.raises(SystemExit, match="requires the reviewed PostgreSQL 18 migration"):
            exec(code, {})
        assert not (runtime / "pgdata18").exists()
    else:
        exec(code, {})
    assert marker.read_text(encoding="utf-8") == contents
