"""Synthetic local bundles and stub transports; no network or database calls."""

import importlib.util
import json
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

from observatory.asset_bundle import MANIFEST_NAME, PDF_MAGIC, PNG_MAGIC, digest

SCRIPT = Path(__file__).parents[1] / "scripts/verify_record_assets_http.py"
SPEC = importlib.util.spec_from_file_location("asset_http_verifier", SCRIPT)
verifier = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verifier
SPEC.loader.exec_module(verifier)

BODY = "Synthetic reviewed article: café and 🌍.\nSecond paragraph."
SOURCE_URL = "https://publisher.example/original-private-source"
PDF = PDF_MAGIC + b"1.4\nSynthetic capture\n%%EOF"
PNG = PNG_MAGIC + b"Synthetic first page"
ORIGIN = "https://observatory.example"


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "private-bundle-root"
    (root / "pdfs").mkdir(parents=True)
    (root / "previews").mkdir()
    pdf_hash, png_hash = digest(PDF), digest(PNG)
    (root / "pdfs" / f"{pdf_hash}.pdf").write_bytes(PDF)
    (root / "previews" / f"{pdf_hash}.page1.png").write_bytes(PNG)
    manifest = {"schema_version": 1, "kind": "observatory_record_assets", "assets": [{
        "record_id": "record-a", "url": SOURCE_URL, "body_sha256": digest(BODY.encode()),
        "pdf_sha256": pdf_hash, "pdf_path": f"pdfs/{pdf_hash}.pdf", "page_count": 1,
        "identity_basis": "Synthetic reviewed fixture", "limitations": [],
        "preview_path": f"previews/{pdf_hash}.page1.png", "preview_sha256": png_hash,
    }]}
    data = json.dumps(manifest).encode()
    (root / MANIFEST_NAME).write_bytes(data)
    return {"root": root, "sha": digest(data), "manifest": manifest}


def rewrite_bundle(bundle):
    data = json.dumps(bundle["manifest"]).encode()
    (bundle["root"] / MANIFEST_NAME).write_bytes(data)
    bundle["sha"] = digest(data)


class Stub:
    def __init__(self, bundle, *, other=True):
        self.calls = []
        self.responses = {}
        self.asset_id = digest(f"record-a:{digest(PDF)}".encode())[:32]
        self.path = f"/records/record-a/attachments/{self.asset_id}"
        self.record = {"record_id": "record-a", "body": BODY, "body_hash": digest(BODY.encode()),
                       "url": SOURCE_URL, "record_asset_status": "bundle_verified", "attachments": [{
                           "asset_id": self.asset_id, "kind": "pdf", "sha256": digest(PDF),
                           "url": self.path, "download_url": self.path + "?download=1",
                           "preview_url": self.path + "/preview.png",
                       }]}
        self.set_record(self.record)
        if other:
            self.set_record({"record_id": "record-b"})
        self.set(self.path, body=PDF, mime="application/pdf")
        self.set(self.path + "?download=1", body=PDF, mime="application/pdf",
                 headers={"content-disposition": 'attachment; filename="fixture.pdf"'})
        self.set(self.path + "/preview.png", body=PNG, mime="image/png")

    def set_record(self, record):
        self.set("/api/records/" + record["record_id"], body=json.dumps(record).encode(), mime="application/json")

    def set(self, path, *, body=b"", mime="text/plain", status=200, headers=None):
        self.responses[ORIGIN + path] = verifier.HttpResponse(status, {"content-type": mime, **(headers or {})}, body)

    def __call__(self, url, timeout, max_bytes):
        self.calls.append((url, timeout, max_bytes))
        response = self.responses.get(url, verifier.HttpResponse(404, {"content-type": "text/html"}, b"missing"))
        if isinstance(response, Exception):
            raise response
        return response


def run(bundle, stub, **kwargs):
    return verifier.verify_record_assets(bundle["root"], bundle["sha"], ORIGIN, fetch=stub, **kwargs)


def failures(report):
    return {code for record in report["records"] for item in [record, *record["assets"]]
            for code in item["failure_codes"]}


