"""Fresh synthetic checks for an explicit, owned private lifecycle capability."""

import json
from types import SimpleNamespace

import postgres_fixtures as pg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo


@pytest.fixture
def owned(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "ROOT", tmp_path)
    name = "synthetic_private"
    directory = tmp_path / ".runtime/private_test_postgres" / name
    data = directory / "data"
    data.mkdir(parents=True)
    marker = directory / "cluster.json"
    state = {"kind": "observatory-private-test-v1", "workspace": str(tmp_path),
             "data_directory": str(data), "host": "127.0.0.1", "port": 57631,
             "database": "obs_test"}
    marker.write_text(json.dumps(state), encoding="utf-8")
    app = "postgresql://obs_private_app:synthetic_app@127.0.0.1:57631/obs_test"
    admin = "postgresql://obs_private_admin:synthetic_admin@127.0.0.1:57631/obs_test"
    monkeypatch.setenv("OBS_TEST_DATABASE_URL", app)
    monkeypatch.setenv("OBS_TEST_ADMIN_DATABASE_URL", admin)
    monkeypatch.setenv("OBS_TEST_PRIVATE_CLUSTER", name)
    monkeypatch.setenv("OBS_DATABASE_URL", "postgresql://main:unread@127.0.0.1:55432/observatory")
    return SimpleNamespace(app=app, admin=admin, marker=marker, state=state, data=data)


def test_explicit_admin_matches_owned_instance_and_preserves_app_setting(owned, monkeypatch):
    admin = conninfo_to_dict(pg.configured_private_admin_url())
    app = conninfo_to_dict(pg.configured_test_url())
    assert admin["user"] == "obs_private_admin"
    assert app["user"] == "obs_private_app"
    assert (admin["host"], admin["port"], admin["dbname"]) == (app["host"], app["port"], app["dbname"])
    assert app["password"] == "synthetic_app"


def test_missing_admin_fails_instead_of_falling_back_or_skipping(owned, monkeypatch):
    monkeypatch.delenv("OBS_TEST_ADMIN_DATABASE_URL")
    with pytest.raises(ValueError, match="explicit private admin"):
        pg.configured_private_admin_url()


@pytest.mark.parametrize("field,value", [
    ("host", "127.0.0.2"), ("host", "localhost"), ("host", "203.0.113.9"),
    ("hostaddr", "127.0.0.2"), ("port", "57632"), ("port", "5432"), ("port", "55432"),
    ("dbname", "obs_test_other"), ("dbname", "observatory"), ("user", "obs_private_app"),
    ("service", "synthetic_service"), ("replication", "database"),
])
def test_admin_target_cannot_change_instance_database_role_or_indirection(owned, monkeypatch, field, value):
    monkeypatch.setenv("OBS_TEST_ADMIN_DATABASE_URL", make_conninfo(owned.admin, **{field: value}))
    with pytest.raises(ValueError):
        pg.configured_private_admin_url()


def test_default_port_is_refused_even_when_both_roles_match(owned, monkeypatch):
    monkeypatch.setenv("OBS_TEST_DATABASE_URL", make_conninfo(owned.app, port=5432))
    monkeypatch.setenv("OBS_TEST_ADMIN_DATABASE_URL", make_conninfo(owned.admin, port=5432))
    with pytest.raises(ValueError, match="match one private"):
        pg.configured_private_admin_url()


@pytest.mark.parametrize("name", ["", "default", "../main", "main/path", "UPPER"])
def test_cluster_name_cannot_select_main_default_or_arbitrary_path(owned, monkeypatch, name):
    monkeypatch.setenv("OBS_TEST_PRIVATE_CLUSTER", name)
    with pytest.raises(ValueError, match="non-default"):
        pg.configured_private_admin_url()


@pytest.mark.parametrize("field,value", [
    ("kind", "main"), ("workspace", "D:/unrelated"), ("data_directory", "D:/main"),
    ("host", "0.0.0.0"), ("port", 57632), ("port", "57631"), ("database", "observatory"),
])
def test_admin_setting_must_match_ownership_marker(owned, field, value):
    state = dict(owned.state)
    state[field] = value
    owned.marker.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="owned cluster marker"):
        pg.configured_private_admin_url()


@pytest.mark.parametrize("content", ["invalid json", "[]"])
def test_invalid_marker_never_grants_admin_capability(owned, content):
    owned.marker.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        pg.configured_private_admin_url()


@pytest.mark.parametrize("name", [
    "obs_test", "observatory", "postgres", "obs_test_observations_20261006_a",
    "obs_test_observations_20261006_ABCDEFGHIJKL", "obs_test_observations_20261006_0123456789ab;DROP",
    "other_0123456789ab", "obs_test_observations_20261006_0123456789ab/../main",
])
def test_lifecycle_database_identifier_is_exact_and_owned(name):
    with pytest.raises(ValueError, match="owned observation-test"):
        pg.validate_owned_observation_database(name)


def test_exact_fresh_owned_identifier_is_allowed():
    name = "obs_test_observations_20261006_0123456789ab"
    assert pg.validate_owned_observation_database(name) == name


@pytest.mark.parametrize("elevated", ["rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication"])
def test_application_elevation_is_rejected(owned, elevated):
    role = {"name": "obs_private_app", "rolsuper": False, "rolcreatedb": False,
            "rolcreaterole": False, "rolreplication": False}
    role[elevated] = True
    conn = SimpleNamespace(execute=lambda query: SimpleNamespace(fetchone=lambda: role))
    with pytest.raises(ValueError, match="remain restricted"):
        pg.verified_private_application_owner(conn)


def test_ordinary_application_owner_remains_restricted(owned):
    role = {"name": "obs_private_app", "rolsuper": False, "rolcreatedb": False,
            "rolcreaterole": False, "rolreplication": False}
    conn = SimpleNamespace(execute=lambda query: SimpleNamespace(fetchone=lambda: role))
    assert pg.verified_private_application_owner(conn) == "obs_private_app"


@pytest.mark.parametrize("field,value", [("name", "other_admin"), ("data_directory", "D:/main")])
def test_actual_admin_server_identity_mismatch_closes_connection(owned, monkeypatch, field, value):
    actual = {"name": "obs_private_admin", "data_directory": str(owned.data)}
    actual[field] = value
    closed = []
    conn = SimpleNamespace(execute=lambda query: SimpleNamespace(fetchone=lambda: actual),
                           close=lambda: closed.append(True))
    monkeypatch.setattr(pg, "verified_test_connection", lambda *args, **kwargs: conn)
    with pytest.raises(ValueError, match="Actual private admin server"):
        pg.verified_private_admin_connection(autocommit=True)
    assert closed == [True]


def test_unowned_database_is_rejected_before_connect(owned, monkeypatch):
    monkeypatch.setattr(pg, "verified_test_connection", lambda *args, **kwargs: pytest.fail("must not connect"))
    with pytest.raises(ValueError, match="owned observation-test"):
        pg.verified_private_admin_connection("obs_test_other", autocommit=True)
