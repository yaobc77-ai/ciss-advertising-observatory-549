"""Reproduce the lazy POSIX processor probe without launching a real child."""

import platform
import subprocess

import pytest
from openai import _base_client

from scripts import run_masked_checks as masking


@pytest.fixture
def synthetic_linux(monkeypatch):
    # Exercise the Linux branch on any host without altering sys.platform.
    monkeypatch.setattr(platform, '_uname_cache', platform.uname_result(
        'Linux', 'synthetic-host', '1', 'synthetic-version', 'x86_64',
    ))
    monkeypatch.setattr(platform, '_platform_cache', {})
    monkeypatch.setattr(platform, 'libc_ver', lambda: ('synthetic-libc', '1'))
    monkeypatch.setattr(platform._Processor, 'get', classmethod(
        lambda cls: cls.from_subprocess() or '',
    ))
    monkeypatch.setattr(_base_client.distro, 'id', lambda: 'synthetic-linux')


def forbid_child_probe(monkeypatch, tmp_path):
    denied = []
    policy = masking.EngineeringExceptions(temp_root=tmp_path)

    class NeverPopen:
        def __init__(self, *args, **kwargs):
            pytest.fail('The synthetic processor command must never launch')

    wrapped = policy.wrap_popen(NeverPopen, denied)
    monkeypatch.setattr(subprocess, 'check_output', lambda command, **kwargs: wrapped(command))
    return denied


def test_uncached_sdk_platform_probe_can_catch_denial_and_still_succeed(
    synthetic_linux, monkeypatch, tmp_path,
):
    denied = forbid_child_probe(monkeypatch, tmp_path)
    assert _base_client.get_platform() == 'Linux'
    # platform._Processor catches PermissionError as an OSError. A successful
    # SDK result therefore does not prove that the offline receipt is clean.
    assert denied == [{'event': 'subprocess_denied'}]


def test_prepared_platform_cache_avoids_probe_under_unchanged_guard(
    synthetic_linux, monkeypatch, tmp_path,
):
    preparation = []

    def synthetic_probe(command, **kwargs):
        assert command == ['uname', '-p']
        preparation.append(command)
        return 'synthetic-processor\n'

    monkeypatch.setattr(subprocess, 'check_output', synthetic_probe)
    masking.prepare_runtime_metadata()
    assert preparation == [['uname', '-p']]
    denied = forbid_child_probe(monkeypatch, tmp_path)
    assert _base_client.get_platform() == 'Linux'
    assert denied == []
    policy = masking.EngineeringExceptions(temp_root=tmp_path)
    assert policy.child_command(['uname', '-p'], None) is None
