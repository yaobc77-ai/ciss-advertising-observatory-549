"""Explicit private PostgreSQL targets for synthetic integration fixtures."""

import ipaddress
import json
import os
import re
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
OBSERVATION_DATABASE_PREFIX = "obs_test_observations_20261006_"


def validate_test_target(url: str) -> dict:
    if not url:
        raise ValueError("An explicit OBS_TEST_DATABASE_URL is required")
    try:
        target = conninfo_to_dict(url)
    except psycopg.Error:
        raise ValueError("Invalid private test connection setting") from None
    if not re.fullmatch(r"obs_test[a-zA-Z0-9_]*", target.get("dbname", "")):
        raise ValueError("Refusing a database outside obs_test*")
    host = target.get("host", "")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback or target.get("hostaddr", host) != host:
        raise ValueError("An explicit matching numeric loopback address is required")
    port = target.get("port", "")
    if not port.isdecimal() or not 1 <= int(port) <= 65535 or int(port) == 55432:
        raise ValueError("A private test port distinct from the main runtime is required")
    if target.get("service") or target.get("replication"):
        raise ValueError("Indirect or replication test targets are refused")
    return target


def configured_test_url() -> str:
    """Do not load .env or derive credentials from the main project cluster."""
    url = os.environ.get("OBS_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("OBS_TEST_DATABASE_URL is not explicitly configured")
    target = validate_test_target(url)
    return make_conninfo(url, hostaddr=target["host"])


def validate_owned_observation_database(name: str) -> str:
    """Accept only the freshly generated identifiers this fixture owns."""
    if not re.fullmatch(OBSERVATION_DATABASE_PREFIX + r"[0-9a-f]{12}", name):
        raise ValueError("An exact owned observation-test database identifier is required")
    return name


def configured_private_admin_url() -> str:
    """Explicit lifecycle capability for the same marked private base instance.

    The ordinary test URL remains the restricted application role. This helper
    does not load .env or credential files and never falls back to that role.
    """
    url = os.environ.get("OBS_TEST_ADMIN_DATABASE_URL", "")
    if not url:
        raise ValueError("An explicit private admin setting is required for owned database lifecycle")
    app = validate_test_target(configured_test_url())
    admin = validate_test_target(url)
    if (app.get("dbname") != "obs_test" or admin.get("dbname") != "obs_test"
            or app.get("user") != "obs_private_app" or admin.get("user") != "obs_private_admin"
            or any(app.get(key) != admin.get(key) for key in ("host", "port"))
            or app["host"] != "127.0.0.1" or int(app["port"]) in {5432, 55432}):
        raise ValueError("Private admin and application settings must match one private obs_test instance")
    name = os.environ.get("OBS_TEST_PRIVATE_CLUSTER", "")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name) or name == "default":
        raise ValueError("An explicitly owned non-default private cluster name is required")
    workspace = ROOT.resolve()
    directory = workspace / ".runtime/private_test_postgres" / name
    marker = directory / "cluster.json"
    data = directory / "data"
    if any(path.resolve() != path for path in (directory, marker, data)):
        raise ValueError("Private admin cluster paths must be canonical and unlinked")
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Private admin cluster ownership marker is missing or invalid") from None
    if (not isinstance(state, dict) or state.get("kind") != "observatory-private-test-v1"
            or state.get("workspace") != str(workspace)
            or state.get("data_directory") != str(data)
            or state.get("host") != app["host"] or state.get("database") != "obs_test"
            or type(state.get("port")) is not int or state["port"] != int(app["port"])):
        raise ValueError("Private admin target differs from its owned cluster marker")
    return make_conninfo(url, hostaddr=admin["host"])


def verified_private_application_owner(connection) -> str:
    """Fail before privileged lifecycle work if the app role is elevated."""
    role = connection.execute("""SELECT current_user AS name,
        rolsuper,rolcreatedb,rolcreaterole,rolreplication
        FROM pg_roles WHERE rolname=current_user""").fetchone()
    if (role["name"] != "obs_private_app"
            or any(role[key] for key in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication"))):
        raise ValueError("The private application role must remain restricted")
    return role["name"]


def verified_private_admin_connection(database="obs_test", *, autocommit=False):
    """Verify the actual marked server before any owned database lifecycle SQL."""
    if database != "obs_test":
        validate_owned_observation_database(database)
    url = make_conninfo(configured_private_admin_url(), dbname=database)
    conn = verified_test_connection(url, autocommit=autocommit)
    try:
        actual = conn.execute("""SELECT current_user AS name,
            current_setting('data_directory') AS data_directory""").fetchone()
        data = ROOT.resolve() / ".runtime/private_test_postgres" / os.environ["OBS_TEST_PRIVATE_CLUSTER"] / "data"
        if (actual["name"] != "obs_private_admin"
                or Path(actual["data_directory"]).resolve() != data):
            raise ValueError("Actual private admin server differs from its owned cluster")
        if not autocommit:
            conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def verified_test_connection(url: str, schema: str | None = None, *, autocommit=False):
    target = validate_test_target(url)
    if schema is not None and not re.fullmatch(r"obs_[a-z0-9_]+", schema):
        raise ValueError("Unexpected owned test schema name")
    options = "-c statement_timeout=60000 -c lock_timeout=5000"
    if schema:
        options += f" -c search_path={schema},public"
    conn = psycopg.connect(
        make_conninfo(url, hostaddr=target["host"], options=options),
        connect_timeout=5, row_factory=dict_row, autocommit=autocommit,
    )
    try:
        actual = conn.execute("""SELECT current_database() AS database,
            inet_server_addr()::text AS address,inet_server_port() AS port,
            current_schema() AS schema""").fetchone()
        if (actual["database"] != target["dbname"]
                or str(ipaddress.ip_interface(actual["address"]).ip) != target["host"]
                or actual["port"] != int(target["port"])):
            raise ValueError("Actual server differs from the requested private test target")
        if schema and actual["schema"] != schema:
            raise ValueError("The owned test schema is not the current search path")
        if not autocommit:
            conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn
