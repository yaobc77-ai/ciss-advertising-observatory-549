"""Fresh frozen media-source checks without question catalogs or legacy helpers."""

import json
import sys
from copy import deepcopy

import pytest

from observatory.evaluate import canonical_digest
from observatory.media_evidence import sha256_text
from scripts import prepare_media_evidence as media


def source_record(*, dataset="native", active=True, countable=True, retrievable=True):
    body = "The Cedar library records a workshop. 保存的中文原文。"
    payload = {
        "record_id": "cedar-workshop", "dataset": dataset, "body": body,
        "title": "Cedar workshop", "url": "https://example.invalid/cedar",
        "published_at": "2042-01-03", "countable": countable, "retrievable": retrievable,
        "retrieval_ranges": [[0, 36], [37, len(body)]],
    }
    version = canonical_digest(payload)
    return {
        "record_id": payload["record_id"], "dataset": dataset, "active": active,
        "body": body, "body_sha256": sha256_text(body), "payload_sha256": version,
        "version_id": version, "payload": payload, "retrieval_ranges": deepcopy(payload["retrieval_ranges"]),
    }


@pytest.mark.parametrize("dataset", ["native", "social"])
def test_frozen_unicode_source_preserves_exact_payload_identity_and_ranges(dataset):
    row = source_record(dataset=dataset)
    original = deepcopy(row)
    checked = media.validate_snapshot_records([row])[row["record_id"]]
    assert checked == original and row == original
    assert checked["version_id"] == canonical_digest(original["payload"])
    row["payload"]["title"] = "A later unrelated title"
    assert checked["payload"]["title"] == "Cedar workshop"


def test_inactive_noncountable_nonretrievable_flags_are_preserved():
    row = source_record(active=False, countable=False, retrievable=False)
    checked = media.validate_snapshot_records([row])[row["record_id"]]
    assert checked["active"] is False
    assert checked["payload"]["countable"] is False and checked["payload"]["retrievable"] is False


@pytest.mark.parametrize("field", ["countable", "retrievable"])
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_frozen_admission_flags_never_coerce_unknown_types(field, value):
    row = source_record()
    row["payload"][field] = value
    row["version_id"] = row["payload_sha256"] = canonical_digest(row["payload"])
    with pytest.raises(ValueError, match="admission_invalid"):
        media.validate_snapshot_records([row])


@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_frozen_active_flag_never_coerces_unknown_types(value):
    row = source_record()
    row["active"] = value
    with pytest.raises(ValueError, match="identity_or_admission_invalid"):
        media.validate_snapshot_records([row])


@pytest.mark.parametrize("ranges", [[[0, True]], [[0, 6], [5, 10]], [[0, 1000]], [["0", 6]]])
def test_invalid_unicode_intervals_never_become_verified_source_ranges(ranges):
    row = source_record()
    row["payload"]["retrieval_ranges"] = ranges
    row["retrieval_ranges"] = deepcopy(ranges)
    row["version_id"] = row["payload_sha256"] = canonical_digest(row["payload"])
    with pytest.raises(ValueError):
        media.validate_snapshot_records([row])


def test_optional_declared_body_hash_cannot_disagree_with_original_bytes():
    row = source_record()
    row["body_hash"] = sha256_text("Unrelated text")
    with pytest.raises(ValueError, match="body_hash_mismatch"):
        media.validate_snapshot_records([row])


@pytest.mark.parametrize("rows", [None, [], [None], [{}]])
def test_missing_frozen_source_identity_is_rejected(rows):
    with pytest.raises(ValueError):
        media.validate_snapshot_records(rows)


def test_independent_inventory_saves_source_text_without_inference_or_original_changes(tmp_path):
    row = source_record(countable=False)
    sources = {
        "snapshot": {"records": [row]},
        "triage": {"records": [{
            "record_id": row["record_id"], "original_url": row["payload"]["url"],
            "upstream_article_id": "cedar", "triage_route": "saved_text_candidate_review",
            "input_version_id": row["version_id"],
        }]},
        "probe": {"record_id": "another-source", "results": []},
        "page_check": {"records": []},
    }
    paths = {}
    originals = {}
    for name, value in sources.items():
        filename = name + ".json"
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        (tmp_path / filename).write_bytes(raw)
        originals[filename] = raw
        paths[name] = filename
    result = media.prepare_inventory(tmp_path, tmp_path / "inventory", paths)
    assert result["model_calls"] == result["network_calls"] == result["database_operations"] == 0
    assert result["counts"]["tasks"] == result["counts"]["saved_page_text_assets"] == 1
    assert (tmp_path / "inventory/saved_page_text/cedar.txt").read_text(encoding="utf-8") == row["body"]
    queue = json.loads((tmp_path / "inventory/media_tasks.json").read_text(encoding="utf-8"))
    assert queue["tasks"][0]["countable"] is False
    assert queue["tasks"][0]["voice_status"] == "unknown"
    assert all((tmp_path / filename).read_bytes() == raw for filename, raw in originals.items())
    assert "prepare_content_review" not in sys.modules
