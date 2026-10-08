"""Fresh boundary cases for exact engineering exceptions; no held-out content."""

import io
import os
import subprocess
import sys

import pytest

from scripts import run_masked_checks as masking


@pytest.fixture
def policy(tmp_path):
    return masking.EngineeringExceptions(temp_root=tmp_path)


def create_directory(policy, path):
    policy.observe_mkdir(path)
    path.mkdir()
    return path


def packaging_path(policy, tmp_path):
    source = create_directory(policy, tmp_path / 'source')
    evaluation = create_directory(policy, source / 'eval')
    customer = create_directory(policy, evaluation / 'customer_metrics')
    return customer / 'content_questions.v1.json'


def test_exact_new_synthetic_packaging_file_can_be_written_then_read(policy, tmp_path):
    path = packaging_path(policy, tmp_path)
    with policy.wrap_open(io.open)(path, 'w', encoding='utf-8') as stream:
        stream.write('{"status":"synthetic packaging fixture"}')
    assert policy.allows_open(path, os.O_RDONLY)
    assert path in policy.synthetic_files
    assert policy.allowed_events[0]['event'] == 'synthetic_packaging_created'


def test_preexisting_packaging_file_is_not_registered_by_a_later_write(policy, tmp_path):
    path = packaging_path(policy, tmp_path)
    path.write_text('{"status":"synthetic existing file"}', encoding='utf-8')
    with policy.wrap_open(io.open)(path, 'w', encoding='utf-8') as stream:
        stream.write('{"status":"synthetic rewritten file"}')
    assert not policy.allows_open(path, os.O_RDONLY)
    assert not policy.synthetic_files


def test_creation_permission_is_scoped_to_the_open_wrapper(policy, tmp_path):
    path = packaging_path(policy, tmp_path)
    assert not policy.allows_open(path, os.O_CREAT | os.O_WRONLY)
    token = policy.creating.set(path)
    try:
        assert policy.allows_open(path, os.O_CREAT | os.O_WRONLY)
        assert not policy.allows_open(path, os.O_RDONLY)
    finally:
        policy.creating.reset(token)
    assert not policy.allows_open(path, os.O_CREAT | os.O_WRONLY)


def test_unrelated_open_rejects_exception_without_filesystem_alias_checks(policy, tmp_path, monkeypatch):
    def forbidden_path_check(path):
        pytest.fail('Ordinary paths must fast-reject the engineering exception')

    monkeypatch.setattr(policy, 'temporary_path', forbidden_path_check)
    assert policy.fixture_path(tmp_path / 'ordinary-fixture.json') is None
    assert policy.fixture_path(masking.ROOT / 'scripts/run_project_checks.py') is None
    assert policy.fixture_path(tmp_path / '24_answer.md') is None


@pytest.mark.parametrize('name', ['24_answer.md', 'test_question_policy.py', 'test_content_fresh.py'])
def test_protected_names_never_receive_a_synthetic_exception(policy, tmp_path, name):
    path = tmp_path / name
    assert masking.protected_path(path)
    assert policy.fixture_path(path) is None
    assert not policy.allows_open(path, os.O_CREAT | os.O_WRONLY)


@pytest.mark.parametrize('name', sorted(masking.DEPENDENT_LEGACY_TESTS))
def test_observed_dependent_legacy_modules_and_caches_remain_protected(policy, tmp_path, name):
    assert masking.protected_path(tmp_path / name)
    cache = tmp_path / '__pycache__' / (name[:-3] + '.cpython-312-pytest-9.1.1.pyc')
    assert masking.protected_path(cache)
    assert not policy.allows_open(tmp_path / name, os.O_RDONLY)
    assert masking.test_exclusion_reason(name)['kind'] == 'dependent_legacy'


@pytest.mark.parametrize('name', ['prepare_statistical_validity.py',
                                'prepare_content_review.py', 'prepare_rag_training.py'])
