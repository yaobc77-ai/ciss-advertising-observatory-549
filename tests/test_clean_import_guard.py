"""Offline guard tests: no real database connection, schema change or API call."""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_clean_import.py"
spec = importlib.util.spec_from_file_location("clean_import_verifier", SCRIPT)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


@pytest.fixture(autouse=True)
def no_real_connection(monkeypatch):
    monkeypatch.setattr(verifier.Database, "connect", Mock(side_effect=AssertionError("No real DB in guard tests")))


@pytest.mark.parametrize("url", [
    "dbname=observatory", "dbname=obs_test_extra", "host=localhost",
    "postgresql://example.invalid/observatory", "invalid connection syntax",
])
def test_rejects_nonexact_or_invalid_database_without_connecting(url):
    with pytest.raises(verifier.VerificationError):
        verifier.TestOnlyDatabase(url)
    verifier.Database.connect.assert_not_called()


def test_missing_test_url_never_uses_application_url():
    with pytest.raises(verifier.VerificationError, match="missing_test_database_url"):
        verifier.test_database_url({"OBS_DATABASE_URL": "dbname=obs_test"})


def test_exact_database_url_is_accepted_without_connecting():
    url = "dbname=obs_test host=example.invalid password=do-not-print"
    assert verifier.test_database_url({"OBS_TEST_DATABASE_URL": url}) == url
    verifier.Database.connect.assert_not_called()


@pytest.mark.parametrize("actual", ["observatory", "obs_test_extra"])
def test_connected_database_mismatch_closes_before_mutation(monkeypatch, actual):
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = {"name": actual}
    monkeypatch.setattr(verifier.Database, "connect", Mock(return_value=conn))
    db = verifier.TestOnlyDatabase("dbname=obs_test")
    with pytest.raises(verifier.VerificationError, match="connected_database_refused"):
        db.connect()
    conn.execute.assert_called_once_with("SELECT current_database() AS name")
    conn.close.assert_called_once()


def test_each_connection_is_checked(monkeypatch):
    connections = [Mock(), Mock()]
    for conn in connections:
        conn.execute.return_value.fetchone.return_value = {"name": "obs_test"}
    monkeypatch.setattr(verifier.Database, "connect", Mock(side_effect=connections))
    db = verifier.TestOnlyDatabase("dbname=obs_test")
    assert db.connect() is connections[0]
    assert db.connect() is connections[1]
    for conn in connections:
        conn.execute.assert_called_once_with("SELECT current_database() AS name")
        conn.commit.assert_called_once_with()


def input_fixture(tmp_path, monkeypatch):
    root = tmp_path / "unpacked"
    root.mkdir()
    hashes = {}
    for name in [*verifier.MANIFEST_HASHES, *[f"sources/input-{i}.txt" for i in range(7)]]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(verifier, "MANIFEST_HASHES", {name: hashes[name] for name in verifier.MANIFEST_HASHES})
    reference = {"after": {"data_version": verifier.EXPECTED_VERSION,
                           "record_counts": {"native": 275}, "chunks": 558},
                 "counts": {"countable": 263, "retrievable": 226}, "source_hashes": hashes}
    reference_path = root / verifier.REFERENCE
    reference_path.parent.mkdir(parents=True)
    reference_path.write_text(json.dumps(reference), encoding="utf-8")
    gold = root / verifier.GOLD
    gold.parent.mkdir(parents=True)
    gold.write_text("synthetic gold file", encoding="utf-8")
    cases = [SimpleNamespace(suite="development", status="ready", support_quote=list(range(15)))]
    monkeypatch.setattr(verifier, "load_cases", Mock(return_value=cases))
    return root, reference


