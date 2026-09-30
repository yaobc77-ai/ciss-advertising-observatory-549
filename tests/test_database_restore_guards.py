"""Protection of source databases and earlier evidence in the restore operator."""

from pathlib import Path

import pytest


@pytest.fixture
def verifier(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import verify_database_restore

    return verify_database_restore


@pytest.mark.parametrize("name", ["observatory", "postgres", "obs_test", "observatory_restore_", "observatory_restore_x;drop database observatory"])
def test_restore_rejects_protected_or_malformed_target(verifier, name):
    with pytest.raises(ValueError, match="new database"):
        verifier.validate_target(name)


def test_existing_evidence_is_retained_before_database_access(verifier, tmp_path, monkeypatch):
    report = tmp_path / "receipt.json"
    report.write_text("earlier evidence", encoding="utf-8")
    monkeypatch.setattr(verifier.pg, "verify_server", lambda: pytest.fail("Database was accessed"))
    with pytest.raises(ValueError, match="previous results"):
        verifier.run("observatory_restore_new", report)
    assert report.read_text() == "earlier evidence"


def test_missing_output_directory_rejected_before_database_access(verifier, tmp_path, monkeypatch):
    monkeypatch.setattr(verifier.pg, "verify_server", lambda: pytest.fail("Database was accessed"))
    with pytest.raises(ValueError, match="directory must already exist"):
        verifier.run("observatory_restore_new", tmp_path / "missing" / "receipt.json")


def test_backup_rejects_snapshot_text_without_creating_archive(verifier, tmp_path, monkeypatch):
    monkeypatch.setattr(verifier.pg, "RUNTIME", tmp_path)
    monkeypatch.setattr(verifier.pg, "verify_server", lambda: None)
    monkeypatch.setattr(verifier.pg, "run", lambda *a, **kw: pytest.fail("pg_dump was called"))
    with pytest.raises(ValueError, match="snapshot identifier"):
        verifier.pg.backup(snapshot="--dbname=observatory")
    assert list((tmp_path / "backups").iterdir()) == []
