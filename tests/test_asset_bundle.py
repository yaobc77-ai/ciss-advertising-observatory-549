"""Curated bundles cannot serve changed files or promote candidate captures."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

from observatory.asset_bundle import (
    MANIFEST_NAME,
    build_bundle,
    digest,
    load_bundle,
    verified_bytes,
)
from observatory.records import RecordDetails, register_record_routes

PDF = b"%PDF-1.4\nSynthetic reviewed capture\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\nSynthetic first-page fixture"
BODY = "Captured text, unchanged.\f"
URL = "https://publisher.example/reviewed"


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    pdf_path = "sources/pdf_archive_20260915/pdfs/batch/source.pdf"
    text_path = "sources/recovered_native/body.txt"
    for name, data in ((pdf_path, PDF), (text_path, BODY.encode())):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    decision = {
        "record_id": "record-a", "url": URL,
        "source_pdf_path": pdf_path, "source_pdf_sha256": digest(PDF),
        "extracted_text_path": text_path, "extracted_text_sha256": digest(BODY.encode()),
        "identity_basis": "Synthetic reviewed identity for engineering tests only.",
        "pages": [{"page": 1}], "limitations": ["Incomplete synthetic fixture."],
    }
    manifest = root / "config/native_body_recoveries.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"decisions": [decision]}), encoding="utf-8")
    return SimpleNamespace(root=root, decision=decision, manifest=manifest,
                           destination=tmp_path / "private-bundle")


def add_preview(source):
    root = source.root / "sources/recovered_native/previews"
    root.mkdir()
    (root / f"{digest(PDF)}.page1.png").write_bytes(PNG)
    (root / f"{digest(PDF)}.page1.json").write_text(json.dumps({
        "schema_version": 1, "source_pdf_sha256": digest(PDF), "image_sha256": digest(PNG), "page": 1,
    }), encoding="utf-8")


def bundle(source, preview=False):
    if preview:
        add_preview(source)
    return build_bundle(source.root, source.destination, ["record-a"])


def service(source, result):
    settings = SimpleNamespace(show_source_links=True, record_asset_root=str(source.destination),
                               record_asset_manifest_sha256=result["manifest_sha256"])
    details = RecordDetails(None, settings)
    row = {"record_id": "record-a", "version_id": "version-a", "dataset": "native",
           "body": BODY, "body_hash": digest(BODY.encode()), "url": URL, "archive_url": "", "issues": []}
    details._row = lambda record_id: row if record_id == "record-a" else None
    return details, row


def mutate(source, update):
    path = source.destination / MANIFEST_NAME
    value = json.loads(path.read_text("utf-8"))
    update(value)
    data = (json.dumps(value) + "\n").encode()
    path.write_bytes(data)
    return digest(data)


def test_builder_selects_reviewed_record_and_keeps_sources_unchanged(source):
    original_manifest = source.manifest.read_bytes()
    result = bundle(source, preview=True)
    assets = load_bundle(source.destination, result["manifest_sha256"])
    assert result == {"manifest_sha256": digest((source.destination / MANIFEST_NAME).read_bytes()),
                      "records": 1, "assets": 1, "previews": 1, "published": False}
    assert assets[0]["record_id"] == "record-a"
    assert assets[0]["extracted_text_sha256"] == digest(BODY.encode())
    assert (source.destination / f"pdfs/{digest(PDF)}.pdf").read_bytes() == PDF
    assert (source.destination / f"previews/{digest(PDF)}.page1.png").read_bytes() == PNG
    assert (source.root / source.decision["source_pdf_path"]).read_bytes() == PDF
    assert source.manifest.read_bytes() == original_manifest
    assert not (source.destination / "config").exists()
    assert not (source.destination / "sources").exists()
    assert not (source.destination / "body.txt").exists()
    assert str(source.root) not in (source.destination / MANIFEST_NAME).read_text("utf-8")


def test_bundle_routes_work_without_the_source_workspace(source):
    result = bundle(source, preview=True)
    (source.root / source.decision["source_pdf_path"]).unlink()
    (source.root / source.decision["extracted_text_path"]).unlink()
    details, _ = service(source, result)
    app = Flask(__name__)
    register_record_routes(app, details)
    client = app.test_client()
    record = client.get("/api/records/record-a").get_json()
    assert record["record_asset_status"] == "bundle_verified"
    assert record["archive_url"] == ""
    assert record["body"] == BODY
    asset = record["attachments"][0]
    assert client.get(asset["url"]).data == PDF
    assert client.get(asset["download_url"]).headers["Content-Disposition"].startswith("attachment;")
    assert client.get(asset["preview_url"]).data == PNG
    assert client.get("/records/another-record/attachments/" + asset["asset_id"]).status_code == 404


@pytest.mark.parametrize("hash_value", ["", "wrong", "0" * 64])
def test_configured_hash_failure_preserves_text_and_has_no_legacy_fallback(source, hash_value):
    result = bundle(source)
    result["manifest_sha256"] = hash_value
    details, _ = service(source, result)
    item = details.get("record-a")
    assert item["body"] == BODY
    assert item["attachments"] == []
    assert item["record_asset_status"] == "bundle_unavailable"


@pytest.mark.parametrize("field", ["url", "body_hash"])
def test_bundle_cannot_attach_to_a_changed_record_binding(source, field):
    details, row = service(source, bundle(source))
    row[field] = "changed-record-binding"
    assert details.get("record-a")["attachments"] == []


def test_source_switch_also_disables_curated_bundle_routes(source):
    details, _ = service(source, bundle(source, preview=True))
    asset = details.get("record-a")["attachments"][0]
    details.settings.show_source_links = False
    assert details.get("record-a")["attachments"] == []
    assert details.attachment("record-a", asset["asset_id"]) is None
    assert details.preview("record-a", asset["asset_id"]) is None


def test_request_rechecks_pdf_after_successful_startup(source):
    details, _ = service(source, bundle(source, preview=True))
    asset = details.get("record-a")["attachments"][0]
    (source.destination / f"pdfs/{digest(PDF)}.pdf").write_bytes(PDF + b"changed")
    assert details.attachment("record-a", asset["asset_id"]) is None
    assert details.preview("record-a", asset["asset_id"]) is None
    assert details.get("record-a")["attachments"] == []


def test_request_rechecks_preview_without_losing_the_pdf(source):
    details, _ = service(source, bundle(source, preview=True))
    asset = details.get("record-a")["attachments"][0]
    (source.destination / f"previews/{digest(PDF)}.page1.png").write_bytes(PNG + b"changed")
    assert details.preview("record-a", asset["asset_id"]) is None
    assert details.attachment("record-a", asset["asset_id"])[0] == PDF
    assert details.get("record-a")["attachments"][0]["preview_url"] is None


@pytest.mark.parametrize("change", ["missing_pdf", "changed_pdf", "changed_preview", "manifest_changed"])
def test_entire_bundle_must_validate_at_startup(source, change):
    result = bundle(source, preview=True)
    if change == "missing_pdf":
        (source.destination / f"pdfs/{digest(PDF)}.pdf").unlink()
    elif change == "changed_pdf":
        (source.destination / f"pdfs/{digest(PDF)}.pdf").write_bytes(PDF + b"changed")
    elif change == "changed_preview":
        (source.destination / f"previews/{digest(PDF)}.page1.png").write_bytes(PNG + b"changed")
    else:
        (source.destination / MANIFEST_NAME).write_text("{}", encoding="utf-8")
    details, _ = service(source, result)
    assert details.asset_status == "bundle_unavailable"
    assert details.get("record-a")["attachments"] == []


@pytest.mark.parametrize("bad_path", ["../outside.pdf", "/absolute.pdf", "C:\\private\\capture.pdf", "pdfs/../outside.pdf", "pdfs/wrong.pdf"])
def test_bundle_rejects_unsafe_or_unpinned_file_names(source, bad_path):
    bundle(source)
    new_hash = mutate(source, lambda value: value["assets"][0].update(pdf_path=bad_path))
    with pytest.raises(ValueError):
        load_bundle(source.destination, new_hash)


@pytest.mark.parametrize("update", [
    lambda v: v.update(schema_version=2),
    lambda v: v.update(kind="candidate_title_matches"),
    lambda v: v.update(assets=[]),
    lambda v: v["assets"].append(v["assets"][0].copy()),
    lambda v: v["assets"][0].update(body_sha256="bad"),
    lambda v: v["assets"][0].update(page_count=True),
    lambda v: v["assets"][0].update(url="https://user:password@example.test/source"),
])
def test_bundle_schema_and_identity_are_checked_before_exposure(source, update):
    bundle(source)
    with pytest.raises(ValueError):
        load_bundle(source.destination, mutate(source, update))


@pytest.mark.parametrize("record_ids", [[], ["record-a", "record-a"], ["unreviewed-candidate"]])
def test_builder_requires_explicit_reviewed_ids_and_never_promotes_candidates(source, record_ids):
    with pytest.raises(ValueError):
        build_bundle(source.root, source.destination, record_ids)
    assert not source.destination.exists()


def test_builder_never_overwrites_existing_output(source):
    bundle(source)
    old = (source.destination / MANIFEST_NAME).read_bytes()
    with pytest.raises(ValueError):
        build_bundle(source.root, source.destination, ["record-a"])
    assert (source.destination / MANIFEST_NAME).read_bytes() == old


def test_failed_extraction_binding_creates_no_output(source):
    (source.root / source.decision["extracted_text_path"]).write_text("different body", encoding="utf-8")
    with pytest.raises(ValueError):
        bundle(source)
    assert not source.destination.exists()


def test_builder_refuses_incomplete_or_changed_cached_preview(source):
    add_preview(source)
    (source.root / "sources/recovered_native/previews" / f"{digest(PDF)}.page1.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        bundle(source)
    assert not source.destination.exists()


def test_symlink_cannot_escape_approved_bundle_directory(source, tmp_path):
    result = bundle(source)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(PDF)
    path = source.destination / f"pdfs/{digest(PDF)}.pdf"
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this test host")
    with pytest.raises(ValueError):
        load_bundle(source.destination, result["manifest_sha256"])


def test_independent_file_validation_rejects_cross_directory_paths(source):
    result = bundle(source, preview=True)
    assert load_bundle(source.destination, result["manifest_sha256"])
    with pytest.raises(ValueError):
        verified_bytes(Path(source.destination), f"pdfs/{digest(PDF)}.pdf", digest(PDF), b"%PDF-", "previews")