def test_good_http_assets_strong_cross_record_and_sanitized_report(bundle):
    stub = Stub(bundle)
    result = run(bundle, stub, other_record_id="record-b")
    assert result["status"] == "passed_for_selected_http_assets"
    assert result["records"][0]["cross_record_isolation"] == "verified"
    assert result["records"][0]["observed_body_sha256"] == digest(BODY.encode())
    assert not failures(result)
    serialized = json.dumps(result)
    assert BODY not in serialized and SOURCE_URL not in serialized and str(bundle["root"]) not in serialized
    assert result["expected_local_manifest_sha256"] == bundle["sha"]
    assert result["remote_manifest_hash"] == "not_observable_via_http"
    assert result["whole_remote_bundle_equivalence"] == "not_verified"
    assert result["ui_verification"] == result["new_container_persistence"] == "not_verified"
    assert result["direct_database_connections"] == result["model_calls"] == 0
    assert result["request_method"] == "GET"
    assert all(url.startswith(ORIGIN + "/") and timeout == 15 and limit <= verifier.ASSET_LIMIT
               for url, timeout, limit in stub.calls)


def test_no_other_record_does_not_claim_cross_record_proof(bundle):
    result = run(bundle, Stub(bundle, other=False))
    assert result["status"] == "passed_for_selected_http_assets"
    assert result["records"][0]["cross_record_isolation"] == "not_verified"
    assert result["records"][0]["assets"][0]["checks"]["unknown_record_pdf_404"]


@pytest.mark.parametrize("field,value,code", [
    ("body", "Tampered article", "body_hash_matches_api"),
    ("body_hash", "0" * 64, "body_hash_matches_api"),
    ("record_id", "record-spoof", "record_id_exact"),
    ("url", "https://other.example/", "source_url_exact"),
    ("record_asset_status", "bundle_unavailable", "asset_status_exact"),
    ("attachments", [], "unique_attachment"),
    ("attachments", 1, "attachments_list"),
])
def test_record_identity_status_and_attachment_failures(bundle, field, value, code):
    stub = Stub(bundle)
    stub.record[field] = value
    stub.set("/api/records/record-a", body=json.dumps(stub.record).encode(), mime="application/json")
    result = run(bundle, stub)
    assert result["status"] == "failed" and code in failures(result)
    if field == "body":
        assert "body_hash_matches_manifest" in failures(result)


def test_api_hash_spoof_cannot_replace_manifest_identity(bundle):
    stub = Stub(bundle)
    stub.record.update(body="Replacement article", body_hash=digest(b"Replacement article"))
    stub.set_record(stub.record)
    result = run(bundle, stub)
    assert result["records"][0]["checks"]["body_hash_matches_api"]
    assert "body_hash_matches_manifest" in failures(result)


def test_duplicate_attachment_is_not_a_unique_reviewed_capture(bundle):
    stub = Stub(bundle)
    stub.record["attachments"] *= 2
    stub.set_record(stub.record)
    assert "unique_attachment" in failures(run(bundle, stub))


@pytest.mark.parametrize("field,value", [
    ("url", "https://attacker.example/secret"),
    ("url", "/records/record-b/attachments/" + "0" * 32),
    ("download_url", "/records/record-a/attachments/" + "0" * 32 + "?download=1"),
    ("preview_url", "//attacker.example/preview.png"),
    ("asset_id", "../secret"),
])
def test_returned_urls_cannot_control_requests(bundle, field, value):
    stub = Stub(bundle)
    stub.record["attachments"][0][field] = value
    stub.set_record(stub.record)
    assert "canonical_attachment_paths" in failures(run(bundle, stub))
    assert all("attacker" not in url and "secret" not in url for url, _, _ in stub.calls)


@pytest.mark.parametrize("endpoint,body,mime,status,code", [
    ("", PDF[:-1] + b"X", "application/pdf", 200, "pdf_bytes"),
    ("", b"<html>not PDF</html>", "text/html", 200, "pdf_type"),
    ("", PDF, "application/pdf", 302, "pdf_http_200"),
    ("?download=1", PDF + b"different", "application/pdf", 200, "download_same_bytes"),
    ("/preview.png", PNG[:-1] + b"X", "image/png", 200, "preview_bytes"),
    ("/preview.png", PNG, "image/png", 404, "preview_http_200"),
])
def test_asset_bytes_type_and_status_failures(bundle, endpoint, body, mime, status, code):
    stub = Stub(bundle)
    stub.set(stub.path + endpoint, body=body, mime=mime, status=status)
    assert code in failures(run(bundle, stub))


def test_download_requires_attachment_disposition(bundle):
    stub = Stub(bundle)
    stub.set(stub.path + "?download=1", body=PDF, mime="application/pdf")
    assert "download_disposition" in failures(run(bundle, stub))


