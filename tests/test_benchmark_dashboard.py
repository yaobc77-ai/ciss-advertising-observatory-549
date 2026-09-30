"""The benchmark must reject a live target before opening any connection."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "benchmark_dashboard", Path(__file__).resolve().parents[1] / "scripts" / "benchmark_dashboard.py",
)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


@pytest.mark.parametrize("url", [
    "", "postgresql://127.0.0.1/observatory", "postgresql://example.org/obs_test",
    "postgresql://localhost/obs_test", "dbname=obs_test", "host=127.0.0.1,example.org dbname=obs_test",
    "host=127.0.0.1 hostaddr=192.0.2.1 dbname=obs_test", "host=127.0.0.1 dbname=obs_test service=live",
])
def test_benchmark_refuses_nonexplicit_test_targets_before_connection(url, monkeypatch):
    def forbidden_connection(*args, **kwargs):
        pytest.fail("An invalid target must be rejected before opening a database connection")

    monkeypatch.setattr(benchmark.psycopg, "connect", forbidden_connection)
    with pytest.raises(ValueError):
        benchmark.verified_connection(url)


def test_loopback_test_target_and_percentile_definition_are_explicit():
    assert benchmark.validate_target("host=127.0.0.1 dbname=obs_test_baseline")["dbname"] == "obs_test_baseline"
    assert benchmark.percentile([100, 0, 200], .5) == 100
    assert benchmark.percentile([100, 0, 200], .95) == 190


@pytest.mark.parametrize("actual_database,address", [
    ("observatory", "127.0.0.1"), ("obs_test", "192.0.2.1/32"),
])
def test_wrong_actual_server_is_rejected_and_connection_closed(monkeypatch, actual_database, address):
    class Connection:
        closed = False

        def execute(self, *args):
            return self

        def fetchone(self):
            return {"database": actual_database, "address": address, "schema": "public"}

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(benchmark.psycopg, "connect", lambda *args, **kwargs: connection)
    with pytest.raises(ValueError, match="not the requested"):
        benchmark.verified_connection("host=127.0.0.1 dbname=obs_test")
    assert connection.closed


def test_postgresql_inet_address_with_netmask_is_validated_before_queries(monkeypatch):
    class Connection:
        committed = False

        def execute(self, *args):
            return self

        def fetchone(self):
            return {"database": "obs_test", "address": "127.0.0.1/32", "schema": "public"}

        def commit(self):
            self.committed = True

    connection = Connection()
    monkeypatch.setattr(benchmark.psycopg, "connect", lambda *args, **kwargs: connection)
    assert benchmark.verified_connection("host=127.0.0.1 dbname=obs_test") is connection
    assert connection.committed
