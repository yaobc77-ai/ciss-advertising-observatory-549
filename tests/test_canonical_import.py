import hashlib
import json
import sys

import pytest

from observatory import cli
from observatory.import_records import load_records
from observatory.migrations import discover_migrations


def write_records(path, *records):
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


def record(**changes):
    return {"record_id": "native:2026:001", "dataset": "native",
            "url": "https://example.org/ad", "published_at": "2026-01-02", **changes}


def test_import_preserves_source_and_explicit_eligibility(tmp_path):
    raw = record(body="Exact original\ntext.", provenance=[{"source": "original.csv", "row": 4}],
                 raw={"original_ad_id": "external-123"})
    path = write_records(tmp_path / "next-year.jsonl", raw)
    batch = load_records(path)
    item = batch.records[0]
    assert item.body == raw["body"]
    assert item.raw == raw["raw"]
    assert item.provenance[0] == raw["provenance"][0]
    assert item.provenance[-1]["row"] == 1
    assert item.record_id == raw["record_id"]
    assert item.published_at.isoformat() == "2026-01-02"
    assert item.retrievable is False and item.annotations == []
    assert batch.source_hashes == {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}
    assert load_records(path).model_dump() == batch.model_dump()


@pytest.mark.parametrize("changes", [
    {"record_id": " "}, {"record_id": " leading"}, {"record_id": "invalid\nID"},
    {"url": ""}, {"url": "file:///secret"}, {"url": " https://example.org/ad"},
    {"archive_url": "invalid"}, {"dataset": "social"}, {"retrievable": "true"},
    {"unexpected_source_column": "do not discard silently"},
    {"retrievable": True, "body": ""}, {"retrievable": True, "body": "video"},
    {"retrievable": True, "body": "  abc", "retrieval_ranges": [[0, 2]]},
    {"body": "abc", "retrieval_end": 10}, {"body": "abc", "retrieval_ranges": [[0, 4]]},
])
def test_invalid_rows_fail_closed(tmp_path, changes):
    path = write_records(tmp_path / "bad.jsonl", record(), record(record_id="second", **changes)
                         if "record_id" not in changes else record(**changes))
    with pytest.raises(ValueError, match="No records imported"):
        load_records(path)


def test_reject_duplicate_ids_and_wrong_dataset(tmp_path):
    path = write_records(tmp_path / "duplicate.jsonl", record(), record())
    with pytest.raises(ValueError, match="duplicate record_id"):
        load_records(path)
    path = write_records(path, record())
    with pytest.raises(ValueError, match="declared dataset"):
        load_records(path, dataset="social")


@pytest.mark.parametrize("line", [
    '{"record_id":"a","record_id":"b"}',
    '{"record_id":"a","raw":{"bad":NaN}}',
])
def test_noncanonical_json_fails_validation(tmp_path, line):
    path = tmp_path / "noncanonical.jsonl"
    path.write_text(line, encoding="utf-8")
    with pytest.raises(ValueError, match="No records imported"):
        load_records(path)


@pytest.mark.parametrize("record_id", [
    "ad/2026", "ad?source", "ad#source", "ad%2026", "ad%2F2026",
    ".", "..", "../ad", "ad\\2026", "-leading", "énergie:2026", "a" * 201,
])
def test_record_id_cannot_break_detail_or_attachment_routes(tmp_path, record_id):
    path = write_records(tmp_path / "unsafe-id.jsonl", record(record_id=record_id))
    with pytest.raises(ValueError, match="record_id must match"):
        load_records(path)


@pytest.mark.parametrize("record_id", [
    "8f5e136d-dffb-5f64-98ec-84d8ad39757c", "source:2026", "ad.2026_001-A", "a" * 200,
])
def test_route_safe_ids_are_preserved_without_rewriting(tmp_path, record_id):
    path = write_records(tmp_path / "safe-id.jsonl", record(
        record_id=record_id, raw={"original_ad_id": "source/ad?2026#1"},
    ))
    imported = load_records(path).records[0]
    assert imported.record_id == record_id
    assert imported.raw["original_ad_id"] == "source/ad?2026#1"