def test_confirmed_historical_preparation_scripts_and_caches_remain_protected(policy, tmp_path, name):
    assert masking.protected_path(masking.ROOT / 'scripts' / name)
    assert masking.protected_path(tmp_path / name)
    assert masking.protected_path(tmp_path / '__pycache__' / (name[:-3] + '.cpython-312.pyc'))
    assert not policy.allows_open(tmp_path / name, os.O_RDONLY)


def test_real_checkout_paths_never_receive_a_synthetic_exception(policy):
    for relative in ('eval/customer_metrics/content_questions.v1.json',
                     '.runtime/customer_holdout/references/24_answer.md',
                     'tests/test_question_policy.py'):
        path = masking.ROOT / relative
        assert masking.protected_path(path)
        assert policy.fixture_path(path) is None
        assert not policy.allows_open(path, os.O_RDONLY)


def test_uncreated_temporary_directory_does_not_grant_fixture_reads(policy, tmp_path):
    path = tmp_path / 'existing/eval/customer_metrics/content_questions.v1.json'
    path.parent.mkdir(parents=True)
    assert policy.fixture_path(path) is None


def test_registered_path_replaced_by_a_directory_loses_read_permission(policy, tmp_path):
    path = packaging_path(policy, tmp_path)
    with policy.wrap_open(io.open)(path, 'w') as stream:
        stream.write('synthetic bytes')
    path.unlink()
    path.mkdir()
    assert not policy.allows_open(path, os.O_RDONLY)


def test_registered_file_with_a_new_hardlink_loses_read_permission(policy, tmp_path):
    path = packaging_path(policy, tmp_path)
    with policy.wrap_open(io.open)(path, 'w') as stream:
        stream.write('synthetic bytes')
    os.link(path, tmp_path / 'synthetic-link.json')
    assert not policy.allows_open(path, os.O_RDONLY)


def test_temporary_alias_cannot_grant_checkout_access(policy, tmp_path):
    alias = tmp_path / 'alias'
    try:
        alias.symlink_to(masking.ROOT, target_is_directory=True)
    except OSError:
        pytest.skip('Host cannot create a test symlink')
    assert policy.temporary_path(alias / 'eval/customer_metrics/content_questions.v1.json') is None


def test_exact_git_fixture_commands_are_local_and_force_offline_options(policy, tmp_path):
    policy.git = str(tmp_path / 'trusted-git')
    source = create_directory(policy, tmp_path / 'source')
    approved, kind = policy.child_command(['git', '-c', 'core.autocrlf=false', 'init', '--quiet'], source)
    assert kind == 'temporary_git_fixture'
    assert approved[0] == policy.git
    assert 'protocol.allow=never' in approved
    assert 'core.hooksPath=' + os.devnull in approved
    assert policy.child_command(['git', 'rev-parse', 'HEAD'], source)


@pytest.mark.parametrize('arguments', [
    ['fetch'], ['clone', 'https://example.invalid/repo'], ['show', 'HEAD:private.md'],
    ['-c', 'core.hooksPath=foreign', 'status'], ['config', 'include.path', 'foreign'],
    ['commit', '-m', 'unapproved message'], ['--git-dir=foreign', 'status'],
])
def test_arbitrary_git_commands_remain_denied(policy, tmp_path, arguments):
    policy.git = 'trusted-git'
    source = create_directory(policy, tmp_path / 'source')
    assert policy.child_command(['git', *arguments], source) is None


def test_git_does_not_operate_on_checkout_or_uncreated_temp_directory(policy, tmp_path):
    policy.git = 'trusted-git'
    assert policy.child_command(['git', 'init', '--quiet'], masking.ROOT) is None
    tmp_path.joinpath('existing').mkdir()
    assert policy.child_command(['git', 'init', '--quiet'], tmp_path / 'existing') is None


def test_git_hooks_configuration_requires_an_empty_new_temp_directory(policy, tmp_path):
    policy.git = 'trusted-git'
    source = create_directory(policy, tmp_path / 'source')
    hooks = create_directory(policy, tmp_path / 'empty-hooks')
    assert policy.child_command(['git', 'config', 'core.hooksPath', str(hooks)], source)
    (hooks / 'hook').write_text('synthetic hook file')
    assert policy.child_command(['git', 'config', 'core.hooksPath', str(hooks)], source) is None


