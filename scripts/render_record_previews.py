"""Render cached first-page PNG previews for reviewed PDFs, outside web requests.

Run from the project: python scripts/render_record_previews.py
Requires Poppler's pdftoppm on PATH, or pass --pdftoppm /path/to/pdftoppm.
Only PDFs already bound by config/native_body_recoveries.json are processed.
Original PDFs, extracted bodies, retrieval indexes and database rows are untouched.
"""

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def render_previews(root, pdftoppm="pdftoppm"):
    root = Path(root).resolve(strict=True)
    archive_root = (root / "sources/pdf_archive_20260915/pdfs").resolve(strict=True)
    output = (root / "sources/recovered_native/previews").resolve()
    if not archive_root.is_relative_to(root) or not output.is_relative_to(root):
        raise ValueError("Preview and archive roots must stay inside the project")
    executable = shutil.which(str(pdftoppm))
    if executable is None:
        raise RuntimeError("pdftoppm is unavailable; install Poppler or pass --pdftoppm")
    manifest = json.loads((root / "config/native_body_recoveries.json").read_text("utf-8"))
    version = subprocess.run([executable, "-v"], capture_output=True, text=True, check=True, timeout=15)
    renderer_version = (version.stderr or version.stdout).splitlines()[0]
    output.mkdir(parents=True, exist_ok=True)
    results = []
    seen = set()
    for item in manifest["decisions"]:
        source = (root / item["source_pdf_path"]).resolve(strict=True)
        if Path(item["source_pdf_path"]).is_absolute() or not source.is_relative_to(archive_root):
            raise ValueError("Reviewed PDF path must be relative and stay in the archive")
        pdf_bytes = source.read_bytes()
        pdf_hash = _hash(pdf_bytes)
        if not pdf_bytes.startswith(b"%PDF-") or pdf_hash != item["source_pdf_sha256"]:
            raise ValueError("Reviewed PDF hash changed; review it before generating a preview")
        if pdf_hash in seen:
            continue
        seen.add(pdf_hash)
        image_path = output / f"{pdf_hash}.page1.png"
        receipt_path = output / f"{pdf_hash}.page1.json"
        # All temporary paths are created beneath the already checked output
        # directory; cleanup cannot remove a source or an unrelated directory.
        with tempfile.TemporaryDirectory(prefix="render-", dir=output) as temp:
            temp_root = Path(temp).resolve()
            if not temp_root.is_relative_to(output):
                raise ValueError("Temporary render path escaped the preview directory")
            pinned_pdf = temp_root / "source.pdf"
            pinned_pdf.write_bytes(pdf_bytes)
            prefix = temp_root / "page1"
            subprocess.run(
                [executable, "-f", "1", "-l", "1", "-singlefile", "-scale-to", "1400",
                 "-png", str(pinned_pdf), str(prefix)],
                check=True, capture_output=True, timeout=60,
            )
            png_path = prefix.with_suffix(".png")
            image = png_path.read_bytes()
            if not image.startswith(b"\x89PNG\r\n\x1a\n") or image[12:16] != b"IHDR":
                raise ValueError("Renderer did not produce a PNG")
            width, height = struct.unpack(">II", image[16:24])
            if not width or not height or max(width, height) > 1400:
                raise ValueError("Preview dimensions exceed the configured limit")
            if _hash(source.read_bytes()) != pdf_hash:
                raise ValueError("Source PDF changed during rendering; no preview published")
            receipt = {
                "schema_version": 1, "source_pdf_sha256": pdf_hash,
                "image_sha256": _hash(image), "page": 1,
                "width": width, "height": height,
                "renderer": renderer_version, "scale_to": 1400,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "note": "Display-only first-page derivative of the reviewed PDF, not a replacement source or complete article.",
            }
            temporary_receipt = temp_root / "receipt.json"
            temporary_receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
            png_path.replace(image_path)
            temporary_receipt.replace(receipt_path)
        results.append({**receipt, "image": image_path.relative_to(root).as_posix(),
                        "receipt": receipt_path.relative_to(root).as_posix()})
    return {"rendered": len(results), "previews": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--pdftoppm", default="pdftoppm")
    args = parser.parse_args()
    print(json.dumps(render_previews(args.root, args.pdftoppm), indent=2))


if __name__ == "__main__":
    main()
