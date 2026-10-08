"""Exact current-record and observation bindings at the DB read boundary."""

import pytest
from test_social_source_retrieval import admitted, enabled

from observatory.quality import body_hash
from observatory.social_source_binding import bind_search_row, evidence_source
from observatory.social_source_retrieval import (
    source_version_id,
    validated_observations,
)


def source_fixture():
    payload = enabled(admitted(("Shared saved post.", "Shared saved post. Lower carbon preview."),
                              reason="paused_body_disagreement"))
    sources = validated_observations(payload)
    source = sources[1]
    row = {"evidence_id": "chunk:one", "record_id": payload["record_id"],
           "version_id": source_version_id(payload), "dataset": "social", "title": "Saved post",
           "url": payload["url"], "text": source["body"], "start": 0, "end": len(source["body"]),
           "observation_payload": payload,
           **{key: source[key] for key in ("source_observation_id", "source_version_id", "source_body_hash")}}
    return payload, sources, row


def test_search_binding_exposes_alternate_source_not_private_payload():
    _, sources, row = source_fixture()
    result = bind_search_row(row)
    assert result.text == sources[1]["body"] and result.source_observation_count == 2
    assert result.source_conflicts == ["body"]
    assert "PRIVATE" not in result.model_dump_json() and "observation_payload" not in result.model_dump()


@pytest.mark.parametrize("field,value", [
    ("record_id", "different:post"), ("version_id", "d" * 64),
    ("dataset", "native"), ("url", "https://example.org/different"),
    ("source_observation_id", "junkipedia:404"), ("source_version_id", "d" * 64),
    ("source_body_hash", "d" * 64), ("start", 1), ("text", "Invented source"),
])
def test_cross_record_stale_or_forged_search_bindings_rejected(field, value):
    _, _, row = source_fixture()
    row[field] = value
    with pytest.raises(ValueError):
        bind_search_row(row)


def test_current_source_binding_requires_all_metadata_and_exact_unicode():
    payload, sources, row = source_fixture()
    item = bind_search_row(row)
    current = {"record_id": item.record_id, "version_id": item.version_id, "dataset": "social",
               "retrievable": True, "body": payload["body"], "body_hash": body_hash(payload["body"]),
               "social_source_observations": sources}
    assert evidence_source(current, item)["body"] == sources[1]["body"]
    for key, value in (("version_id", "d" * 64), ("retrievable", False),
                       ("social_source_observations", [sources[0]])):
        with pytest.raises(ValueError):
            evidence_source({**current, key: value}, item)
    forged = item.model_copy(update={"source_conflicts": []})
    with pytest.raises(ValueError):
        evidence_source(current, forged)


def test_native_null_provenance_preserves_canonical_offsets():
    item = bind_search_row({"evidence_id": "native:one", "record_id": "native:one", "version_id": "v1",
        "dataset": "native", "title": "Old source", "text": "Source.", "start": 0, "end": 7,
        "source_observation_id": None, "source_version_id": None, "source_body_hash": None,
        "observation_payload": None})
    current = {"record_id": item.record_id, "version_id": item.version_id, "dataset": "native",
               "retrievable": True, "body": "Source.", "body_hash": body_hash("Source.")}
    assert evidence_source(current, item)["body"] == "Source."


def test_mutated_evidence_cannot_bypass_model_validation():
    _, sources, row = source_fixture()
    item = bind_search_row(row)
    current = {"record_id": item.record_id, "version_id": item.version_id, "dataset": "social",
               "retrievable": True, "social_source_observations": sources}
    for value in (False, True, -1, 100000):
        with pytest.raises(ValueError):
            evidence_source(current, item.model_copy(update={"start": value}))
