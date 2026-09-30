"""Public details preserve text and only serve reviewed, unchanged local PDFs."""

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest
from flask import Flask

from observatory.records import RecordDetails, register_record_routes

PDF = b"%PDF-1.4\nReviewed source fixture\n%%EOF"
BODY = "A source paragraph.\n\nNavigation remains in the original.\f"
URL = "https://publisher.example/article"
SECRET = "private-source-path-and-disclosure"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
)


def digest(value):
    return hashlib.sha256(value).hexdigest()


class FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, parameters):
        self.calls.append((sql, parameters))
        assert "r.active" in sql and "countable" in sql and "r.current_version" in sql
        assert "SELECT" in sql and parameters
        self.current = self.rows.get(parameters[0])
        return self

    def fetchone(self):
        return self.current


@pytest.fixture
def setup(tmp_path):
    pdf_path = "sources/pdf_archive_20260915/pdfs/batch/capture.pdf"
    pdf = tmp_path / pdf_path
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(PDF)
    decision = {
        "record_id": "record-a", "url": URL,
        "source_pdf_path": pdf_path, "source_pdf_sha256": digest(PDF),
        "extracted_text_sha256": digest(BODY.encode()),
        "identity_basis": "Capture metadata and distinctive article text were reviewed.",
        "pages": [{"page": 1}], "limitations": ["Partial snapshot; image text is not transcribed."],
    }
    config = tmp_path / "config/native_body_recoveries.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"decisions": [decision]}), encoding="utf-8")
    index = tmp_path / "analysis/pdf_archive/source_index.json"
    index.parent.mkdir(parents=True)
    index.write_text(json.dumps([
        {"id": "PDF-001", "source_urls": [URL], "path": SECRET,
         "source_match": "Unique title match"},
        {"id": "PDF-002", "source_urls": ["https://publisher.example/candidate"],
         "path": SECRET, "source_match": "Unique title match"},
    ]), encoding="utf-8")
    row = {
        "record_id": "record-a", "version_id": "version-a", "dataset": "native",
        "title": "Source article", "publisher": "Publisher", "sponsor": "Company",
        "date": "2024-01-01", "keyword": "Collection term", "platform": "", "account": "",
        "retrievable": True, "body": BODY, "body_hash": digest(BODY.encode()),
        "url": URL, "archive_url": "", "retrieval_ranges": [[0, 19]], "retrieval_end": None,
        "issues": [{"code": "body_partial_recovery", "detail": SECRET, "source": SECRET}],
        "raw": {"private": SECRET}, "provenance": [{"source": SECRET}], "disclosure": SECRET,
    }
    db = FakeDB({row["record_id"]: row})
    settings = SimpleNamespace(show_source_links=True)
    return SimpleNamespace(root=tmp_path, pdf=pdf, config=config, decision=decision,
                           db=db, settings=settings, row=row)


def details(setup):
    return RecordDetails(setup.db, setup.settings, setup.root)


def test_details_preserve_body_and_do_not_expose_internal_fields(setup):
    item = details(setup).get("record-a")
    assert item["body"] == BODY
    assert item["body_hash"] == digest(BODY.encode())
    assert item["body_status"] == "partial"
    assert item["quality_notes"]
    assert SECRET not in json.dumps(item)
    assert str(setup.root) not in json.dumps(item)
    assert item["archive_url"] == ""
    assert item["archive_status"] == "Local PDF snapshot available"
    assert item["attachments"][0]["pages"] == 1
    assert item["attachments"][0]["sha256"] == digest(PDF)


def test_candidate_title_match_is_not_a_verified_attachment(setup):
    setup.row.update(record_id="record-b", url="https://publisher.example/candidate")
    setup.db.rows = {"record-b": setup.row}
    result = details(setup).get("record-b")
    assert result["candidate_snapshot_count"] == 1
    assert result["attachments"] == []
    assert result["archive_status"] == "No verified archived copy linked"


@pytest.mark.parametrize("changed", ["url", "body_hash"])
def test_changed_record_identity_disables_snapshot(setup, changed):
    setup.row[changed] = "different-value"
    assert details(setup).get("record-a")["attachments"] == []