def test_explicit_root_is_independent_of_cwd(tmp_path, monkeypatch):
    root, reference = input_fixture(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    checked, hashes, _ = verifier.verify_inputs(root)
    assert checked == root
    assert hashes == reference["source_hashes"]
    verifier.load_cases.assert_called_once_with(root / verifier.GOLD)


def test_modified_source_is_rejected_before_database(tmp_path, monkeypatch):
    root, _ = input_fixture(tmp_path, monkeypatch)
    (root / "sources/input-0.txt").write_bytes(b"changed")
    with pytest.raises(verifier.VerificationError, match="input_hash_mismatch"):
        verifier.run(root, {})
    verifier.Database.connect.assert_not_called()


def test_manifest_and_reference_cannot_be_changed_together(tmp_path, monkeypatch):
    root, reference = input_fixture(tmp_path, monkeypatch)
    name = next(iter(verifier.MANIFEST_HASHES))
    (root / name).write_bytes(b"changed manifest")
    reference["source_hashes"][name] = hashlib.sha256(b"changed manifest").hexdigest()
    (root / verifier.REFERENCE).write_text(json.dumps(reference), encoding="utf-8")
    with pytest.raises(verifier.VerificationError, match="unexpected_manifest_hash"):
        verifier.verify_inputs(root)


@pytest.mark.parametrize("relative", ["../outside.txt", "/outside.txt"])
def test_input_path_cannot_escape_root(tmp_path, relative):
    with pytest.raises(verifier.VerificationError):
        verifier.project_file(tmp_path, relative)


def test_output_exists_prevents_any_run(tmp_path, monkeypatch):
    output = tmp_path / "result.json"
    output.write_text("keep", encoding="utf-8")
    work = Mock()
    monkeypatch.setattr(verifier, "run", work)
    assert verifier.main(["--root", str(tmp_path), "--output", str(output)]) == 1
    assert output.read_text(encoding="utf-8") == "keep"
    work.assert_not_called()


def test_exception_report_and_console_omit_connection_details(tmp_path, monkeypatch, capsys):
    secret = "postgresql://account:SECRET@example.invalid/obs_test"
    monkeypatch.setattr(verifier, "run", Mock(side_effect=RuntimeError(secret)))
    output = tmp_path / "result.json"
    assert verifier.main(["--root", str(tmp_path), "--output", str(output)]) == 1
    serialized = output.read_text(encoding="utf-8")
    assert secret not in serialized
    assert "SECRET" not in capsys.readouterr().out
    assert json.loads(serialized)["failure_code"] == "verification_failed"


def test_repeat_reloads_all_required_manifests_without_api(tmp_path, monkeypatch):
    hashes = {"source": "hash"}
    monkeypatch.setattr(verifier, "verify_inputs", Mock(return_value=(tmp_path, hashes, [])))
    gold = tmp_path / verifier.GOLD
    gold.parent.mkdir()
    gold.write_bytes(b"gold")
    batch = SimpleNamespace(source_hashes=hashes, rejected=[], records=[None] * 275)
    loader = Mock(return_value=batch)
    monkeypatch.setattr(verifier, "load_native", loader)
    db = Mock()
    db.connect.return_value.__enter__ = Mock(return_value=Mock())
    db.connect.return_value.__exit__ = Mock(return_value=False)
    db.import_batch.side_effect = [
        {"new_versions": 275, "unchanged": 0, "deactivated": []},
        {"new_versions": 0, "unchanged": 275, "deactivated": []},
    ]
    monkeypatch.setattr(verifier, "TestOnlyDatabase", Mock(return_value=db))
    monkeypatch.setattr(verifier, "test_database_url", Mock(return_value="dbname=obs_test"))
    monkeypatch.setattr(verifier, "verify_snapshot", Mock(return_value={"valid": True}))
    report = {}
    verifier.run(tmp_path, report)
    assert report["status"] == "passed"
    assert loader.call_count == 2
    for call in loader.call_args_list:
        assert call.args == (tmp_path,)
        assert call.kwargs == {"require_admissions": True, "require_body_reviews": True, "require_body_recoveries": True}
    assert db.import_batch.call_count == 2
    assert db.initialize.call_count == 2
    cleared = db.connect.return_value.__enter__.return_value.execute.call_args.args[0]
    for table in ("retrieval_state", "retrieval_profiles", "retrieval_preparations",
                  "retrieval_publications", "chunk_profile_membership"):
        assert table in cleared


@pytest.mark.parametrize("source_version,profile,code", [
    ("changed", verifier.LEGACY_PROFILE, "wrong_source_data_version"),
    (None, verifier.LEGACY_PROFILE, "wrong_source_data_version"),
    (verifier.EXPECTED_VERSION, "sentence600-v1", "wrong_index_profile"),
])
def test_snapshot_requires_historical_source_and_legacy_profile(source_version, profile, code):
    db = Mock()
    db.health.return_value = {
        "record_counts": {"native": 275}, "source_data_version": source_version,
        "active_profile": profile, "data_version": verifier.EXPECTED_VERSION,
    }
    with pytest.raises(verifier.VerificationError, match=code):
        verifier.verify_snapshot(db, [])
    db.public_rows.assert_not_called()


def test_snapshot_uses_source_hash_instead_of_new_combined_version(monkeypatch):
    db = Mock()
    db.health.return_value = {
        "record_counts": {"native": 275}, "source_data_version": verifier.EXPECTED_VERSION,
        "active_profile": verifier.LEGACY_PROFILE, "data_version": "new-combined-version",
        "index_version": "legacy-index-version", "chunks": 558,
    }
    db.public_rows.return_value = [{"retrievable": i < 226} for i in range(263)]
    monkeypatch.setattr(verifier, "verify_chunk_locators", Mock(return_value=558))
    monkeypatch.setattr(verifier, "load_snapshot", Mock(return_value={}))
    monkeypatch.setattr(verifier, "validate_gold", Mock(return_value={"case": list(range(15))}))
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = {
        "usage_entries": 0, "embeddings": 0, "answers": 0, "generations": 0,
    }
    db.connect.return_value.__enter__ = Mock(return_value=conn)
    db.connect.return_value.__exit__ = Mock(return_value=False)
    result = verifier.verify_snapshot(db, [])
    assert result["health"]["data_version"] == "new-combined-version"
    assert result["health"]["source_data_version"] == verifier.EXPECTED_VERSION


def test_locator_rejects_text_spanning_excluded_gap(monkeypatch):
    body = "alpha NAV omega"
    row = {"body": body, "text": body, "start_char": 0, "end_char": len(body),
           "payload": {"retrieval_ranges": [[0, 5], [10, 15]]},
           "version_id": "version", "text_hash": verifier.digest(body),
           "chunk_id": verifier.digest(f"version:0:{len(body)}")}
    db = Mock()
    conn = Mock()
    conn.execute.return_value.fetchall.return_value = [row]
    db.connect.return_value.__enter__ = Mock(return_value=conn)
    db.connect.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(verifier, "EXPECTED_COUNTS", {"chunks": 1})
    with pytest.raises(verifier.VerificationError, match="chunk_crosses_excluded_gap"):
        verifier.verify_chunk_locators(db)
