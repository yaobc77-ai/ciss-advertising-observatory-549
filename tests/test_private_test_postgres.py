"""Fresh synthetic ownership/port checks; no customer cases or live subprocesses."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import local_postgres as main_pg
from scripts import private_test_postgres as pg


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "ROOT", tmp_path)
    monkeypatch.setattr(pg, "BIN", tmp_path / "binaries")
    monkeypatch.delenv("OBS_DATABASE_URL", raising=False)
    result = pg.PrivateTestCluster("synthetic_probe")
    monkeypatch.setattr(result, "verify_runtime", lambda: None)
    return result


def marked(cluster, port=57019):
    cluster.data.mkdir(parents=True)
    (cluster.data / "PG_VERSION").write_text("18\n", encoding="ascii")
    state = {"kind": pg.MARKER_KIND, "workspace": str(cluster.workspace),
             "data_directory": str(cluster.data), "host": pg.HOST,
             "database": pg.DATABASE, "port": port}
    cluster.marker.write_text(json.dumps(state), encoding="utf-8")
    return state


def credentials(cluster):
    cluster.directory.mkdir(parents=True, exist_ok=True)
    cluster.credentials.write_text(json.dumps({
        "admin_user": pg.ADMIN_USER, "admin_password": "synthetic_admin",
        "app_user": pg.APP_USER, "app_password": "synthetic password"}), encoding="utf-8")


@pytest.mark.parametrize("name", ["../main", "a/b", "a\\b", "D:\\data", "", ".", "MAIN"])
def test_names_cannot_select_arbitrary_directory(name):
    with pytest.raises(ValueError):
        pg.PrivateTestCluster(name)


@pytest.mark.parametrize("port", [5432, 55432, 0, 65536, "57019", True])
def test_marker_rejects_main_default_and_invalid_ports(cluster, port):
    marked(cluster, port)
    with pytest.raises(RuntimeError, match="ownership/port"):
        cluster.state()


@pytest.mark.parametrize("field,value", [
    ("kind", "main"), ("workspace", "D:\\elsewhere"),
    ("data_directory", "D:\\main"), ("host", "0.0.0.0"),
    ("database", "observatory"),
])
def test_marker_rejects_wrong_owner_or_target(cluster, field, value):
    state = marked(cluster)
    state[field] = value
    cluster.marker.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(RuntimeError, match="ownership/port"):
        cluster.state()


def test_explicit_main_port_is_also_reserved(cluster, monkeypatch):
    marked(cluster)
    monkeypatch.setenv("OBS_DATABASE_URL", "postgresql://127.0.0.1:57019/observatory")
    with pytest.raises(RuntimeError, match="ownership/port"):
        cluster.state()


def test_main_url_parse_error_does_not_print_connection(monkeypatch):
    monkeypatch.setenv("OBS_DATABASE_URL", "not-a-dsn synthetic-secret")
    with pytest.raises(RuntimeError) as error:
        pg.prohibited_ports()
    assert "synthetic-secret" not in str(error.value)


def test_free_port_selection_skips_known_and_explicit_main_ports(monkeypatch):
    monkeypatch.setenv("OBS_DATABASE_URL", "postgresql://127.0.0.1:57019/observatory")
    ports = iter([55432, 5432, 57019, 57311])

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def bind(self, address):
            assert address == ("127.0.0.1", 0)

        def getsockname(self):
            return ("127.0.0.1", next(ports))

    monkeypatch.setattr(pg.socket, "socket", lambda *args: Probe())
    assert pg.choose_port() == 57311


def test_canonical_path_guard_refuses_junction_escape(cluster, monkeypatch):
    original = Path.resolve

    def resolved(path, *args, **kwargs):
        if path == cluster.directory:
            return cluster.workspace / ".runtime" / "pgdata18"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolved)
    with pytest.raises(RuntimeError, match="links are refused"):
        cluster.assert_paths()


def test_nonempty_unmarked_directory_is_preserved(cluster, monkeypatch):
    cluster.directory.mkdir(parents=True)
    keep = cluster.directory / "retain.txt"
    keep.write_text("synthetic evidence", encoding="utf-8")
    calls = []
    monkeypatch.setattr(pg, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RuntimeError, match="nonempty"):
        cluster.initialize()
    assert keep.read_text(encoding="utf-8") == "synthetic evidence"
    assert calls == []


def test_data_major_mismatch_is_preserved(cluster):
    marked(cluster)
    (cluster.data / "PG_VERSION").write_text("16", encoding="ascii")
    with pytest.raises(RuntimeError, match="major version"):
        cluster.state()


def test_private_url_is_separate_and_not_printed(cluster, monkeypatch, capsys):
    marked(cluster)
    credentials(cluster)
    project_env = cluster.workspace / ".env"
    project_env.write_text("OBS_DATABASE_URL=synthetic-main\n", encoding="utf-8")
    monkeypatch.setattr(pg, "restrict_acl", lambda *args, **kwargs: None)
    cluster.write_env()
    assert project_env.read_text(encoding="utf-8") == "OBS_DATABASE_URL=synthetic-main\n"
    content = cluster.env_file.read_text(encoding="utf-8")
    assert "@127.0.0.1:57019/obs_test" in content
    assert "synthetic%20password" in content
    assert capsys.readouterr().out == ""


def test_connection_uses_private_state_not_inherited_test_url(cluster, monkeypatch):
    marked(cluster)
    credentials(cluster)
    monkeypatch.setenv("OBS_TEST_DATABASE_URL", "postgresql://127.0.0.1:55432/observatory")
    calls = []
    monkeypatch.setattr(pg.psycopg, "connect", lambda **kwargs: calls.append(kwargs))
    cluster.connect()
    assert calls[0]["port"] == 57019
    assert calls[0]["hostaddr"] == "127.0.0.1"
    assert calls[0]["dbname"] == "obs_test"
    with pytest.raises(ValueError):
        cluster.connect("observatory", admin=True)


def process_fixture(cluster, monkeypatch, *, data_path=None, executable=None):
    marked(cluster)
    (cluster.data / "postmaster.pid").write_text(
        f"12345\n{cluster.data}\n100\n57019\n", encoding="utf-8")
    monkeypatch.setattr(pg, "process_info", lambda pid: {
        "ProcessId": pid, "ExecutablePath": str(executable or pg.BIN / "postgres.exe"),
        "CommandLine": f'"{pg.BIN / "postgres.exe"}" -D "{data_path or cluster.data}"',
    })


def test_process_must_identify_own_data_directory(cluster, monkeypatch):
    process_fixture(cluster, monkeypatch, data_path=cluster.workspace / ".runtime" / "pgdata18")
    with pytest.raises(RuntimeError, match="does not own"):
        cluster.verify_process()


def test_process_must_use_expected_executable(cluster, monkeypatch):
    process_fixture(cluster, monkeypatch, executable=cluster.workspace / "another.exe")
    with pytest.raises(RuntimeError, match="unexpected executable"):
        cluster.verify_process()


def test_private_process_command_line_handles_spaces(cluster, monkeypatch):
    process_fixture(cluster, monkeypatch)
    assert cluster.verify_process() == 12345


def test_wrong_pidfile_port_is_rejected_before_process_lookup(cluster, monkeypatch):
    process_fixture(cluster, monkeypatch)
    (cluster.data / "postmaster.pid").write_text(
        f"12345\n{cluster.data}\n100\n55432\n", encoding="utf-8")
    monkeypatch.setattr(pg, "process_info", lambda pid: pytest.fail("process lookup must not run"))
    with pytest.raises(RuntimeError, match="pidfile identity"):
        cluster.verify_process()


def test_stop_identity_failure_never_calls_pg_ctl_stop(cluster, monkeypatch):
    monkeypatch.setattr(cluster, "running", lambda: True)
    monkeypatch.setattr(cluster, "verify_server", lambda: (_ for _ in ()).throw(RuntimeError("wrong server")))
    calls = []
    monkeypatch.setattr(pg, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RuntimeError, match="wrong server"):
        cluster.stop()
    assert calls == []


def test_stop_targets_only_owned_data_and_retains_it(cluster, monkeypatch):
    marked(cluster)
    running = iter([True, False])
    monkeypatch.setattr(cluster, "running", lambda: next(running))
    monkeypatch.setattr(cluster, "verify_server", lambda: 12345)
    monkeypatch.setattr(cluster, "verify_process", lambda: 12345)
    calls = []
    monkeypatch.setattr(pg, "run", lambda command, **kwargs: calls.append(command))
    result = cluster.stop()
    assert len(calls) == 1
    assert calls[0][1:5] == ["stop", "-D", str(cluster.data), "-m"]
    assert cluster.data.is_dir()
    assert result["data_retained"] is True
    assert result["main_cluster_operated"] is False


def test_stop_rejects_pid_change_before_control_command(cluster, monkeypatch):
    monkeypatch.setattr(cluster, "running", lambda: True)
    monkeypatch.setattr(cluster, "verify_server", lambda: 12345)
    monkeypatch.setattr(cluster, "verify_process", lambda: 54321)
    monkeypatch.setattr(pg, "run", lambda *args, **kwargs: pytest.fail("must not stop"))
    with pytest.raises(RuntimeError, match="identity changed"):
        cluster.stop()


def test_occupied_port_does_not_stop_any_process(cluster, monkeypatch):
    marked(cluster)
    monkeypatch.setattr(cluster, "initialize", lambda: None)
    monkeypatch.setattr(cluster, "running", lambda: False)

    class Occupied:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def connect_ex(self, address):
            assert address == ("127.0.0.1", 57019)
            return 0

    monkeypatch.setattr(pg.socket, "socket", lambda *args: Occupied())
    monkeypatch.setattr(pg, "run", lambda *args, **kwargs: pytest.fail("must not start/stop"))
    with pytest.raises(RuntimeError, match="port is occupied"):
        cluster.setup()


def test_legacy_setup_delegates_without_main_cluster_functions(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(pg.PrivateTestCluster, "setup", lambda self: calls.append(self.name) or {"private": True})
    for name in ("verify_server", "connection", "update_database_env", "private_settings"):
        monkeypatch.setattr(main_pg, name, lambda *args, **kwargs: pytest.fail("main cluster function called"))
    main_pg.setup_test_db()
    assert calls == ["default"]
    assert json.loads(capsys.readouterr().out) == {"private": True}


def test_status_of_new_cluster_creates_nothing(cluster):
    assert cluster.status()["status"] == "not_initialized"
    assert not cluster.directory.exists()


def test_cli_alias_start_uses_setup(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(pg.PrivateTestCluster, "setup", lambda self: calls.append(self.name) or {"running": True})
    assert pg.main(["start", "--cluster", "fresh_probe"]) == 0
    assert calls == ["fresh_probe"]
    assert json.loads(capsys.readouterr().out)["running"] is True


def test_bad_runtime_version_is_rejected(cluster, monkeypatch):
    for name in ("postgres.exe", "pg_ctl.exe", "initdb.exe"):
        (pg.BIN / name).parent.mkdir(parents=True, exist_ok=True)
        (pg.BIN / name).touch()
    monkeypatch.setattr(pg, "run", lambda *args, **kwargs: SimpleNamespace(stdout="postgres (PostgreSQL) 16.15"))
    with pytest.raises(RuntimeError, match="require PostgreSQL 18"):
        pg.PrivateTestCluster.verify_runtime(cluster)