def test_git_includes_filters_and_alternate_object_stores_are_denied(policy, tmp_path):
    policy.git = 'trusted-git'
    source = create_directory(policy, tmp_path / 'source')
    repository = create_directory(policy, source / '.git')
    for content in ('[include]\npath = foreign\n', '[filter "fixture"]\nclean = foreign\n'):
        (repository / 'config').write_text(content)
        assert policy.child_command(['git', 'add', '--all'], source) is None
    (repository / 'config').write_text('[core]\nbare = false\n')
    assert policy.child_command(['git', 'add', '--all'], source)
    (repository / 'objects/info').mkdir(parents=True)
    (repository / 'objects/info/alternates').touch()
    assert policy.child_command(['git', 'add', '--all'], source) is None


def test_only_exact_cli_help_is_approved(policy):
    assert policy.child_command([sys.executable, '-m', 'observatory.cli', '--help'], None)
    for command in ([sys.executable, '-m', 'observatory.cli', 'health'],
                    [sys.executable, '-c', 'print("synthetic")'],
                    ['powershell.exe', '-Command', 'synthetic'], 'git status'):
        assert policy.child_command(command, None) is None


def test_child_environment_removes_authority_and_external_git_python_settings(policy):
    policy.child_environment.update(OBS_DATABASE_URL='synthetic', OPENAI_API_KEY='synthetic',
                                    PGSERVICE='synthetic', GIT_CONFIG_COUNT='2', PYTHONHOME='foreign')
    env = policy.sanitized_child_environment('temporary_git_fixture')
    assert not {'OBS_DATABASE_URL', 'OPENAI_API_KEY', 'PGSERVICE', 'GIT_CONFIG_COUNT', 'PYTHONHOME'} & env.keys()
    assert env['GIT_CONFIG_GLOBAL'] == os.devnull
    assert env['GIT_TERMINAL_PROMPT'] == '0'
    assert env['PYTHON_DOTENV_DISABLED'] == '1'
    assert env['PYTHONPATH'] == str(masking.ROOT / 'src')


def test_popen_class_compatibility_and_unapproved_audit_calls(policy):
    wrapped = policy.wrap_popen(subprocess.Popen, [])
    assert issubclass(wrapped, subprocess.Popen)
    assert not policy.allows_popen_audit((sys.executable, [], None, {}))
    command = [sys.executable, '-m', 'observatory.cli', '--help']
    token = policy.launching.set((command, None, {}))
    try:
        assert policy.allows_popen_audit((sys.executable, command, None, {}))
        assert policy.allows_popen_audit((sys.executable, subprocess.list2cmdline(command), None, {}))
        assert not policy.allows_popen_audit((sys.executable, [*command, 'foreign'], None, {}))
    finally:
        policy.launching.reset(token)


def test_shell_and_executable_overrides_are_refused_before_launch(policy):
    class FakePopen:
        def __init__(self, *args, **kwargs):
            pytest.fail('No unapproved process may be launched')

    denied = []
    wrapped = policy.wrap_popen(FakePopen, denied)
    for kwargs in ({'shell': True}, {'executable': 'foreign'}):
        with pytest.raises(PermissionError):
            wrapped([sys.executable, '-m', 'observatory.cli', '--help'], **kwargs)
    assert denied == [{'event': 'subprocess_denied'}, {'event': 'subprocess_denied'}]


def test_cli_help_forces_checkout_cwd_and_runtime_environment(policy, tmp_path):
    observed = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            observed.update(command=command, **kwargs)

    wrapped = policy.wrap_popen(FakePopen, [])
    wrapped([sys.executable, '-m', 'observatory.cli', '--help'], cwd=tmp_path,
            env={'PYTHONPATH': 'untrusted source', 'OBS_DATABASE_URL': 'synthetic'})
    assert observed['cwd'] == str(masking.ROOT)
    assert observed['executable'] == sys.executable
    assert observed['env']['PYTHONPATH'] == str(masking.ROOT / 'src')
    assert 'OBS_DATABASE_URL' not in observed['env']
