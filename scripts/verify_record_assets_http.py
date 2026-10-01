"""Read-only HTTP checks against an explicitly pinned local record-asset bundle.

This verifies selected public responses, not the remote mount, UI, or persistence.
No settings, credentials, database client, or model provider is loaded.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from observatory.asset_bundle import PDF_MAGIC, PNG_MAGIC, digest, load_bundle

JSON_LIMIT = 8 * 1024 * 1024
ASSET_LIMIT = 64 * 1024 * 1024
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class FetchFailure(Exception):
    """A deliberately sanitized transport error."""

    def __init__(self, code):
        self.code = code


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_http(url, timeout, max_bytes):
    """One bounded GET; redirects and response text never become diagnostics."""
    try:
        opener = build_opener(NoRedirect())
        try:
            response = opener.open(Request(url, headers={"Accept-Encoding": "identity"}), timeout=timeout)
        except HTTPError as exc:
            response = exc
        with response:
            if response.geturl() != url:
                raise FetchFailure("redirect_refused")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise FetchFailure("response_too_large")
            return HttpResponse(response.code, {k.lower(): v for k, v in response.headers.items()}, data)
    except (OSError, URLError, ValueError, HTTPException):
        raise FetchFailure("request_failed") from None


def normalize_origin(value):
    """Accept HTTPS origins, or HTTP localhost/loopback origins, without extras."""
    if not isinstance(value, str) or re.search(r"[\s\\]", value):
        raise ValueError("invalid origin")
    parsed = urlsplit(value)
    host = parsed.hostname
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
            or parsed.password is not None or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment or "?" in value or "#" in value
            or not re.fullmatch(r"[A-Za-z0-9.:-]+", host)
            or not re.fullmatch(r"(?:[A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(?::[0-9]+)?", parsed.netloc)):
        raise ValueError("invalid origin")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("invalid port")
    if parsed.scheme == "http" and host.lower() != "localhost":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("HTTP requires loopback")
    return value.rstrip("/")


def _inputs(root, manifest_sha256, base_url, expected_status, other_record_id, timeout):
    origin = normalize_origin(base_url)
    if expected_status not in {"bundle_verified", "local_workspace"}:
        raise ValueError("invalid expected status")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
        raise ValueError("invalid timeout")
    assets = load_bundle(Path(root), manifest_sha256)
    if any(not ID_PATTERN.fullmatch(item["record_id"]) for item in assets):
        raise ValueError("invalid record ID")
    if other_record_id is not None and not ID_PATTERN.fullmatch(other_record_id):
        raise ValueError("invalid other record ID")
    identities = {}
    for asset in assets:
        identity = (asset["url"], asset["extracted_text_sha256"])
        if identities.setdefault(asset["record_id"], identity) != identity:
            raise ValueError("contradictory record identity")
        for name in ("source_pdf_path", "preview_path"):
            if asset.get(name):
                size = (Path(root) / asset[name]).stat().st_size
                if size > ASSET_LIMIT:
                    raise ValueError("asset exceeds request limit")
                asset[name + "_size"] = size
    return origin, assets


def _json_object(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite JSON number")
        return number

    value = json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_float=finite,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))
    if not isinstance(value, dict):
        raise ValueError("record JSON must be an object")
    return value


def verify_record_assets(root, manifest_sha256, base_url, *, expected_status="bundle_verified",
                         other_record_id=None, timeout=15, fetch=None):
    """Return sanitized evidence; transport injection is for offline tests only."""
    origin, assets = _inputs(root, manifest_sha256, base_url, expected_status, other_record_id, timeout)
    fetch = fetch or fetch_http
    pinned_asset_hashes = {asset[key] for asset in assets for key in (
        "source_pdf_sha256", "preview_sha256") if asset.get(key)}
    report = {
        "schema_version": 1, "origin": origin, "expected_local_manifest_sha256": manifest_sha256,
        "expected_record_asset_status": expected_status,
        "requested_other_record_id": other_record_id, "expected_assets": len(assets),
        "checked_at": datetime.now(timezone.utc).isoformat(), "request_method": "GET",
        "direct_database_connections": 0, "model_calls": 0,
        "remote_manifest_hash": "not_observable_via_http",
        "whole_remote_bundle_equivalence": "not_verified", "ui_verification": "not_verified",
        "new_container_persistence": "not_verified", "records": [],
    }

    def check(item, name, passed):
        item["checks"][name] = bool(passed)
        if not passed:
            item["failure_codes"].append(name)

    def get(item, name, path, limit):
        try:
            response = fetch(origin + path, timeout, limit)
            if len(response.body) > limit:
                raise FetchFailure("response_too_large")
            return response
        except FetchFailure as exc:
            check(item, name, False)
            item["failure_codes"].append(exc.code if exc.code in {
                "redirect_refused", "response_too_large", "request_failed",
            } else "request_failed")
        except (OSError, ValueError, URLError, HTTPException):
            check(item, name, False)
            item["failure_codes"].append("request_failed")
        return None

    record_ids = sorted({asset["record_id"] for asset in assets})
    available = {}
    for record_id in record_ids + ([other_record_id] if other_record_id not in record_ids and other_record_id else []):
        item = {"record_id": record_id, "checks": {}, "failure_codes": []}
        response = get(item, "record_request", f"/api/records/{record_id}", JSON_LIMIT)
        value = None
        if response:
            check(item, "record_http_200", response.status == 200)
            check(item, "record_json_type", response.headers.get("content-type", "").split(";")[0] == "application/json")
            if response.status == 200:
                try:
                    value = _json_object(response.body)
                    check(item, "record_id_exact", value.get("record_id") == record_id)
                except (ValueError, UnicodeError, RecursionError):
                    check(item, "record_json_valid", False)
        available[record_id] = (item, value)

    for record_id in record_ids:
        item, value = available[record_id]
        record_assets = [asset for asset in assets if asset["record_id"] == record_id]
        item["expected_body_sha256"] = record_assets[0]["extracted_text_sha256"]
        item["assets"] = []
        report["records"].append(item)
        if value is None:
            continue
        body = value.get("body")
        try:
            body_hash = digest(body.encode("utf-8")) if isinstance(body, str) else None
        except UnicodeError:
            body_hash = None
        item["observed_body_sha256"] = body_hash
        check(item, "body_hash_matches_api", body_hash is not None and body_hash == value.get("body_hash"))
        check(item, "body_hash_matches_manifest", body_hash == item["expected_body_sha256"])
        check(item, "source_url_exact", value.get("url") == record_assets[0]["url"])
        check(item, "asset_status_exact", value.get("record_asset_status") == expected_status)
        attachments = value.get("attachments")
        check(item, "attachments_list", isinstance(attachments, list))
        attachments = attachments if isinstance(attachments, list) else []
        other_id = next((candidate for candidate in [other_record_id, *record_ids]
                         if candidate and candidate != record_id), None)
        other_valid = bool(other_id and available[other_id][1]
                           and not available[other_id][0]["failure_codes"])
        item["cross_record_isolation"] = "not_verified"
        item["other_record_id"] = other_id
        if other_id:
            check(item, "other_record_available", other_valid)
        for asset in record_assets:
            pdf_hash = asset["source_pdf_sha256"]
            asset_id = digest(f"{record_id}:{pdf_hash}".encode())[:32]
            path = f"/records/{record_id}/attachments/{asset_id}"
            result = {"asset_id": asset_id, "expected_pdf_sha256": pdf_hash,
                      "checks": {}, "failure_codes": [],
                      "preview": "expected" if asset["preview_path"] else "not_present_in_expected_bundle"}
            item["assets"].append(result)
            matches = [a for a in attachments or [] if isinstance(a, dict) and a.get("sha256") == pdf_hash]
            check(result, "unique_attachment", len(matches) == 1)
            match = matches[0] if len(matches) == 1 else {}
            check(result, "canonical_attachment_paths", match.get("asset_id") == asset_id
                  and match.get("kind") == "pdf" and match.get("url") == path
                  and match.get("download_url") == path + "?download=1"
                  and match.get("preview_url") == (path + "/preview.png" if asset["preview_path"] else None))
            pdf = get(result, "pdf_request", path, ASSET_LIMIT)
            download = get(result, "download_request", path + "?download=1", ASSET_LIMIT)
            for name, response in (("pdf", pdf), ("download", download)):
                if response:
                    result["observed_" + name + "_sha256"] = digest(response.body)
                    check(result, name + "_http_200", response.status == 200)
                    check(result, name + "_type", response.headers.get("content-type", "").split(";")[0] == "application/pdf")
                    check(result, name + "_bytes", response.body.startswith(PDF_MAGIC)
                          and digest(response.body) == pdf_hash
                          and len(response.body) == asset["source_pdf_path_size"])
            if download:
                check(result, "download_disposition", download.headers.get("content-disposition", "").split(";")[0].strip().lower() == "attachment")
                check(result, "download_same_bytes", pdf is not None and download.body == pdf.body)
            if asset["preview_path"]:
                result["expected_preview_sha256"] = asset["preview_sha256"]
                preview = get(result, "preview_request", path + "/preview.png", ASSET_LIMIT)
                if preview:
                    result["observed_preview_sha256"] = digest(preview.body)
                    check(result, "preview_http_200", preview.status == 200)
                    check(result, "preview_type", preview.headers.get("content-type", "").split(";")[0] == "image/png")
                    check(result, "preview_bytes", preview.body.startswith(PNG_MAGIC)
                          and digest(preview.body) == asset["preview_sha256"]
                          and len(preview.body) == asset["preview_path_size"])
            unknown_record = "asset-verification-unknown"
            while unknown_record in available:
                unknown_record += "-x"
            unknown_asset = "0" * 32 if asset_id != "0" * 32 else "1" * 32
            probes = {"unknown_record": f"/records/{unknown_record}/attachments/{asset_id}",
                      "unknown_asset": f"/records/{record_id}/attachments/{unknown_asset}"}
            if other_valid:
                probes["cross_record"] = f"/records/{other_id}/attachments/{asset_id}"
            for name, probe in probes.items():
                for suffix, ending in (("pdf", ""), ("preview", "/preview.png")):
                    response = get(result, name + "_" + suffix + "_request", probe + ending, ASSET_LIMIT)
                    if response:
                        check(result, name + "_" + suffix + "_404", response.status == 404)
                        check(result, name + "_" + suffix + "_no_asset_bytes",
                              not response.body.startswith((PDF_MAGIC, PNG_MAGIC))
                              and digest(response.body) not in pinned_asset_hashes)
        if other_valid:
            item["cross_record_isolation"] = "verified" if all(
                a["checks"].get("cross_record_pdf_404") and a["checks"].get("cross_record_preview_404")
                and a["checks"].get("cross_record_pdf_no_asset_bytes")
                and a["checks"].get("cross_record_preview_no_asset_bytes")
                for a in item["assets"]) else "failed"
    failed = any(r["failure_codes"] or any(a["failure_codes"] for a in r["assets"]) for r in report["records"])
    report["status"] = "failed" if failed else "passed_for_selected_http_assets"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-status", choices=["bundle_verified", "local_workspace"], default="bundle_verified")
    parser.add_argument("--other-record-id")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.out.exists() or not args.out.parent.is_dir():
            raise ValueError("output must be a new path")
        origin, assets = _inputs(args.root, args.manifest_sha256, args.base_url,
                                 args.expected_status, args.other_record_id, args.timeout)
        with args.out.open("x", encoding="utf-8", newline="\n") as output:
            try:
                report = verify_record_assets(args.root, args.manifest_sha256, args.base_url,
                                              expected_status=args.expected_status,
                                              other_record_id=args.other_record_id, timeout=args.timeout)
                serialized = json.dumps(report, indent=2) + "\n"
                exit_code = int(report["status"] == "failed")
            except Exception:
                # Input is already validated and output reserved. A late race or
                # protocol error is a failed check, never an invalid-input exit.
                report = {
                    "schema_version": 1, "status": "failed", "failure_codes": ["verification_failed"],
                    "origin": origin, "expected_local_manifest_sha256": args.manifest_sha256,
                    "expected_record_asset_status": args.expected_status, "expected_assets": len(assets),
                    "checked_at": datetime.now(timezone.utc).isoformat(), "request_method": "GET",
                    "direct_database_connections": 0, "model_calls": 0, "records": [],
                    "remote_manifest_hash": "not_observable_via_http",
                    "whole_remote_bundle_equivalence": "not_verified", "ui_verification": "not_verified",
                    "new_container_persistence": "not_verified",
                }
                serialized = json.dumps(report, indent=2) + "\n"
                exit_code = 1
            output.write(serialized)
        print(json.dumps({"status": report["status"]}))
        return exit_code
    except (OSError, ValueError):
        print(json.dumps({"status": "invalid_input", "failure_code": "invalid_input"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
