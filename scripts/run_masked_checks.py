"""Run free development checks with held-out material denied before collection.

This launcher never opens the holdout to choose cases. It guards Python reads,
records source paths only, and permits only exact offline engineering fixture
commands and newly created synthetic packaging files. It is not an OS sandbox.
"""

from __future__ import annotations

import argparse
import builtins
import configparser
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DENIED_PARTS = ('eval/customer_metrics', 'eval/user_questions', 'eval/selftest',
                '.runtime/full_test_20261007',
                '.runtime/teacher_reference_', '.runtime/statistical_validity_',
                '.runtime/content_source_refresh_', '.runtime/content_review',
                '.runtime/decision_followup_20261006/evaluation',
                '.runtime/accuracy_followup_20261007/customer24',
                '.runtime/accuracy_current_20261007/evaluation_only',
                '.runtime/masked_repair_20261007/root/before_source',
                '客户需求/01_', '客户需求/02_', '客户需求/06_')
DENIED_NAMES = {'24_answer.md', 'rag_validation_requirements.md',
                'test_combined_tools.py', 'test_prepare_rag_training.py',
                'test_prompt_policy.py', 'test_question_policy.py', 'test_research_agent.py',
                'test_research_comparison.py', 'test_research_service.py',
                'prepare_statistical_validity.py', 'prepare_content_review.py',
                'prepare_rag_training.py'}
DENIED_TEST_PREFIXES = ('test_content_', 'test_prepare_content_', 'test_run_content_',
                       'test_frozen_evaluation', 'test_teacher_reference',
                       'test_statistical_validity', 'test_prepare_statistical_validity')

# Observed collection import paths only. These modules were not inspected to
# migrate fixtures; their original bodies remain evaluation-only until replaced
# by new independent development fixtures.
DEPENDENT_LEGACY_TESTS = {
    'test_claims_research.py': 'Imports protected tests/test_research_agent.py.',
    'test_claims_source_search.py': 'Imports protected tests/test_research_agent.py.',
    'test_mcp_server.py': 'Imports test_claims_research.py, which imports protected tests/test_research_agent.py.',
    'test_rag_training.py': 'Loads protected scripts/prepare_rag_training.py through SPEC.loader.exec_module.',
    'test_selftest_cases.py': 'Imports scripts/run_selftest.py, which reads protected eval/selftest/cases.json.',
    'test_selftest_runner.py': 'Imports scripts/run_selftest.py, which reads protected eval/selftest/cases.json.',
    'test_structured_shares.py': 'Imports protected tests/test_research_agent.py.',
    'test_user_questions.py': 'Reads protected eval/user_questions/cases.json during collection.',
    'test_web_research.py': 'Imports protected tests/test_research_agent.py.',
}

SYNTHETIC_PACKAGING_SUFFIX = ('eval', 'customer_metrics', 'content_questions.v1.json')


