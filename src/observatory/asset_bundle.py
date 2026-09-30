"""Build and validate explicitly selected, reviewed record attachments.

Bundles contain display assets only. They cannot approve source identities,
change article text, or turn title-matched archive candidates into evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

MANIFEST_NAME = "record_assets.json"
PDF_MAGIC = b"%PDF-"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_HASH = re.compile(r"[0-9a-f]{64}")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verified_bytes(root: Path, relative: str, sha256: str, magic: bytes, directory: str):
    """Read only a relative, hash-matched asset inside the approved directory."""
    if not isinstance(relative, str) or not isinstance(sha256, str) or not _HASH.fullmatch(sha256):
        raise ValueError("Asset path or hash is invalid")
    path_parts = PurePosixPath(relative)
    if (
        path_parts.is_absolute() or "\\" in relative or ":" in relative
        or ".." in path_parts.parts or not path_parts.parts
    ):
        raise ValueError("Asset path must be relative and contained")
    root = root.resolve(strict=True)
    allowed = (root / directory).resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    if not allowed.is_relative_to(root) or not path.is_relative_to(allowed) or not path.is_file():
        raise ValueError("Asset path is outside its approved directory")
    data = path.read_bytes()
    if not data.startswith(magic) or digest(data) != sha256:
        raise ValueError("Asset bytes do not match their declared type and hash")
    return data


def _identity(item):
    required = ("record_id", "url", "body_sha256", "pdf_sha256", "identity_basis")
    if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k] for k in required):
        raise ValueError("Each asset needs a reviewed record, URL, body and PDF identity")
    if not _HASH.fullmatch(item["body_sha256"]) or not _HASH.fullmatch(item["pdf_sha256"]):
        raise ValueError("Body and PDF hashes must be SHA-256")
    try:
        url = urlsplit(item["url"])
        valid_url = (
            url.scheme in {"http", "https"} and url.hostname
            and not url.username and not url.password
            and not any(c.isspace() for c in item["url"])
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("Reviewed URL must be an HTTP(S) source URL")
    if type(item.get("page_count")) is not int or item["page_count"] < 1:
        raise ValueError("Reviewed page count must be a positive integer")
    if not isinstance(item.get("limitations"), list) or any(not isinstance(v, str) for v in item["limitations"]):
        raise ValueError("Reviewed limitations must be a list of strings")


def load_bundle(root: Path, expected_manifest_sha256: str) -> list[dict]:
    """Validate the whole curated bundle before exposing any attachment."""
    if not isinstance(expected_manifest_sha256, str) or not _HASH.fullmatch(expected_manifest_sha256):
        raise ValueError("Configure the expected record-asset manifest SHA-256")
    root = Path(root).resolve(strict=True)
    manifest_path = (root / MANIFEST_NAME).resolve(strict=True)
    if not manifest_path.is_relative_to(root) or not manifest_path.is_file():
        raise ValueError("Record-asset manifest must stay inside its root")
    data = manifest_path.read_bytes()
    if digest(data) != expected_manifest_sha256:
        raise ValueError("Record-asset manifest hash does not match")
    value = json.loads(data.decode("utf-8"))
    if (
        not isinstance(value, dict) or value.get("schema_version") != 1
        or value.get("kind") != "observatory_record_assets"
        or not isinstance(value.get("assets"), list) or not value["assets"]
    ):
        raise ValueError("Record-asset bundle schema is invalid")
    reviewed, seen = [], set()
    for item in value["assets"]:
        _identity(item)
        key = (item["record_id"], item["pdf_sha256"])
        if key in seen:
            raise ValueError("Duplicate record/PDF asset in bundle")
        seen.add(key)
        pdf_path = f"pdfs/{item['pdf_sha256']}.pdf"
        if item.get("pdf_path") != pdf_path:
            raise ValueError("Bundle PDFs must use their hash as the file name")
        verified_bytes(root, pdf_path, item["pdf_sha256"], PDF_MAGIC, "pdfs")
        preview_path, preview_hash = item.get("preview_path"), item.get("preview_sha256")
        if preview_path is not None or preview_hash is not None:
            if preview_path != f"previews/{item['pdf_sha256']}.page1.png":
                raise ValueError("Bundle preview path is invalid")
            verified_bytes(root, preview_path, preview_hash, PNG_MAGIC, "previews")
        reviewed.append({
            "record_id": item["record_id"], "url": item["url"],
            "source_pdf_path": pdf_path, "source_pdf_sha256": item["pdf_sha256"],
            "extracted_text_sha256": item["body_sha256"], "identity_basis": item["identity_basis"],
            "pages": [{"page": n} for n in range(1, item["page_count"] + 1)],
            "limitations": item["limitations"],
            "preview_path": preview_path, "preview_sha256": preview_hash,
        })
    return reviewed


def build_bundle(source_root: Path, destination: Path, record_ids: list[str]) -> dict:
    """Copy only explicitly selected, already reviewed local captures.

    A fresh destination is required. Source files and database records are never
    modified. The output is a private maintenance artifact, not a publication.
    """
    source_root = Path(source_root).resolve(strict=True)
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Use a new bundle destination; existing bundles are not overwritten")
    if not record_ids or any(not isinstance(v, str) or not v for v in record_ids) or len(set(record_ids)) != len(record_ids):
        raise ValueError("Explicit, nonduplicate reviewed record IDs are required")
    manifest = json.loads((source_root / "config/native_body_recoveries.json").read_text("utf-8"))
    decisions = manifest.get("decisions") if isinstance(manifest, dict) else None
    if not isinstance(decisions, list):
        raise ValueError("Reviewed local recovery manifest is invalid")
    selected = [item for item in decisions if isinstance(item, dict) and item.get("record_id") in record_ids]
    if {item["record_id"] for item in selected} != set(record_ids):
        raise ValueError("A requested record has no reviewed local capture")
    assets, files = [], {}
    for item in selected:
        pages = item.get("pages")
        if not isinstance(pages, list) or not pages:
            raise ValueError("Reviewed page metadata is required")
        asset = {
            "record_id": item.get("record_id"), "url": item.get("url"),
            "body_sha256": item.get("extracted_text_sha256"),
            "pdf_sha256": item.get("source_pdf_sha256"), "identity_basis": item.get("identity_basis"),
            "page_count": len(pages), "limitations": item.get("limitations", []),
        }
        _identity(asset)
        pdf = verified_bytes(source_root, item.get("source_pdf_path"), asset["pdf_sha256"], PDF_MAGIC,
                             "sources/pdf_archive_20260915/pdfs")
        # Bind the selected capture to the exact extraction reviewed for this body.
        verified_bytes(source_root, item.get("extracted_text_path"), asset["body_sha256"], b"",
                       "sources/recovered_native")
        asset["pdf_path"] = f"pdfs/{asset['pdf_sha256']}.pdf"
        files[asset["pdf_path"]] = pdf
        preview_root = source_root / "sources/recovered_native/previews"
        image_path = preview_root / f"{asset['pdf_sha256']}.page1.png"
        receipt_path = preview_root / f"{asset['pdf_sha256']}.page1.json"
        if image_path.exists() or receipt_path.exists():
            checked_preview_root = preview_root.resolve(strict=True)
            if not checked_preview_root.is_relative_to(source_root) or not receipt_path.resolve(strict=True).is_relative_to(checked_preview_root):
                raise ValueError("Cached preview receipt is outside its approved directory")
            receipt = json.loads(receipt_path.read_text("utf-8"))
            if not isinstance(receipt, dict) or receipt.get("schema_version") != 1 or receipt.get("source_pdf_sha256") != asset["pdf_sha256"] or receipt.get("page") != 1:
                raise ValueError("Cached preview receipt does not match the reviewed capture")
            preview = verified_bytes(source_root, image_path.relative_to(source_root).as_posix(),
                                     receipt.get("image_sha256"), PNG_MAGIC, "sources/recovered_native/previews")
            asset["preview_path"] = f"previews/{asset['pdf_sha256']}.page1.png"
            asset["preview_sha256"] = receipt["image_sha256"]
            files[asset["preview_path"]] = preview
        assets.append(asset)
    content = (json.dumps({"schema_version": 1, "kind": "observatory_record_assets", "assets": assets},
                          ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="record-assets-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "bundle"
        staging.mkdir()
        for name, data in files.items():
            path = staging / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(data)
        (staging / MANIFEST_NAME).write_bytes(content)
        load_bundle(staging, digest(content))
        staging.rename(destination)
    return {"manifest_sha256": digest(content), "records": len(set(record_ids)), "assets": len(assets),
            "previews": sum("preview_path" in a for a in assets), "published": False}