@pytest.mark.parametrize("probe,code", [
    ("/records/asset-verification-unknown/attachments/{asset}", "unknown_record_pdf_404"),
    ("/records/asset-verification-unknown/attachments/{asset}/preview.png", "unknown_record_preview_404"),
    ("/records/record-a/attachments/" + "0" * 32, "unknown_asset_pdf_404"),
    ("/records/record-a/attachments/" + "0" * 32 + "/preview.png", "unknown_asset_preview_404"),
    ("/records/record-b/attachments/{asset}", "cross_record_pdf_404"),
    ("/records/record-b/attachments/{asset}/preview.png", "cross_record_preview_404"),
])
def test_binding_negative_routes_must_return_404(bundle, probe, code):
    stub = Stub(bundle)
    stub.set(probe.format(asset=stub.asset_id), status=200, body=PDF)
    assert code in failures(run(bundle, stub, other_record_id="record-b"))


@pytest.mark.parametrize("probe,body,code", [
    ("/records/record-b/attachments/{asset}", PDF, "cross_record_pdf_no_asset_bytes"),
    ("/records/record-b/attachments/{asset}/preview.png", PNG, "cross_record_preview_no_asset_bytes"),
    ("/records/asset-verification-unknown/attachments/{asset}", PDF, "unknown_record_pdf_no_asset_bytes"),
    ("/records/record-a/attachments/" + "0" * 32 + "/preview.png", PNG, "unknown_asset_preview_no_asset_bytes"),
])
def test_negative_404_must_not_leak_pinned_asset_bytes(bundle, probe, body, code):
    stub = Stub(bundle)
    stub.set(probe.format(asset=stub.asset_id), status=404, body=body)
    result = run(bundle, stub, other_record_id="record-b")
    assert result["status"] == "failed" and code in failures(result)
    if code.startswith("cross_record"):
        assert result["records"][0]["cross_record_isolation"] == "failed"


def test_other_record_must_exist_and_have_exact_id(bundle):
    stub = Stub(bundle, other=False)
    result = run(bundle, stub, other_record_id="record-b")
    assert "other_record_available" in failures(result)
    assert result["records"][0]["cross_record_isolation"] == "not_verified"
    stub.set("/api/records/record-b", body=b'{"record_id":"record-spoof"}', mime="application/json")
    assert "other_record_available" in failures(run(bundle, stub, other_record_id="record-b"))


def test_optional_preview_and_local_workspace_status(bundle):
    for key in ("preview_path", "preview_sha256"):
        bundle["manifest"]["assets"][0].pop(key)
    rewrite_bundle(bundle)
    stub = Stub(bundle)
    stub.record["record_asset_status"] = "local_workspace"
    stub.record["attachments"][0]["preview_url"] = None
    stub.set_record(stub.record)
    result = run(bundle, stub, expected_status="local_workspace")
    assert result["status"] == "passed_for_selected_http_assets"
    assert result["records"][0]["assets"][0]["preview"] == "not_present_in_expected_bundle"
    assert (ORIGIN + stub.path + "/preview.png") not in {url for url, _, _ in stub.calls}


def test_shared_pdf_has_distinct_opaque_ids_and_automatic_cross_record_probes(bundle):
    second_asset = dict(bundle["manifest"]["assets"][0], record_id="record-b")
    bundle["manifest"]["assets"].append(second_asset)
    rewrite_bundle(bundle)
    stub = Stub(bundle)
    second_id = digest(f"record-b:{digest(PDF)}".encode())[:32]
    second_path = f"/records/record-b/attachments/{second_id}"
    second_record = dict(stub.record, record_id="record-b", attachments=[{
        "asset_id": second_id, "kind": "pdf", "sha256": digest(PDF), "url": second_path,
        "download_url": second_path + "?download=1", "preview_url": second_path + "/preview.png",
    }])
    stub.set_record(second_record)
    stub.set(second_path, body=PDF, mime="application/pdf")
    stub.set(second_path + "?download=1", body=PDF, mime="application/pdf",
             headers={"content-disposition": "attachment"})
    stub.set(second_path + "/preview.png", body=PNG, mime="image/png")
    result = run(bundle, stub)
    assert result["status"] == "passed_for_selected_http_assets"
    assert result["expected_assets"] == 2
    assert len(result["records"]) == 2
    assert all(r["cross_record_isolation"] == "verified" for r in result["records"])
    ids = {r["assets"][0]["asset_id"] for r in result["records"]}
    assert len(ids) == 2
    assert (ORIGIN + f"/records/record-b/attachments/{stub.asset_id}") in {url for url, _, _ in stub.calls}