class EngineeringExceptions:
    """Exact offline fixture operations; never relax the checkout's holdout rules."""

    def __init__(self, root=ROOT, temp_root=None):
        self.root = Path(root).resolve()
        self.temp_root = Path(temp_root or tempfile.gettempdir()).resolve()
        self.created_directories = set()
        self.synthetic_files = {}
        self.allowed_events = []
        self.creating = ContextVar('creating_synthetic_packaging_file', default=None)
        self.launching = ContextVar('launching_engineering_child', default=None)
        self.git = shutil.which('git')
        self.child_environment = dict(os.environ)

    def temporary_path(self, path):
        path = Path(path).absolute()
        if (path == self.root or self.root in path.parents
                or self.temp_root not in path.parents):
            return None
        # Resolve and inspect the original spelling: aliases cannot grant access.
        for current in (path, *path.parents):
            if current.is_symlink() or current.is_junction():
                return None
        return path.resolve() if path.resolve() == path else None

    def observe_mkdir(self, path):
        temporary = self.temporary_path(path)
        if temporary is not None and not temporary.exists():
            self.created_directories.add(temporary)

    def fixture_path(self, path):
        # Ordinary opens cannot qualify. Reject by spelling before filesystem
        # alias checks; the separate canonical holdout guard still handles them.
        if Path(path).parts[-3:] != SYNTHETIC_PACKAGING_SUFFIX:
            return None
        temporary = self.temporary_path(path)
        if (temporary is None or temporary.parts[-3:] != SYNTHETIC_PACKAGING_SUFFIX
                or temporary.parent not in self.created_directories):
            return None
        return temporary

    @staticmethod
    def identity(path):
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        return info.st_dev, info.st_ino

    def allows_open(self, path, flags):
        fixture = self.fixture_path(path)
        if fixture is None:
            return False
        if self.creating.get() == fixture and flags & os.O_CREAT and not fixture.exists():
            return True
        try:
            return (fixture in self.synthetic_files
                    and self.identity(fixture) == self.synthetic_files[fixture])
        except OSError:
            return False

    def wrap_open(self, original):
        def fixture_open(file, mode='r', *args, **kwargs):
            if not isinstance(file, (str, bytes, os.PathLike)):
                return original(file, mode, *args, **kwargs)
            path = self.fixture_path(os.fsdecode(file))
            if path is None or not any(flag in mode for flag in 'wax') or path.exists():
                return original(file, mode, *args, **kwargs)
            token = self.creating.set(path)
            try:
                stream = original(file, mode, *args, **kwargs)
                info = os.fstat(stream.fileno())
                self.synthetic_files[path] = (info.st_dev, info.st_ino)
                self.allowed_events.append({'event': 'synthetic_packaging_created', 'path': str(path)})
                return stream
            finally:
                self.creating.reset(token)
        return fixture_open

    def git_config_is_local(self, cwd):
        repository = cwd / '.git'
        if repository.exists() and (not repository.is_dir() or self.temporary_path(repository) is None):
            return False
        if (repository / 'objects/info/alternates').exists():
            return False
        config = repository / 'config'
        if not config.exists():
            return True
        if self.temporary_path(config) is None:
            return False
        parser = configparser.ConfigParser()
        try:
            parser.read_string(config.read_text(encoding='utf-8'))
            allowed = {'repositoryformatversion', 'filemode', 'bare', 'logallrefupdates',
                       'symlinks', 'ignorecase', 'hookspath'}
            if parser.sections() != ['core'] or set(parser['core']) - allowed:
                return False
            hooks = parser['core'].get('hookspath')
            return not hooks or self.empty_created_directory(hooks)
        except (OSError, UnicodeError, configparser.Error):
            return False

    def empty_created_directory(self, path):
        temporary = self.temporary_path(path)
        return (temporary in self.created_directories and temporary.is_dir()
                and not any(temporary.iterdir()))

    def child_command(self, command, cwd):
        if not isinstance(command, (tuple, list)) or not command:
            return None
        command = [os.fsdecode(item) for item in command]
        if command == [sys.executable, '-m', 'observatory.cli', '--help']:
            return command, 'cli_help'
        if not self.git or command[0] not in {'git', self.git}:
            return None
        temporary = self.temporary_path(cwd or Path.cwd())
        if (temporary not in self.created_directories or not temporary.is_dir()
                or not self.git_config_is_local(temporary)):
            return None
        arguments = command[1:]
        if arguments[:2] == ['-c', 'core.autocrlf=false']:
            arguments = arguments[2:]
        accepted = arguments in [
            ['init', '--quiet'], ['add', '--all'], ['rev-parse', '--show-toplevel'],
            ['rev-parse', '--verify', 'HEAD'], ['rev-parse', 'HEAD'],
            ['ls-files', '--cached', '-z'], ['ls-files', '--stage', '-z'],
            ['ls-files', '--others', '--exclude-standard', '-z'],
            ['diff', '--name-only', '--no-renames', 'HEAD', '-z'],
            ['status', '--porcelain=v1', '--untracked-files=all', '-z'],
            ['-c', 'user.name=Handoff fixture', '-c', 'user.email=fixture@example.invalid',
             'commit', '--quiet', '-m', 'Synthetic source fixture'],
        ]
        if arguments[:2] == ['config', 'core.hooksPath'] and len(arguments) == 3:
            accepted = self.empty_created_directory(arguments[2])
        if not accepted:
            return None
        # Disable hooks, templates, signing and fsmonitor even if local settings drift.
        return ([self.git, '--no-pager', '-c', 'core.hooksPath=' + os.devnull,
                 '-c', 'core.fsmonitor=false', '-c', 'commit.gpgsign=false',
                 '-c', 'protocol.allow=never', *command[1:]], 'temporary_git_fixture')

    def sanitized_child_environment(self, kind):
        runtime_keys = {'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP',
                        'HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'LANG', 'LC_ALL',
                        'LC_CTYPE', 'TZ', 'OS'}
        env = {name: value for name, value in self.child_environment.items()
               if name.upper() in runtime_keys}
        env.update(PYTHON_DOTENV_DISABLED='1', PYTHONPATH=str(self.root / 'src'))
        if kind == 'temporary_git_fixture':
            env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_SYSTEM=os.devnull,
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_TEMPLATE_DIR='',
                       GIT_ATTR_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
        return env

    def wrap_popen(self, original, denied):
        policy = self

        # asyncio.windows_utils subclasses Popen; retain its class interface.
        class FixturePopen(original):
            def __init__(self, *args, **kwargs):
                command = args[0] if args else kwargs.get('args')
                approved = None if (len(args) > 1 or kwargs.get('shell') or kwargs.get('executable')) else (
                    policy.child_command(command, kwargs.get('cwd')))
                if approved is None:
                    denied.append({'event': 'subprocess_denied'})
                    raise PermissionError('Only exact offline engineering fixture commands are allowed.')
                safe_command, kind = approved
                parameters = dict(kwargs)
                parameters.pop('args', None)
                parameters['env'] = policy.sanitized_child_environment(kind)
                parameters['executable'] = safe_command[0]
                if kind == 'cli_help':
                    parameters['cwd'] = str(policy.root)
                cwd = parameters.get('cwd')
                token = policy.launching.set((safe_command, os.fsdecode(cwd) if cwd is not None else None,
                                             parameters['env']))
                try:
                    super().__init__(safe_command, **parameters)
                    policy.allowed_events.append({'event': kind, 'command': safe_command,
                                                  'cwd': str(parameters.get('cwd') or Path.cwd())})
                finally:
                    policy.launching.reset(token)
        return FixturePopen

    def allows_popen_audit(self, values):
        approved = self.launching.get()
        if approved is None:
            return False
        command, cwd, env = approved
        executable, arguments, actual_cwd, actual_env = values
        return (executable == command[0] and actual_cwd == cwd and actual_env == env
                and arguments in (command, subprocess.list2cmdline(command)))


