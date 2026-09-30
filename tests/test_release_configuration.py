"""Check the actual release artifact, rather than an editable source checkout.

CI supplies OBS_RELEASE_WHEEL after building and installing the wheel. Ordinary
source-checkout test runs do not need a build tool or duplicate environment.
"""

import glob
import os
import subprocess
import sys
import zipfile
from importlib import resources
from pathlib import Path

import pytest

import observatory
from observatory.migrations import discover_migrations

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def release_wheel():
    pattern = os.environ.get("OBS_RELEASE_WHEEL")
    if not pattern:
        pytest.skip("OBS_RELEASE_WHEEL is not configured; run in the release environment")
    paths = glob.glob(pattern)
    assert len(paths) == 1, "The release check requires exactly one built wheel"
    return Path(paths[0])


def test_release_wheel_and_installed_package_preserve_every_runtime_resource(release_wheel):
    package = resources.files("observatory")
    source = ROOT / "src" / "observatory"
    expected = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file() and (
            "assets" in path.relative_to(source).parts or path.suffix == ".sql"
        )
    }
    assert expected, "Runtime resource check must not pass with an empty manifest"
    with zipfile.ZipFile(release_wheel) as wheel:
        for relative_path, content in expected.items():
            assert wheel.read(f"observatory/{relative_path}") == content
            assert package.joinpath(relative_path).read_bytes() == content


def test_release_installation_can_discover_migrations_and_start_its_cli(release_wheel):
    assert not Path(observatory.__file__).resolve().is_relative_to(ROOT / "src"), (
        "Release checks must run against an installed wheel, not the editable checkout"
    )
    installed_migrations = discover_migrations()
    source_migrations = discover_migrations(ROOT / "src" / "observatory" / "migrations")
    assert installed_migrations == source_migrations
    with zipfile.ZipFile(release_wheel) as wheel:
        entry_points = [name for name in wheel.namelist() if name.endswith(".dist-info/entry_points.txt")]
        assert len(entry_points) == 1
        assert "observatory = observatory.cli:main" in wheel.read(entry_points[0]).decode("utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "observatory.cli", "--help"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert "migration-status" in result.stdout
    assert "serve" in result.stdout