def test_response_size_failure_is_reported(bundle, monkeypatch):
    monkeypatch.setattr(verifier, "JSON_LIMIT", 100)
    result = run(bundle, Stub(bundle))
    assert result["status"] == "failed" and "response_too_large" in failures(result)


@pytest.mark.parametrize("payload,mime", [
    (b"<html>error</html>", "text/html"),
    (b'[]', "application/json"),
    (b'{"record_id":"record-a","record_id":"record-a"}', "application/json"),
    (b'{"value":1e999}', "application/json"),
    (b'{"value":NaN}', "application/json"),
    (b'\xff', "application/json"),
])
def test_invalid_record_json_is_a_reported_failure(bundle, payload, mime):
    stub = Stub(bundle)
    stub.set("/api/records/record-a", body=payload, mime=mime)
    assert run(bundle, stub)["status"] == "failed"


@pytest.mark.parametrize("origin", [
    "https://user:password@example.org", "https://example.org/path", "https://example.org/?key=secret",
    "https://example.org/#secret", "http://example.org", "file:///tmp/secret", "https://example.org\\evil",
    "https://example.org:99999", "https://example.org/\n", "https://example.org?", "https://example.org#",
    "https://example.org:", "https://[::1]unexpected", "https://example.org:0",
])
def test_unsafe_origins_rejected_before_get(bundle, origin):
    stub = Stub(bundle)
    with pytest.raises(ValueError):
        verifier.verify_record_assets(bundle["root"], bundle["sha"], origin, fetch=stub)
    assert not stub.calls


@pytest.mark.parametrize("origin", ["https://example.org", "https://example.org/", "http://127.0.0.1:8050",
                                    "http://localhost:8050", "http://[::1]:8050"])
def test_valid_origins(origin):
    assert verifier.normalize_origin(origin) == origin.rstrip("/")


@pytest.mark.parametrize("mutation", ["manifest_hash", "pdf", "unsafe_record", "contradictory_record"])
def test_bundle_preflight_failure_prevents_http(bundle, mutation):
    if mutation == "manifest_hash":
        bundle["sha"] = "0" * 64
    elif mutation == "pdf":
        (bundle["root"] / bundle["manifest"]["assets"][0]["pdf_path"]).write_bytes(b"broken")
    elif mutation == "unsafe_record":
        bundle["manifest"]["assets"][0]["record_id"] = "../secret"
        rewrite_bundle(bundle)
    else:
        second = dict(bundle["manifest"]["assets"][0])
        second["url"] = "https://different.example"
        second["pdf_sha256"] = digest(PDF + b"second")
        second["pdf_path"] = f"pdfs/{second['pdf_sha256']}.pdf"
        second.pop("preview_path")
        second.pop("preview_sha256")
        (bundle["root"] / second["pdf_path"]).write_bytes(PDF + b"second")
        bundle["manifest"]["assets"].append(second)
        rewrite_bundle(bundle)
    stub = Stub(bundle)
    with pytest.raises(ValueError):
        run(bundle, stub)
    assert not stub.calls


def test_network_failures_are_sanitized_and_partial_evidence_survives(bundle):
    stub = Stub(bundle)
    stub.responses[ORIGIN + stub.path] = OSError("secret token and C:/private/path")
    result = run(bundle, stub)
    assert result["status"] == "failed" and "request_failed" in failures(result)
    assert result["records"][0]["checks"]["body_hash_matches_manifest"]
    assert result["records"][0]["assets"][0]["checks"]["preview_bytes"]
    assert "secret" not in json.dumps(result) and "private/path" not in json.dumps(result)


def cli_args(bundle, out):
    return ["--root", str(bundle["root"]), "--manifest-sha256", bundle["sha"],
            "--base-url", ORIGIN, "--out", str(out)]


def test_cli_existing_output_prevents_get(bundle, tmp_path, monkeypatch, capsys):
    stub = Stub(bundle)
    monkeypatch.setattr(verifier, "fetch_http", stub)
    out = tmp_path / "existing.json"
    out.write_text("preserve")
    assert verifier.main(cli_args(bundle, out)) == 2
    assert out.read_text() == "preserve" and not stub.calls
    assert json.loads(capsys.readouterr().out)["status"] == "invalid_input"