@pytest.mark.parametrize("mode", ["missing", "changed", "non_pdf"])
def test_missing_changed_or_non_pdf_file_is_not_served(setup, mode):
    service = details(setup)
    asset_id = service.get("record-a")["attachments"][0]["asset_id"]
    if mode == "missing":
        setup.pdf.unlink()
    else:
        data = b"%PDF-1.4 altered bytes" if mode == "changed" else b"Not a PDF"
        setup.pdf.write_bytes(data)
        if mode == "non_pdf":
            service._reviewed["record-a"][0]["source_pdf_sha256"] = digest(data)
    assert service.attachment("record-a", asset_id) is None
    assert service.get("record-a")["attachments"] == []


@pytest.mark.parametrize("path_type", ["absolute", "outside_archive", "traversal"])
def test_registry_cannot_serve_outside_allowlisted_archive(setup, path_type):
    outside = setup.root / "config/outside.pdf"
    outside.write_bytes(PDF)
    paths = {
        "absolute": str(setup.pdf),
        "outside_archive": "config/outside.pdf",
        "traversal": "sources/pdf_archive_20260915/pdfs/../../../config/outside.pdf",
    }
    setup.decision["source_pdf_path"] = paths[path_type]
    setup.config.write_text(json.dumps({"decisions": [setup.decision]}), encoding="utf-8")
    assert details(setup).get("record-a")["attachments"] == []


def test_unknown_record_and_cross_record_asset_are_not_served(setup):
    service = details(setup)
    asset_id = service.get("record-a")["attachments"][0]["asset_id"]
    assert service.get("private-or-inactive") is None
    assert service.attachment("private-or-inactive", asset_id) is None
    assert service.attachment("record-a", "../../config/private") is None
    assert service.attachment("record-a", "0" * 32) is None


def test_source_link_setting_disables_all_source_links_and_pdf_route(setup):
    asset_id = details(setup).get("record-a")["attachments"][0]["asset_id"]
    setup.settings.show_source_links = False
    setup.row["archive_url"] = "https://archive.example/capture"
    service = details(setup)
    result = service.get("record-a")
    assert result["url"] == result["archive_url"] == ""
    assert result["attachments"] == []
    assert service.attachment("record-a", asset_id) is None


@pytest.mark.parametrize("unsafe", ["javascript:alert(1)", "file:///secret", "https://user:password@example.test/path"])
def test_original_and_archive_links_reject_unsafe_urls(setup, unsafe):
    setup.row.update(url=unsafe, archive_url=unsafe)
    result = details(setup).get("record-a")
    assert result["url"] == result["archive_url"] == ""


def test_summary_does_not_attach_snapshot_to_stale_browse_version(setup):
    row = {**setup.row, "version_id": "old-version"}
    summary = details(setup).summaries([row])["record-a"]
    assert summary["snapshot_count"] == 0
    assert summary["snapshot_url"] == ""


def test_coverage_distinguishes_online_local_and_unverified(setup):
    candidate = {**setup.row, "record_id": "record-b", "url": "https://publisher.example/candidate"}
    archived = {**setup.row, "record_id": "record-c", "url": "https://publisher.example/archived",
                "archive_url": "https://archive.example/capture"}
    coverage = details(setup).coverage([setup.row, candidate, archived])
    assert coverage["records"] == 3
    assert coverage["records_with_public_archive"] == 1
    assert coverage["records_with_reviewed_local_snapshot"] == 1
    assert coverage["records_with_unverified_candidates"] == 1


def test_flask_routes_verify_pdf_bytes_each_request_and_download_header(setup):
    app = Flask(__name__)
    register_record_routes(app, details(setup))
    client = app.test_client()
    response = client.get("/api/records/record-a")
    assert response.status_code == 200
    item = response.get_json()
    assert SECRET not in response.get_data(as_text=True)
    attachment = item["attachments"][0]
    inline = client.get(attachment["url"])
    assert inline.data == PDF
    assert inline.mimetype == "application/pdf"
    assert inline.headers["Content-Disposition"].startswith("inline;")
    assert inline.headers["X-Content-Type-Options"] == "nosniff"
    download = client.get(attachment["download_url"])
    assert download.headers["Content-Disposition"].startswith("attachment;")
    setup.pdf.write_bytes(b"%PDF-changed")
    assert client.get(attachment["url"]).status_code == 404
    assert client.get("/api/records/private-or-inactive").status_code == 404
    assert client.get("/records/private-or-inactive/attachments/" + attachment["asset_id"]).status_code == 404


