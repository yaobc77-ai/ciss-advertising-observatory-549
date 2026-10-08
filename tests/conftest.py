"""Default development collection excludes customer holdout-dependent modules."""

import os
from pathlib import Path

import pytest

from scripts.run_masked_checks import DEPENDENT_LEGACY_TESTS

_HOLDOUT_TEST_PREFIXES = ('test_content_', 'test_prepare_content_', 'test_run_content_',
                          'test_frozen_evaluation', 'test_teacher_reference',
                          'test_statistical_validity', 'test_prepare_statistical_validity')
_HOLDOUT_TEST_FILES = {'test_combined_tools.py', 'test_prepare_rag_training.py',
                       'test_prompt_policy.py', 'test_question_policy.py', 'test_research_agent.py',
                       'test_research_comparison.py', 'test_research_service.py'}


def pytest_ignore_collect(collection_path: Path, config):
    # Ignore before test module import; marking after collection is too late.
    name = collection_path.name
    return (name in _HOLDOUT_TEST_FILES or name in DEPENDENT_LEGACY_TESTS
            or name.startswith(_HOLDOUT_TEST_PREFIXES))


@pytest.fixture(autouse=True)
def _isolated_environment():
    # Settings.from_env() loads .env into os.environ. Without restoring it, one
    # test's dotenv values (e.g. a main-cluster OBS_TEST_DATABASE_URL) leak into
    # later PostgreSQL fixtures, which then refuse to run instead of skipping.
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
