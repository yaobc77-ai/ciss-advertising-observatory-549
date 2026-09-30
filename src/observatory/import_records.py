"""Validate a canonical JSONL batch before opening an import transaction."""

import hashlib
import json
import re
from pathlib import Path

from pydantic import ConfigDict, ValidationError

from .chunking import retrieval_spans
from .models import ImportBatch, RecordInput
from .quality import missing, valid_url

_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")


class CanonicalRecord(RecordInput):
    model_config = ConfigDict(extra="forbid", strict=True)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("JSON must not contain NaN or Infinity")


def load_records(path: Path, *, dataset: str | None = None) -> ImportBatch:
    """Keep explicit IDs, original bodies, eligibility and source metadata unchanged.

    No label, sponsor, date, source URL or retrieval approval is inferred. A source
    file checksum and line number are appended to provenance for reproducibility.
    Unknown top-level fields fail: original source fields belong in ``raw``.
    """
    data = path.read_bytes()
    checksum = hashlib.sha256(data).hexdigest()
    source = path.name
    batch = ImportBatch(source_hashes={source: checksum})
    ids: set[str] = set()
    errors = []
    for number, line in enumerate(data.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            json.loads(line, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            # JSON validation permits ISO dates, while keeping booleans/strings strict.
            record = CanonicalRecord.model_validate_json(line)
            if not _RECORD_ID.fullmatch(record.record_id):
                raise ValueError(
                    "record_id must match [A-Za-z0-9][A-Za-z0-9._:-]{0,199}; "
                    "preserve the original source ID in raw when mapping it"
                )
            if record.record_id in ids:
                raise ValueError("duplicate record_id within the batch")
            if dataset is not None and record.dataset != dataset:
                raise ValueError("record dataset does not match the declared dataset")
            if not valid_url(record.url) or record.url != record.url.strip():
                raise ValueError("url must be a nonempty HTTP(S) source URL without surrounding whitespace")
            if record.archive_url and not valid_url(record.archive_url):
                raise ValueError("archive_url must be an HTTP(S) URL when supplied")
            if record.dataset == "social" and not record.platform.strip():
                raise ValueError("social records require an explicit platform")
            spans = retrieval_spans(
                record.body, retrieval_ranges=record.retrieval_ranges,
                retrieval_end=record.retrieval_end,
            )
            if record.retrievable and (
                missing(record.body) or not spans
                or not any(record.body[start:end].strip() for start, end in spans)
                or record.body.strip().casefold() == "video"
            ):
                raise ValueError("retrievable=true requires source text and a nonempty valid retrieval span")
            record.provenance.append({
                "source": source, "source_asset_id": f"sha256:{checksum}",
                "sha256": checksum, "row": number, "row_basis": "jsonl_line",
                "role": "canonical_import",
            })
            ids.add(record.record_id)
            batch.records.append(record)
        except ValidationError as exc:
            fields = ", ".join(
                ".".join(map(str, error["loc"])) or "record"
                for error in exc.errors(include_input=False)
            )
            errors.append(f"line {number}: invalid fields ({fields})")
        except ValueError as exc:
            errors.append(f"line {number}: {exc}")
    if errors:
        raise ValueError("No records imported; canonical JSONL validation failed:\n" + "\n".join(errors))
    if not batch.records:
        raise ValueError("Canonical JSONL must contain at least one record")
    return batch