def test_missing_manifest_and_index_keep_basic_details_available(setup):
    setup.config.unlink()
    (setup.root / "analysis/pdf_archive/source_index.json").unlink()
    item = details(setup).get("record-a")
    assert item["body"] == BODY
    assert item["attachments"] == []
    assert item["candidate_snapshot_count"] == 0


@pytest.mark.parametrize("malformed", ["{broken", "[]", '{"decisions":null}', '{"decisions":[null]}'])
def test_malformed_optional_registry_disables_links_without_losing_text(setup, malformed):
    setup.config.write_text(malformed, encoding="utf-8")
    item = details(setup).get("record-a")
    assert item["body"] == BODY
    assert item["attachments"] == []


def cached_preview(setup):
    target = setup.root / "sources/recovered_native/previews"
    target.mkdir(parents=True)
    image = target / f"{digest(PDF)}.page1.png"
    image.write_bytes(PNG)
    receipt = target / f"{digest(PDF)}.page1.json"
    receipt.write_text(json.dumps({
        "schema_version": 1, "source_pdf_sha256": digest(PDF),
        "image_sha256": digest(PNG), "page": 1,
    }), encoding="utf-8")
    return image, receipt


def test_preview_absent_keeps_original_pdf_available(setup):
    service = details(setup)
    item = service.get("record-a")["attachments"][0]
    assert item["preview_url"] is None
    assert service.attachment("record-a", item["asset_id"])[0] == PDF
    assert service.preview("record-a", item["asset_id"]) is None


def test_hash_checked_preview_serves_png_without_executing_renderer(setup, monkeypatch):
    import subprocess

    def forbid_process(*args, **kwargs):
        pytest.fail("Public requests must not execute a renderer")

    monkeypatch.setattr(subprocess, "run", forbid_process)
    cached_preview(setup)
    app = Flask(__name__)
    register_record_routes(app, details(setup))
    client = app.test_client()
    item = client.get("/api/records/record-a").get_json()["attachments"][0]
    assert item["preview_page"] == 1
    result = client.get(item["preview_url"])
    assert result.status_code == 200
    assert result.mimetype == "image/png"
    assert result.data == PNG
    assert result.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("change", ["image_bytes", "receipt_pdf_hash", "receipt_page", "receipt_missing", "source_pdf"])
def test_preview_changed_or_unmatched_fails_closed(setup, change):
    image, receipt = cached_preview(setup)
    service = details(setup)
    item = service.get("record-a")["attachments"][0]
    if change == "image_bytes":
        image.write_bytes(PNG + b"tampered")
    elif change == "receipt_missing":
        receipt.unlink()
    elif change == "source_pdf":
        setup.pdf.write_bytes(b"%PDF-changed")
    else:
        value = json.loads(receipt.read_text(encoding="utf-8"))
        value["source_pdf_sha256" if change == "receipt_pdf_hash" else "page"] = "wrong"
        receipt.write_text(json.dumps(value), encoding="utf-8")
    assert service.preview("record-a", item["asset_id"]) is None
    current = service.get("record-a")["attachments"]
    if current:
        assert current[0]["preview_url"] is None


@pytest.mark.parametrize("change", ["disabled", "inactive", "body", "url"])
def test_preview_requires_enabled_links_and_current_record_identity(setup, change):
    cached_preview(setup)
    service = details(setup)
    item = service.get("record-a")["attachments"][0]
    if change == "disabled":
        setup.settings.show_source_links = False
    elif change == "inactive":
        setup.db.rows.clear()
    else:
        setup.row["body_hash" if change == "body" else "url"] = "changed"
    assert service.preview("record-a", item["asset_id"]) is None
