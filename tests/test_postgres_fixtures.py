"""Private-target guards must fail before opening any connection."""

import pytest
from postgres_fixtures import configured_test_url, validate_test_target
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def target(**changes):
    return make_conninfo(host="127.0.0.1", port=55499, dbname="obs_test_synthetic", **changes)


@pytest.mark.parametrize("field,value", [
    ("dbname", "observatory"), ("dbname", "obs_test;DROP"),
    ("host", "localhost"), ("host", "192.0.2.1"),
    ("hostaddr", "192.0.2.1"), ("port", "55432"),
    ("port", "0"), ("service", "external"), ("replication", "true"),
])
def test_refuses_unsafe_or_main_runtime_target(field, value):
    base = dict(host="127.0.0.1", port="55499", dbname="obs_test_synthetic")
    base[field] = value
    with pytest.raises(ValueError):
        validate_test_target(make_conninfo(**base))


def test_configured_url_preserves_private_port_and_pins_numeric_address(monkeypatch):
    monkeypatch.setenv("OBS_TEST_DATABASE_URL", target())
    monkeypatch.setenv("PGHOSTADDR", "192.0.2.1")
    configured = conninfo_to_dict(configured_test_url())
    assert configured["port"] == "55499"
    assert configured["hostaddr"] == "127.0.0.1"
    assert configured["dbname"] == "obs_test_synthetic"


def test_missing_configuration_skips_without_main_cluster_fallback(monkeypatch):
    monkeypatch.delenv("OBS_TEST_DATABASE_URL", raising=False)
    with pytest.raises(pytest.skip.Exception):
        configured_test_url()