def test_social_mapping_is_explicit_and_spans_are_exact(tmp_path):
    path = write_records(tmp_path / "social.jsonl", record(
        dataset="social", platform="Instagram", account="Source account",
        body="Header\nPaid source text\nFooter", retrieval_ranges=[[7, 23]], retrievable=True,
    ))
    result = load_records(path, dataset="social").records[0]
    assert result.retrieval_ranges == [(7, 23)] and result.retrievable


def test_dry_run_has_no_database_connection(tmp_path, monkeypatch, capsys):
    path = write_records(tmp_path / "new.jsonl", record())
    monkeypatch.setattr(cli.Database, "connect", lambda *args: pytest.fail("dry-run opened the database"))
    monkeypatch.setattr(sys, "argv", ["observatory", "import-records", str(path), "--dry-run", "--out", ""])
    cli.main()
    assert json.loads(capsys.readouterr().out)["validated"] is True


@pytest.mark.parametrize("mode,expected", [(None, None), ("upsert", None), ("snapshot", "native")])
def test_native_snapshot_must_be_explicit(tmp_path, monkeypatch, mode, expected):
    from observatory import ingest
    from observatory.models import ImportBatch

    calls = []
    monkeypatch.setattr(ingest, "load_native", lambda *args, **kwargs: ImportBatch())
    monkeypatch.setattr(cli.Database, "import_batch", lambda self, batch, snapshot_dataset: calls.append(snapshot_dataset) or {})
    args = ["observatory", "import-native", "--out", str(tmp_path / "report.json")]
    monkeypatch.setattr(sys, "argv", args + (["--mode", mode] if mode else []))
    cli.main()
    assert calls == [expected]


def test_snapshot_requires_explicit_dataset_before_reading_file(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["observatory", "import-records", "absent.jsonl", "--mode", "snapshot"])
    with pytest.raises(SystemExit, match="2"):
        cli.main()


def test_migrations_are_ordered_and_line_endings_do_not_change_hash(tmp_path):
    path = tmp_path / "0001_example.sql"
    path.write_bytes(b"SELECT 1;\r\n")
    original = discover_migrations(tmp_path)[0].checksum
    path.write_bytes(b"SELECT 1;\n")
    assert discover_migrations(tmp_path)[0].checksum == original
    (tmp_path / "0003_gap.sql").write_text("SELECT 3;", encoding="utf-8")
    with pytest.raises(ValueError, match="consecutive"):
        discover_migrations(tmp_path)


@pytest.mark.parametrize("changes", [
    {"body": "Private source\u0000text"},
    {"raw": {"nested": [{"text": "Private\u0000explanation"}]}},
    {"raw": {"Private\u0000key": "value"}},
    {"annotations": [{"explanation": "Private\u0000value"}]},
])
def test_nul_is_rejected_without_disclosing_source_text(tmp_path, changes):
    path = write_records(tmp_path / "nul.jsonl", record(), record(record_id="second", **changes))
    with pytest.raises(ValueError, match="No records imported") as caught:
        load_records(path)
    assert "line 2" in str(caught.value)
    assert "Private" not in str(caught.value)


@pytest.mark.parametrize("where", ["record", "candidate", "rejected", "source_hashes"])
def test_mutated_batch_with_nul_fails_before_database_connection(monkeypatch, where):
    from observatory.db import Database
    from observatory.models import ImportBatch, Issue, RecordInput

    batch = ImportBatch(records=[RecordInput(**record())])
    if where == "record":
        batch.records.append(RecordInput(**record(record_id="second")))
        batch.records[-1].raw = {"nested": ["bad\u0000value"]}
    elif where == "candidate":
        batch.candidates = [{"note": "bad\u0000value"}]
    elif where == "rejected":
        batch.rejected = [Issue(code="bad", detail="bad\u0000value")]
    else:
        batch.source_hashes = {"bad\u0000key": "hash"}
    monkeypatch.setattr(Database, "connect", lambda *args: pytest.fail("Invalid batch connected to the DB"))
    with pytest.raises(ValueError, match=r"U\+0000"):
        Database("unused").import_batch(batch)
