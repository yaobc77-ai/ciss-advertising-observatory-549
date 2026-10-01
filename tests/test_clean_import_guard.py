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
OPTIONAL_INPUTS = (
    "sources/Native Advertising Data/native_ad_dataset.xlsx",
    "sources/FA25_SP26/CLAIMS 1.0 Runs/CSVS/predictions_calibrated.csv",
    "analysis/pdf_archive/source_index.json",
)


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
    for name in [*verifier.MANIFEST_HASHES, *OPTIONAL_INPUTS,
                 *[f"sources/input-{i}.txt" for i in range(4)]]:
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


@pytest.mark.parametrize("flags", [[], ["--inputs-only"]])
def test_output_exists_prevents_any_run(tmp_path, monkeypatch, flags):
    output = tmp_path / "result.json"
    output.write_text("keep", encoding="utf-8")
    work = Mock()
    offline = Mock()
    monkeypatch.setattr(verifier, "run", work)
    monkeypatch.setattr(verifier, "run_inputs_only", offline)
    assert verifier.main(["--root", str(tmp_path), "--output", str(output), *flags]) == 1
    assert output.read_text(encoding="utf-8") == "keep"
    work.assert_not_called()
    offline.assert_not_called()


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


def offline_fixture(tmp_path, monkeypatch):
    root, reference = input_fixture(tmp_path, monkeypatch)
    records = [
        SimpleNamespace(
            record_id=f"record-{i}", url=f"https://example.invalid/article/{i}",
            dataset="native", countable=i < 263, retrievable=i < 226,
            annotations=[], raw={},
        ) for i in range(275)
    ]
    records[0].annotations = [
        {"version": "claims-original", "labels": []},
        {"version": "claims-calibrated", "labels": ["green_labels.green_binary"]},
    ]
    records[1].raw = {"previous_body_annotations": [
        {"version": "claims-original", "labels": ["green_labels.green_binary"]},
        {"version": "claims-calibrated", "labels": []},
    ]}
    batch = SimpleNamespace(
        source_hashes=dict(reference["source_hashes"]), records=records,
        rejected=[], candidates=[{} for _ in range(12)],
    )
    monkeypatch.setattr(verifier, "load_native", Mock(return_value=batch))
    return root, batch


