"""Private, offline comparison packets for legacy CLAIMS source candidates.

A saved search reference and a matching captured excerpt do not authenticate a
web page, establish complete article identity, or authorize classification.
The command writes only a new local packet, never database or network state.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

from .claims_linkage import load_input_bytes
from .claims_source_search import POLICY_VERSION, TOOL_NAME, _public_url
from .claims_spans import ClaimSpanLocator, validate_claim_span

SCHEMA = "claims-source-review-packet-v1"
MAX_JSON_BYTES = 250_000
MAX_CSV_BYTES = 20_000_000
MAX_CAPTURE_BYTES = 1_000_000
MAX_LOCATIONS = 100


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError as exc:
        raise ValueError("Cannot read the requested packet input") from exc
    if len(data) > limit:
        raise ValueError("Input exceeds the offline packet size limit")
    return data


def _json(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON keys are not allowed")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("Nonfinite JSON numbers are not allowed")

    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Nonfinite JSON numbers are not allowed")
        return parsed

    try:
        value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs,
                           parse_constant=constant, parse_float=number)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("A UTF-8 JSON object is required") from exc
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required")
    return value


def _time(value):
    if not isinstance(value, str):
        raise ValueError("Capture time must include a timezone")
    try:
        parsed = datetime.fromisoformat(value)
        if ("T" not in value and " " not in value) or parsed.utcoffset() is None:
            raise ValueError("Capture time must include a timezone")
    except ValueError as exc:
        raise ValueError("Capture time must include a timezone") from exc


def _occurrences(text, excerpt):
    positions, cursor = [], 0
    while (position := text.find(excerpt, cursor)) != -1:
        positions.append(position)
        cursor = position + 1
    return positions


def _capture_files(path, urls):
    if path is None:
        return None, {}
    data = _read(path, MAX_JSON_BYTES)
    manifest = _json(data)
    if set(manifest) != {"schema_version", "captures"} or manifest["schema_version"] != "claims-source-captures-v1":
        raise ValueError("Unknown capture manifest format")
    rows = manifest["captures"]
    if not isinstance(rows, list) or len(rows) > 5:
        raise ValueError("At most five captures are allowed")
    captures = {}
    required = {"candidate_url", "text_file", "captured_at", "capture_method", "completeness"}
    allowed = required | {"title", "publisher", "final_url"}
    for row in rows:
        if not isinstance(row, dict) or not required <= set(row) <= allowed:
            raise ValueError("Capture fields do not match the manifest format")
        url = row["candidate_url"]
        if not isinstance(url, str) or url not in urls or url in captures:
            raise ValueError("Capture URL must identify one distinct recorded candidate")
        if not isinstance(row["text_file"], str) or not row["text_file"]:
            raise ValueError("Capture text needs a relative filename")
        relative = Path(row["text_file"])
        windows, posix = PureWindowsPath(row["text_file"]), PurePosixPath(row["text_file"])
        if relative.is_absolute() or windows.drive or windows.root or posix.is_absolute():
            raise ValueError("Capture text needs a relative filename")
        source = (path.parent / relative).resolve()
        if not source.is_relative_to(path.parent.resolve()):
            raise ValueError("Capture text must stay within the manifest directory")
        _time(row["captured_at"])
        if not isinstance(row["completeness"], str) or row["completeness"] not in {"unknown", "partial", "complete"}:
            raise ValueError("Unknown capture completeness")
        for name in ("capture_method", "title", "publisher"):
            value = row.get(name, "")
            if not isinstance(value, str) or len(value) > 500 or (name == "capture_method" and not value.strip()):
                raise ValueError("Capture metadata must be bounded text")
        if "final_url" in row and not _public_url(row["final_url"]):
            raise ValueError("Final URL must be a public HTTP(S) URL")
        raw = _read(source, MAX_CAPTURE_BYTES)
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise ValueError("Capture text must be UTF-8") from exc
        metadata = {key: value for key, value in row.items() if key != "text_file"}
        captures[url] = (raw, text, metadata)
    return data, captures


def _render(packet):
    def escape(value):
        return html.escape(str(value), quote=True)
    parts = ["<!doctype html><html lang='en'><meta charset='utf-8'>",
             "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">",
             "<title>CLAIMS source review</title><style>body{max-width:1000px;margin:48px auto;padding:0 24px;font:16px/1.6 system-ui;color:#183044}h1,h2{line-height:1.2}section{padding:24px 0;border-top:1px solid #cad2d9}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f2f5f7;padding:20px}a{color:#165479}small{color:#506374}</style>",
             "<h1>CLAIMS source review</h1><p>Every source association is pending. Excerpt matches identify text locations; they do not establish article identity or authorize publication.</p>",
             f"<p>Legacy input {escape(packet['legacy_input_id'])} · article {escape(packet['legacy_article_id'])}. Numeric legacy IDs refer only to the supplied CSV.</p>",
             "<p><a href='source_input.json'>Open the frozen original paragraph and input binding</a> · <a href='review.json'>Open the pending review file</a></p>",
             f"<h2>Original searched excerpt</h2><pre>{escape(packet['excerpt'])}</pre>"]
    for candidate in packet["candidates"]:
        parts.extend(["<section>", f"<h2>{escape(candidate['title'] or candidate['url'])}</h2>",
                      f"<a href='{escape(candidate['url'])}' target='_blank' rel='noopener noreferrer'>Open candidate page</a>",
                      f"<p>{escape(candidate['comparison_state'])} · {candidate['match_count']} excerpt locations · source decision: pending</p>"])
        if candidate.get("capture"):
            capture = candidate["capture"]
            parts.append(f"<p>Capture coverage (as supplied): {escape(capture['completeness'])}. Capture metadata is an assertion, not proof that the URL served these bytes.</p>")
            parts.append("<dl>" + "".join(f"<dt>{escape(label)} (as supplied)</dt><dd>{escape(capture[key])}</dd>"
                         for key, label in (("title", "Captured title"), ("publisher", "Publisher"),
                                            ("final_url", "Final URL"), ("captured_at", "Capture time"),
                                            ("capture_method", "Capture method")) if capture.get(key)) + "</dl>")
            parts.append(f"<a href='{escape(capture['packet_text_file'])}'>Open saved UTF-8 text</a>")
        for location in candidate["locations"]:
            parts.append(f"<p>Characters [{location['start']}, {location['end']}) · {escape(location['match_method'])}</p>")
            parts.append(f"<pre>{escape(location['context_before'])}<mark>{escape(location['quote'])}</mark>{escape(location['context_after'])}</pre>")
        if not candidate["locations_complete"]:
            parts.append("<p>Only the first 100 locations are shown. Repeated occurrences remain unresolved.</p>")
        parts.append("</section>")
    if not packet["candidates"]:
        parts.append("<p>No candidate URLs were recorded. Source identity remains unresolved.</p>")
    parts.append("<p>Review title, publisher, paragraph order and original captures. For an external page, follow corpus admission and rerun claims-audit before using the reviewed-result importer.</p></html>")
    return "\n".join(parts).encode("utf-8")


def write_source_review_packet(input_csv: Path, input_id: str, lookup: Path, out: Path,
                               *, captures: Path | None = None, excerpt_start: int | None = None) -> dict:
    """Freeze one original row and source candidates; all human decisions are pending."""
    input_csv, lookup, out = Path(input_csv), Path(lookup), Path(out)
    if out.exists() or out.is_symlink():
        raise ValueError("Choose a new private output directory")
    csv_bytes = _read(input_csv, MAX_CSV_BYTES)
    paragraphs = load_input_bytes(csv_bytes)
    selected = [item for item in paragraphs if item.source_id == input_id]
    if len(selected) != 1:
        raise ValueError("Input ID must identify one original CSV row")
    paragraph = selected[0]
    receipt_bytes = _read(lookup, MAX_JSON_BYTES)
    document = _json(receipt_bytes)
    receipt = document.get("result", document)
    if not isinstance(receipt, dict) or receipt.get("tool") != TOOL_NAME or receipt.get("policy_version") != POLICY_VERSION:
        raise ValueError("Unknown source discovery receipt")
    if receipt.get("status") not in {"ok", "unresolved"} or receipt.get("identity_verified", False) is not False:
        raise ValueError("Only unverified completed candidate lookups can be reviewed")
    excerpt = receipt.get("excerpt")
    if not isinstance(excerpt, str) or not 40 <= len(excerpt) <= 2000 or not excerpt.strip():
        raise ValueError("Receipt needs its unchanged searched excerpt")
    if receipt.get("excerpt_sha256") != _sha(excerpt.encode("utf-8")):
        raise ValueError("Receipt excerpt hash differs from its original text")
    positions = _occurrences(paragraph.text, excerpt)
    if excerpt_start is None:
        if len(positions) != 1:
            raise ValueError("An unchanged unique input excerpt or explicit excerpt_start is required")
        excerpt_start = positions[0]
    if type(excerpt_start) is not int or excerpt_start not in positions:
        raise ValueError("Excerpt start does not locate the unchanged input text")
    descriptor = document.get("legacy_input")
    if descriptor is not None:
        expected = {"source_file_sha256": _sha(csv_bytes), "legacy_input_id": input_id,
                    "legacy_article_id": paragraph.article_id,
                    "original_text_sha256": _sha(paragraph.text.encode("utf-8")),
                    "excerpt": excerpt, "excerpt_start": excerpt_start, "excerpt_end": excerpt_start + len(excerpt)}
        if not isinstance(descriptor, dict) or any(descriptor.get(key) != value or type(descriptor.get(key)) is not type(value)
                                                   for key, value in expected.items()):
            raise ValueError("Legacy lookup descriptor differs from the selected original CSV row")
    recorded = receipt.get("candidates")
    if not isinstance(recorded, list) or len(recorded) > 5:
        raise ValueError("At most five recorded source candidates are allowed")
    urls = []
    for row in recorded:
        if not isinstance(row, dict) or not _public_url(row.get("url")):
            raise ValueError("Recorded candidates need public HTTP(S) URLs")
        if (row["url"] in urls or row.get("source_association", "needs_review") != "needs_review"
                or row.get("identity_verified", False) is not False):
            raise ValueError("Recorded candidates must be distinct and unapproved")
        if not isinstance(row.get("title", ""), str):
            raise ValueError("Candidate title must be text")
        urls.append(row["url"])
    capture_manifest, captured = _capture_files(Path(captures) if captures is not None else None, urls)
    files = {"lookup_receipt.json": receipt_bytes}
    if capture_manifest is not None:
        files["capture_manifest.json"] = capture_manifest
    source_input = {"schema_version": SCHEMA, "source_file_sha256": _sha(csv_bytes),
                    **asdict(paragraph), "text_sha256": _sha(paragraph.text.encode("utf-8")),
                    "excerpt_start": excerpt_start, "excerpt_end": excerpt_start + len(excerpt),
                    "excerpt": excerpt, "excerpt_sha256": receipt["excerpt_sha256"]}
    packet = {"schema_version": SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
              "legacy_input_id": input_id, "legacy_article_id": paragraph.article_id,
              "source_file_sha256": _sha(csv_bytes), "lookup_receipt_sha256": _sha(receipt_bytes),
              "excerpt": excerpt, "excerpt_sha256": receipt["excerpt_sha256"], "candidates": [],
              "identity_verified": False, "complete_article_identity": "not_established",
              "capture_provenance": "supplied_assertions_not_authenticated", "publication_applied": False,
              "model_calls": 0, "network_calls": 0, "database_writes": 0}
    for number, row in enumerate(recorded, 1):
        url = row["url"]
        key = _sha(json.dumps([SCHEMA, _sha(csv_bytes), input_id, _sha(receipt_bytes), url], separators=(",", ":")).encode())
        candidate = {"candidate_key": key, "url": url, "title": row.get("title", ""),
                     "source_kind": row.get("source_kind"), "provider_source": row.get("provider_source"),
                     "source_association": "pending", "identity_verified": False,
                     "comparison_state": "not_captured", "match_count": 0, "locations": [], "locations_complete": True}
        if url in captured:
            raw, text, metadata = captured[url]
            spans = ClaimSpanLocator(text).locate(excerpt)
            for span in spans:
                validate_claim_span(text, excerpt, span)
            filename = f"captured-text/{number:03d}.txt"
            files[filename] = raw
            candidate.update(comparison_state="no_match" if not spans else "unique_excerpt" if len(spans) == 1 else "repeated_excerpt",
                             match_count=len(spans), locations_complete=len(spans) <= MAX_LOCATIONS,
                             capture={**metadata, "packet_text_file": filename, "file_sha256": _sha(raw),
                                      "text_sha256": _sha(text.encode("utf-8")), "provenance_verified": False},
                             locations=[{**asdict(span), "context_before": text[max(0, span.start - 200):span.start],
                                         "context_after": text[span.end:span.end + 200]} for span in spans[:MAX_LOCATIONS]])
        packet["candidates"].append(candidate)
    review = {"schema_version": SCHEMA, "lookup_receipt_sha256": _sha(receipt_bytes),
              "source_file_sha256": _sha(csv_bytes), "publication_applied": False,
              "decisions": {item["candidate_key"]: {"decision": "pending", "reviewer": None,
                                                     "reviewed_at": None, "note": None}
                            for item in packet["candidates"]}}
    for name, value in (("source_input.json", source_input), ("packet.json", packet), ("review.json", review)):
        files[name] = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    files["review.html"] = _render(packet)
    # Validate first, create once, and write the completion marker last. A partial
    # packet has no manifest and is not a completed handoff artifact.
    out.mkdir(parents=True, exist_ok=False)
    for name, data in files.items():
        destination = out / name
        destination.parent.mkdir(exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(data)
    manifest = {"schema_version": SCHEMA, "complete": True,
                "artifacts": {name: _sha(data) for name, data in sorted(files.items())}}
    with (out / "packet_manifest.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"schema_version": SCHEMA, "candidate_count": len(recorded), "capture_count": len(captured),
            "comparison_states": dict(Counter(item["comparison_state"] for item in packet["candidates"])),
            "identity_verified": False, "publication_applied": False, "model_calls": 0,
            "network_calls": 0, "database_writes": 0, "packet_manifest_sha256": _sha((out / "packet_manifest.json").read_bytes())}
