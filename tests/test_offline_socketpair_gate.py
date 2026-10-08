"""Fresh local transport boundaries; no external connections or question data."""

import socket

import pytest

from scripts import run_project_checks as checks


def test_actual_captured_stdlib_socketpair_operates_and_context_is_not_inherited():
    left, right = checks._stdlib_socketpair()
    try:
        left.sendall(b"synthetic socketpair bytes")
        assert right.recv(128) == b"synthetic socketpair bytes"
        assert checks._SOCKETPAIR_CONTEXT.get() is False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as ordinary:
            denied = []
            with pytest.raises(PermissionError):
                checks.guard_network_operation("socket.connect", (ordinary, ("127.0.0.1", 12345)), denied)
            assert denied == [{"kind": "network", "event": "socket.connect"}]
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("event,address", [
    ("socket.connect", ("127.0.0.1", 12345)),
    ("socket.connect", ("203.0.113.1", 12345)),
    ("socket.connect", ("localhost", 12345)),
    ("socket.getaddrinfo", ("127.0.0.1", 12345)),
    ("socket.sendto", ("127.0.0.1", 12345)),
])
def test_ordinary_network_operations_are_denied_by_isolated_policy(event, address):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        denied = []
        with pytest.raises(PermissionError):
            checks.guard_network_operation(event, (connection, address), denied)
        assert denied == [{"kind": "network", "event": event}]


@pytest.mark.parametrize("address", [
    ("203.0.113.1", 12345), ("localhost", 12345), ("127.0.0.1", 0),
    ("127.0.0.1", True), ("127.0.0.1", 12345, "extra"),
])
def test_socketpair_context_does_not_authorize_other_addresses_or_resolution(address):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        token = checks._SOCKETPAIR_CONTEXT.set(True)
        try:
            assert checks.internal_socketpair_connect((connection, ("127.0.0.1", 12345)))
            assert not checks.internal_socketpair_connect((connection, address))
            for event in ("socket.getaddrinfo", "socket.sendto"):
                with pytest.raises(PermissionError):
                    checks.guard_network_operation(event, (connection, ("127.0.0.1", 12345)), [])
        finally:
            checks._SOCKETPAIR_CONTEXT.reset(token)


def test_socketpair_context_is_reset_when_captured_original_raises(monkeypatch):
    def failure(*args, **kwargs):
        raise RuntimeError("synthetic local transport failure")

    monkeypatch.setattr(checks, "_ORIGINAL_SOCKETPAIR", failure)
    with pytest.raises(RuntimeError):
        checks._stdlib_socketpair()
    assert checks._SOCKETPAIR_CONTEXT.get() is False