def test_inputs_only_verifies_batch_without_database_environment_or_api(tmp_path, monkeypatch):
    import dotenv
    import openai

    from observatory import config

    root, _ = offline_fixture(tmp_path, monkeypatch)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    tripwires = []
    for owner, attribute in [
        (verifier, "run"), (verifier, "TestOnlyDatabase"), (verifier, "test_database_url"),
        (config.Settings, "from_env"), (config, "load_dotenv"),
        (dotenv, "load_dotenv"), (openai, "OpenAI"),
    ]:
        blocker = Mock(side_effect=AssertionError(f"offline called {attribute}"))
        monkeypatch.setattr(owner, attribute, blocker)
        tripwires.append(blocker)
    monkeypatch.setattr(verifier, "version", Mock(return_value="9.8.7"))
    monkeypatch.chdir(tmp_path)
    output = tmp_path / "offline.json"
    assert verifier.main(["--inputs-only", "--root", str(root), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["mode"] == "inputs_only"
    assert report["snapshot_release"] == "0.2.3"
    assert report["package_version"] == "9.8.7"
    assert report["database"] is None
    assert report["test_tables_cleared"] is False
    assert report["index"] == "not_evaluated"
    assert report["expected_index_profile"] is None
    assert report["counts"] == {"stored": 275, "countable": 263, "retrievable": 226}
    assert report["unique_record_ids"] == report["unique_record_urls"] == 275
    assert report["rejected"] == 0
    assert report["additional_candidates"] == 12
    assert len(report["input_hashes"]) == 10
    annotations = report["historical_annotations"]
    assert annotations["current_entries"] == 2
    assert annotations["records_with_current_annotations"] == 1
    assert annotations["entries_by_version"] == {"claims-original": 1, "claims-calibrated": 1}
    assert annotations["nonempty_entries_by_version"] == {"claims-calibrated": 1}
    assert annotations["previous_body_records"] == 1
    assert annotations["previous_body_entries"] == 2
    assert report["development_contract"]["quote_location_validation"] == "not_evaluated"
    assert report["reference_sha256"] == hashlib.sha256(before[Path(verifier.REFERENCE)]).hexdigest()
    assert report["gold_sha256"] == hashlib.sha256(before[Path(verifier.GOLD)]).hexdigest()
    verifier.load_native.assert_called_once_with(
        root, require_admissions=True, require_body_reviews=True, require_body_recoveries=True
    )
    verifier.Database.connect.assert_not_called()
    for blocker in tripwires:
        blocker.assert_not_called()
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("name", OPTIONAL_INPUTS)
@pytest.mark.parametrize("change,code", [
    ("modified", "input_hash_mismatch"), ("missing", "missing_or_escaped_input"),
])
def test_inputs_only_rejects_changed_or_missing_optional_inputs(tmp_path, monkeypatch, name, change, code):
    root, _ = offline_fixture(tmp_path, monkeypatch)
    path = root / name
    if change == "modified":
        path.write_bytes(b"changed optional input")
    else:
        path.unlink()
    with pytest.raises(verifier.VerificationError, match=f"^{code}$"):
        verifier.run_inputs_only(root, {})
    verifier.load_native.assert_not_called()
    verifier.Database.connect.assert_not_called()


@pytest.mark.parametrize("change,code", [
    ("hashes", "loaded_input_hashes_changed"),
    ("records", "invalid_import_batch"), ("rejected", "invalid_import_batch"),
    ("countable", "wrong_input_counts"), ("retrievable", "wrong_input_counts"),
    ("record_id", "duplicate_record_ids"), ("url", "duplicate_record_urls"),
    ("dataset", "wrong_import_dataset"),
])
def test_inputs_only_rejects_loaded_batch_mismatches(tmp_path, monkeypatch, change, code):
    root, batch = offline_fixture(tmp_path, monkeypatch)
    if change == "hashes":
        batch.source_hashes[OPTIONAL_INPUTS[0]] = "changed"
    elif change == "records":
        batch.records.pop()
    elif change == "rejected":
        batch.rejected.append("rejected source row")
    elif change == "countable":
        batch.records[-1].countable = True
    elif change == "retrievable":
        batch.records[-1].retrievable = True
    elif change == "record_id":
        batch.records[-1].record_id = batch.records[0].record_id
    elif change == "url":
        batch.records[-1].url = batch.records[0].url
    else:
        batch.records[-1].dataset = "social"
    with pytest.raises(verifier.VerificationError, match=f"^{code}$"):
        verifier.run_inputs_only(root, {})
    verifier.Database.connect.assert_not_called()


def test_default_cli_retains_historical_database_route(tmp_path, monkeypatch):
    def fake_run(root, report):
        report["status"] = "passed"

    database = Mock(side_effect=fake_run)
    offline = Mock(side_effect=AssertionError("default route must remain database reproduction"))
    monkeypatch.setattr(verifier, "run", database)
    monkeypatch.setattr(verifier, "run_inputs_only", offline)
    output = tmp_path / "default.json"
    assert verifier.main(["--root", str(tmp_path), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["mode"] == "database_reproduction"
    assert report["database"] == "obs_test"
    assert report["expected_index_profile"] == verifier.LEGACY_PROFILE
    assert verifier.EXPECTED_COUNTS["chunks"] == 558
    database.assert_called_once()
    offline.assert_not_called()


@pytest.mark.parametrize("kind", [RuntimeError, verifier.VerificationError])
def test_inputs_only_failure_reports_only_allowlisted_codes(tmp_path, monkeypatch, capsys, kind):
    secret = "postgresql://account:SECRET@example.invalid/application"
    monkeypatch.setattr(verifier, "run_inputs_only", Mock(side_effect=kind(secret)))
    output = tmp_path / "failed.json"
    assert verifier.main(["--inputs-only", "--root", str(tmp_path), "--output", str(output)]) == 1
    serialized = output.read_text(encoding="utf-8")
    captured = capsys.readouterr()
    assert "SECRET" not in serialized + captured.out + captured.err
    report = json.loads(serialized)
    assert report["failure_code"] == "verification_failed"
    assert "error_type" not in report
    assert report["database"] is None
    assert report["test_tables_cleared"] is False
