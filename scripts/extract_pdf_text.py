"""Extract an immutable page-indexed text artifact with pypdf; no body selection."""

import argparse
import hashlib
import json
from pathlib import Path


def main():
    import pypdf

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    pdf_hash = hashlib.sha256(args.pdf.read_bytes()).hexdigest()
    if pdf_hash != args.expected_sha256:
        raise ValueError("PDF hash changed; review the source before extracting")
    pages, parts, offset = [], [], 0
    for number, page in enumerate(pypdf.PdfReader(args.pdf).pages, 1):
        # Keep the extractor's text exactly, including layout line breaks. A form
        # feed belongs to the preceding page; offsets count Unicode characters.
        part = (page.extract_text() or "") + "\f"
        parts.append(part)
        pages.append({"page": number, "start": offset, "end": offset + len(part)})
        offset += len(part)
    data = "".join(parts).encode("utf-8")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("xb") as handle:
        handle.write(data)
    print(
        json.dumps(
            {
                "source_pdf_sha256": pdf_hash,
                "extracted_text_sha256": hashlib.sha256(data).hexdigest(),
                "extraction_method": f"pypdf {pypdf.__version__} page.extract_text; UTF-8; page terminator U+000C; no normalization",
                "characters": offset,
                "pages": pages,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
