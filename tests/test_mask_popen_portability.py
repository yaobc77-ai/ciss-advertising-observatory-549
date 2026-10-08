"""Synthetic subprocess contracts shared by Windows and POSIX development gates."""

import subprocess
from pathlib import Path

import pytest

from scripts import run_masked_checks as masking


@pytest.mark.parametrize('cwd_type', ['path', 'text'])
@pytest.mark.parametrize('audit_arguments', ['list', 'windows_command_line'])
def test_approved_fixture_normalizes_cwd_before_either_platform_audit(
    tmp_path, cwd_type, audit_arguments,
):
    policy = masking.EngineeringExceptions(temp_root=tmp_path)
    source = tmp_path / 'synthetic source'
    policy.observe_mkdir(source)
    source.mkdir()
    policy.git = str(tmp_path / 'trusted-git')
    cwd = source if cwd_type == 'path' else str(source)
    observed = {}

    class AuditPopen:
        def __init__(self, command, **kwargs):
            observed.update(command=command, **kwargs)
            arguments = (command if audit_arguments == 'list'
                         else subprocess.list2cmdline(command))
            event = (kwargs['executable'], arguments, kwargs['cwd'], kwargs['env'])
            assert policy.allows_popen_audit(event)
            assert not policy.allows_popen_audit((*event[:2], str(source / 'other'), event[3]))
            assert not policy.allows_popen_audit((event[0], [*command, 'extra'], *event[2:]))
            assert not policy.allows_popen_audit((*event[:3], {'UNAPPROVED': 'value'}))

    wrapped = policy.wrap_popen(AuditPopen, [])
    wrapped(['git', 'init', '--quiet'], cwd=cwd)
    assert observed['cwd'] == str(source)
    assert isinstance(observed['cwd'], str)
    assert policy.launching.get() is None
    assert policy.allowed_events[0]['event'] == 'temporary_git_fixture'
    assert not policy.allows_popen_audit((policy.git, observed['command'], str(source), observed['env']))


def test_unapproved_checkout_cwd_is_rejected_before_normalization(tmp_path):
    policy = masking.EngineeringExceptions(temp_root=tmp_path)
    policy.git = str(tmp_path / 'trusted-git')

    class ForbiddenPopen:
        def __init__(self, *args, **kwargs):
            pytest.fail('A checkout command must not launch')

    denied = []
    with pytest.raises(PermissionError, match='Only exact offline'):
        policy.wrap_popen(ForbiddenPopen, denied)(
            ['git', 'init', '--quiet'], cwd=Path(masking.ROOT),
        )
    assert denied == [{'event': 'subprocess_denied'}]
