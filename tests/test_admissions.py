import hashlib
import json

import pytest
from test_ingest import BODY, FIELDS, base_row, native_fixture, write_csv

from observatory.admissions import ADMISSIONS_PATH
from observatory.ingest import COMBINED_CSV, load_native
from observatory.quality import body_hash


def admission_fixture(root):
    native_fixture(root, [base_row()])
    rows = [
        base_row(f"https://example.org/extra-{i}", date="2024-01-01", publisher="WSJ")
        for i in range(4)
    ]
    write_csv(root / COMBINED_CSV, FIELDS, rows)
    manifest = {
        "schema_version": 1,
        "review_id": "fixture-v1",
        "reviewer_type": "ai",
        "scope_note": "Synthetic engineering records, not research evidence.",
        "source_path": COMBINED_CSV.as_posix(),
        "source_sha256": hashlib.sha256((root / COMBINED_CSV).read_bytes()).hexdigest(),
        "decisions": [
            {
                "review_id": f"extra-{i}",
                "url": row["url"],
                "source_row": i + 2,
                "body_sha256": body_hash(row["article"]),
                "decision": decision,
                "body_mode": "text" if i == 0 else "metadata_only",
                "sponsor_policy": "unknown" if i == 0 else "source",
                "date_policy": "unknown" if i == 0 else "source",
                "reason": "Fixture review reason",
                "evidence_refs": ["fixture-source"],
            }
            for i, (row, decision) in enumerate(
                zip(rows, ["include", "include", "exclude", "pending"], strict=True)
            )
        ],
    }
    save_manifest(root, manifest)
    return manifest, rows


def save_manifest(root, manifest):
    path = root / ADMISSIONS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_admissions_preserve_raw_values_and_distinguish_scope_from_body(tmp_path):
    manifest, rows = admission_fixture(tmp_path)
    before = (tmp_path / COMBINED_CSV).read_bytes()
    batch = load_native(tmp_path, require_admissions=True)
    assert len(batch.records) == 3 and len(batch.candidates) == 4
    text, metadata = batch.records[1:]
    assert text.countable and text.retrievable
    assert text.sponsor == "" and text.published_at is None
    assert text.raw["supplement"]["sponsor"] == "Company"
    assert text.raw["supplement"]["date"] == "2024-01-01"
    assert text.body == BODY and text.raw["admission_review"]["reviewer_type"] == "ai"
    assert metadata.countable and not metadata.retrievable and metadata.body == BODY
    assert metadata.sponsor == "company" and metadata.keyword == "Energy"
    assert metadata.publisher == "The Wall Street Journal"
    assert metadata.raw["supplement"]["publisher"] == "WSJ"
    assert metadata.raw["supplement"]["sponsor"] == "Company"
    assert [c["admission_status"] for c in batch.candidates] == [
        "admitted",
        "admitted",
        "excluded",
        "pending",
    ]
    assert ADMISSIONS_PATH.as_posix() in batch.source_hashes
    assert (tmp_path / COMBINED_CSV).read_bytes() == before
    assert (
        batch.model_dump()
        == load_native(tmp_path, require_admissions=True).model_dump()
    )


@pytest.mark.parametrize(
    "mutation", ["source", "row", "body", "url", "duplicate", "extra_field"]
)
def test_stale_or_ambiguous_manifest_stops_snapshot_preparation(tmp_path, mutation):
    manifest, _ = admission_fixture(tmp_path)
    item = manifest["decisions"][0]
    if mutation == "source":
        with (tmp_path / COMBINED_CSV).open("a", encoding="utf-8") as file:
            file.write("\n")
    elif mutation == "row":
        item["source_row"] = 99
    elif mutation == "body":
        item["body_sha256"] = "0" * 64
    elif mutation == "url":
        item["url"] = "https://example.org/absent"
    elif mutation == "duplicate":
        manifest["decisions"].append(dict(item))
    else:
        item["silent_override"] = True
    save_manifest(tmp_path, manifest)
    with pytest.raises(ValueError):
        load_native(tmp_path, require_admissions=True)


def test_missing_required_manifest_cannot_silently_remove_admitted_records(tmp_path):
    admission_fixture(tmp_path)
    (tmp_path / ADMISSIONS_PATH).unlink()
    with pytest.raises(ValueError, match="Required admission manifest"):
        load_native(tmp_path, require_admissions=True)
    assert (
        len(load_native(tmp_path).records) == 1
    )  # Explicit library baseline-only use.


def test_duplicate_extra_url_cannot_be_admitted_as_first_row(tmp_path):
    manifest, rows = admission_fixture(tmp_path)
    write_csv(tmp_path / COMBINED_CSV, FIELDS, [*rows, rows[0]])
    manifest["source_sha256"] = hashlib.sha256(
        (tmp_path / COMBINED_CSV).read_bytes()
    ).hexdigest()
    save_manifest(tmp_path, manifest)
    with pytest.raises(ValueError, match="no longer a unique"):
        load_native(tmp_path, require_admissions=True)