def protected_path(path):
    try:
        normalized = str(Path(path).resolve()).replace('\\', '/').casefold()
        name = Path(normalized).name
        if name.endswith('.pyc'):
            # Importlib may use a cache without opening its .py source.
            name = name.split('.', 1)[0] + '.py'
    except (TypeError, ValueError, OSError):
        return False
    return (any(part.casefold() in normalized for part in DENIED_PARTS)
            or name in DENIED_NAMES or name in DEPENDENT_LEGACY_TESTS
            or name.startswith(DENIED_TEST_PREFIXES))


def test_exclusion_reason(path):
    """Describe exclusion from filename rules, without opening a legacy module."""
    name = Path(path).name.casefold()
    if name in DEPENDENT_LEGACY_TESTS:
        return {'kind': 'dependent_legacy', 'reason': DEPENDENT_LEGACY_TESTS[name]}
    if name in DENIED_NAMES:
        return {'kind': 'direct_holdout', 'reason': 'Exact protected legacy module name.'}
    prefix = next((prefix for prefix in DENIED_TEST_PREFIXES if name.startswith(prefix)), None)
    if prefix:
        return {'kind': 'direct_holdout', 'reason': 'Protected legacy module prefix: ' + prefix}
    return {'kind': 'direct_holdout', 'reason': 'Path matches a protected holdout location.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('pytest_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    report = args.report_dir.resolve()
    if protected_path(report) or ROOT not in report.parents:
        raise ValueError('Use a new unprotected report directory inside this checkout.')
    report.mkdir(parents=True, exist_ok=False)
    pytest_args = args.pytest_args
    if pytest_args and pytest_args[0] == '--':
        pytest_args = pytest_args[1:]
    if any(protected_path(item.split('::', 1)[0]) for item in pytest_args if not item.startswith('-')):
        raise PermissionError('A held-out test module was requested in development.')
    reads, denied = set(), []
    engineering = EngineeringExceptions()

    def guard(event, values):
        if event == 'open' and values:
            path = values[0]
            if not isinstance(path, (str, bytes, os.PathLike)):
                return
            normalized = str(Path(os.fsdecode(path)).resolve())
            if protected_path(normalized) and not engineering.allows_open(path, values[2]):
                denied.append({'event': 'read_denied', 'path': normalized})
                raise PermissionError('Held-out material cannot be read by development checks.')
            if ROOT in Path(normalized).parents:
                reads.add(normalized)
        elif event == 'os.mkdir' and values:
            engineering.observe_mkdir(values[0])
        elif event == 'subprocess.Popen' and engineering.allows_popen_audit(values):
            return
        elif event in {'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.posix_spawnp',
                       'os.exec', 'os.spawn', 'os.fork', 'os.forkpty'}:
            denied.append({'event': 'subprocess_denied'})
            raise PermissionError('Subprocess development reads require a separate masked invocation.')

    builtins.open = engineering.wrap_open(builtins.open)
    io.open = engineering.wrap_open(io.open)
    subprocess.Popen = engineering.wrap_popen(subprocess.Popen, denied)
    sys.addaudithook(guard)
    # A reused virtual environment may contain an editable install pointing at
    # another worktree. Always test this checkout's source and launcher modules.
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / 'src'))
    import pytest

    exit_code = int(pytest.main([*(pytest_args or ['tests', '-m', 'not live and not integration', '-q']),
                                 '--junitxml=' + str(report / 'junit.xml')]))
    file_hashes = {}
    for filename in sorted(reads):
        path = Path(filename)
        if path.is_file() and path.suffix in {'.py', '.pyc', '.sql', '.toml', '.json'}:
            file_hashes[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt = {'at_utc': datetime.now(timezone.utc).isoformat(), 'purpose': 'development',
               'exit_code': exit_code, 'read_paths_sha256': file_hashes, 'denied_events': denied,
               'engineering_exceptions': engineering.allowed_events,
               'customer_questions_loaded': False, 'human_accuracy': None,
               'scope': 'Python process read/collection guard; no OS sandbox or unseen-data assertion'}
    (report / 'mask_receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
