"""Release verification must catch missing wheel files and unsafe DB authority."""

import importlib.util
from pathlib import Path
from zipfile import ZipFile

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_current_release.py"
_SPEC = importlib.util.spec_from_file_location("release_verification", _SCRIPT)
verification = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verification)


def make_wheel(tmp_path, wheel_content=b"same"):
    root = tmp_path / "source"
    source = root / "src" / "observatory"
    (source / "migrations").mkdir(parents=True)
    (source / "assets").mkdir()
    (source / "__init__.py").write_bytes(b"same")
    (source / "migrations" / "0001_baseline.sql").write_bytes(b"SELECT 1;")
    (source / "assets" / "graph.js").write_bytes(b"const ready = true;")
    (source / "assets" / "tools.svg").write_bytes(b"<svg/>")
    wheel = tmp_path / "fixture.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("observatory/__init__.py", wheel_content)
        archive.writestr("observatory/migrations/0001_baseline.sql", b"SELECT 1;")
        archive.writestr("observatory/assets/graph.js", b"const ready = true;")
        archive.writestr("observatory/assets/tools.svg", b"<svg/>")
    return root, wheel


def test_inventory_covers_code_assets_and_migrations(tmp_path):
    root, wheel = make_wheel(tmp_path)
    assert len(verification.package_inventory(root, wheel)) == 4
    (root / "src" / "observatory" / "new.py").write_bytes(b"new")
    with pytest.raises(verification.VerificationError, match="wheel_package_inventory_mismatch"):
        verification.package_inventory(root, wheel)


def test_inventory_refuses_stale_wheel_bytes(tmp_path):
    root, wheel = make_wheel(tmp_path, b"stale")
    with pytest.raises(verification.VerificationError, match="wheel_source_mismatch"):
        verification.package_inventory(root, wheel)


@pytest.mark.parametrize("url", [
    "postgresql://localhost/observatory", "postgresql://localhost/obs_test_production-other",
    "postgresql://remote.example/obs_test", "dbname=obs_test host=127.0.0.1 hostaddr=203.0.113.1",
    "dbname=obs_test", "invalid connection string",
])
def test_database_authority_refuses_main_remote_and_implicit_hosts(url):
    with pytest.raises(verification.VerificationError):
        verification.test_database_url({"OBS_TEST_DATABASE_URL": url})


def test_database_authority_never_falls_back_to_application_url():
    with pytest.raises(verification.VerificationError, match="missing_test_database_url"):
        verification.test_database_url({"OBS_DATABASE_URL": "postgresql://localhost/obs_test"})
    url = "postgresql://localhost/obs_test_release"
    assert verification.test_database_url({"OBS_TEST_DATABASE_URL": url}) == (url, "obs_test_release")


def test_python_network_guard_refuses_model_transport():
    with pytest.raises(verification.VerificationError, match="offline_network_connection_refused"):
        verification.deny_python_network("socket.connect", (None, ("api.openai.com", 443)))
    verification.deny_python_network("import", ())