def test_cli_failed_checks_write_report_and_exit_one(bundle, tmp_path, monkeypatch):
    stub = Stub(bundle)
    stub.set(stub.path, body=b"corrupt", mime="application/pdf")
    monkeypatch.setattr(verifier, "fetch_http", stub)
    out = tmp_path / "report.json"
    assert verifier.main(cli_args(bundle, out)) == 1
    assert json.loads(out.read_text())["status"] == "failed"


def test_cli_pass_exit_zero_and_invalid_manifest_exit_two(bundle, tmp_path, monkeypatch):
    stub = Stub(bundle)
    monkeypatch.setattr(verifier, "fetch_http", stub)
    assert verifier.main(cli_args(bundle, tmp_path / "pass.json")) == 0
    stub.calls.clear()
    bundle["sha"] = "0" * 64
    out = tmp_path / "invalid.json"
    assert verifier.main(cli_args(bundle, out)) == 2
    assert not stub.calls and not out.exists()


@pytest.mark.parametrize("error", [RuntimeError("secret runtime credential"), TypeError("private body"),
                                   ValueError("late validation race")])
def test_cli_late_exception_writes_sanitized_failed_receipt(bundle, tmp_path, monkeypatch, capsys, error):
    stub = Stub(bundle)
    original = verifier.verify_record_assets

    def failing_verification(*args, **kwargs):
        assert out.exists()  # The output path was reserved before any GET.
        stub(ORIGIN + "/api/records/record-a", 15, verifier.JSON_LIMIT)
        raise error

    out = tmp_path / "failure-receipt.json"
    monkeypatch.setattr(verifier, "verify_record_assets", failing_verification)
    assert verifier.main(cli_args(bundle, out)) == 1
    assert stub.calls
    receipt = json.loads(out.read_text())
    assert receipt["status"] == "failed" and receipt["failure_codes"] == ["verification_failed"]
    assert receipt["expected_local_manifest_sha256"] == bundle["sha"]
    assert receipt["new_container_persistence"] == receipt["ui_verification"] == "not_verified"
    serialized = out.read_text() + capsys.readouterr().out
    assert str(error) not in serialized and "Traceback" not in serialized
    assert "invalid_input" not in serialized
    monkeypatch.setattr(verifier, "verify_record_assets", original)


def test_cli_unexpected_transport_protocol_error_is_failed_receipt(bundle, tmp_path, monkeypatch):
    calls = []

    def broken_response(url, timeout, max_bytes):
        calls.append(url)
        return object()  # A malformed transport response, not an input error.

    monkeypatch.setattr(verifier, "fetch_http", broken_response)
    out = tmp_path / "protocol-error.json"
    assert verifier.main(cli_args(bundle, out)) == 1
    assert calls and json.loads(out.read_text())["failure_codes"] == ["verification_failed"]


class WireResponse:
    def __init__(self, url, data, code=200, headers=None):
        self.url, self.data, self.code = url, data, code
        self.headers = headers or {"Content-Type": "application/pdf"}
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def geturl(self):
        return self.url

    def read(self, size):
        self.read_sizes.append(size)
        return self.data[:size]


def test_transport_bounded_read_and_redirect_refusal(monkeypatch):
    url = ORIGIN + "/expected"
    response = WireResponse(url, b"oversized")

    class Opener:
        def open(self, request, timeout):
            assert request.get_header("Accept-encoding") == "identity"
            assert timeout == 3
            return response

    def opener_factory(handler):
        assert isinstance(handler, verifier.NoRedirect)
        return Opener()

    monkeypatch.setattr(verifier, "build_opener", opener_factory)
    with pytest.raises(verifier.FetchFailure) as exc:
        verifier.fetch_http(url, 3, 4)
    assert exc.value.code == "response_too_large" and response.read_sizes == [5]
    response.url = "https://attacker.example"
    with pytest.raises(verifier.FetchFailure) as exc:
        verifier.fetch_http(url, 3, 20)
    assert exc.value.code == "redirect_refused"
    assert verifier.NoRedirect().redirect_request(None, None, 302, "redirect", {}, response.url) is None


def test_transport_http_error_stays_response_without_following(monkeypatch):
    from io import BytesIO

    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 302, "redirect", {"Location": "https://attacker.example"}, BytesIO(b""))

    monkeypatch.setattr(verifier, "build_opener", lambda _: Opener())
    result = verifier.fetch_http(ORIGIN + "/expected", 1, 100)
    assert result.status == 302
