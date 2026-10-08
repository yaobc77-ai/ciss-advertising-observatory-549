"""Batch/source parity; optional PostgreSQL cases execute only SELECT CTEs.

The scalar SQL byte hash freezes the pre-P32 reference. PostgreSQL tests shadow
business table names with fabricated CTEs; no tables are created or modified.
"""

import hashlib
import json
import os
from copy import deepcopy

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from observatory.config import Settings
from observatory.social_annotation_sql import (
    social_annotation_projection_sql,
    social_source_snapshot_sql,
)
from observatory.social_annotations import SCHEME, STATUS, social_label_metadata

SCALAR_SHA256 = "5f0d9e457fef447b953b21f62362f5abfa733c8504a21acf613253e929b0cf18"
KEYS = [item["key"] for item in social_label_metadata()]


def legacy_snapshot_sql(public_sql, *, include_counts=False):
    """Pre-P32 query shape with its independently frozen scalar SQL bytes."""
    assert hashlib.sha256(social_annotation_projection_sql().encode()).hexdigest() == SCALAR_SHA256
    counts = """
        count(*) AS total,
        count(*) FILTER (WHERE annotation->'valid'='true'::jsonb) AS valid_annotation_records,
        count(*) FILTER (WHERE annotation->'valid'='false'::jsonb) AS unknown_annotation_records,
        COALESCE((SELECT jsonb_object_agg(state_id,n) FROM (
            SELECT state_id,count(*) AS n FROM social_states
            CROSS JOIN LATERAL jsonb_array_elements_text(annotation->'states') AS state_id
            GROUP BY state_id
        ) state_counts),'{}'::jsonb) AS state_counts,
    """ if include_counts else ""
    return f"""WITH filtered AS ({public_sql}), social_states AS MATERIALIZED (
        SELECT f.record_id,f.version_id,{social_annotation_projection_sql()} AS annotation
        FROM filtered f JOIN record_versions v ON v.version_id=f.version_id
        WHERE f.dataset='social'
    ) SELECT {counts} encode(sha256(convert_to(jsonb_build_array(
        '{SCHEME}','{STATUS}',COALESCE(jsonb_agg(jsonb_build_array(
            record_id,version_id,annotation->>'source_token'
        ) ORDER BY record_id COLLATE "C"),'[]'::jsonb)
    )::text,'UTF8')),'hex') AS source_state_version FROM social_states"""


def test_scalar_reference_remains_byte_identical_after_shared_predicate_extraction():
    assert hashlib.sha256(social_annotation_projection_sql().encode()).hexdigest() == SCALAR_SHA256


@pytest.mark.parametrize("include_counts", [False, True])
def test_snapshot_is_set_oriented_and_keeps_the_original_scope(include_counts):
    sql = social_source_snapshot_sql("SELECT * FROM snapshot_fixture", include_counts=include_counts)
    assert "public_source AS NOT MATERIALIZED" in sql
    assert "filtered AS MATERIALIZED (\n        SELECT record_id,version_id,dataset FROM public_source" in sql
    for name in ("social_versions", "candidates", "source_values", "value_metrics",
                 "checked_sources", "version_states", "version_annotations", "social_states"):
        assert f"{name} AS MATERIALIZED" in sql
    assert "SELECT DISTINCT f.version_id FROM filtered f WHERE f.dataset='social'" in sql
    assert "FROM social_versions v LEFT JOIN annotations a ON a.version_id=v.version_id" in sql
    assert sql.count("sha256(convert_to(v.body,'UTF8'))") == 1
    assert "candidate.n=1" in sql and "count(a.version_id) AS n" in sql
    assert "FROM filtered f JOIN version_annotations v ON v.version_id=f.version_id" in sql
    assert 'ORDER BY record_id COLLATE "C"' in sql
    assert " LIMIT " not in sql and " OFFSET " not in sql
    assert ("AS state_counts" in sql) is include_counts
    assert "->'raw'" not in sql and "explanations" not in sql


def fixture(mode="valid"):
    body = "Synthetic source: clean energy can create employment."
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    values = dict.fromkeys(KEYS, False)
    values[KEYS[0]] = values[KEYS[-1]] = True
    payload = {
        "version": SCHEME, "status": STATUS,
        "basis": "supplied_source_post_id_and_exact_body",
        "taxonomy_mapping": "source_keys_preserved_no_native_label_mapping",
        "values": values, "labels": [key for key in KEYS if values[key]],
        "source_sha256": "a" * 64, "source_row": 1,
        "body_sha256": body_hash,
    }
    versions = [{"version_id": "version-1", "body_hash": body_hash, "body": body}]
    annotations = [{"version_id": "version-1", "ordinal": 0, "payload": payload}]
    rows = [{"record_id": "record-1", "version_id": "version-1", "dataset": "social"}]
    if mode == "missing":
        annotations.clear()
    elif mode in {"values_array", "values_string", "values_number", "values_null"}:
        payload["values"] = {"values_array": [], "values_string": "false",
                             "values_number": 13, "values_null": None}[mode]
    elif mode == "missing_key":
        del values[KEYS[-1]]
    elif mode == "extra_key":
        values["extra"] = False
    elif mode == "non_boolean":
        values[KEYS[0]] = "true"
    elif mode == "row_float":
        payload["source_row"] = 1.0
    elif mode == "row_string":
        payload["source_row"] = "1"
    elif mode == "row_zero":
        payload["source_row"] = 0
    elif mode == "row_negative":
        payload["source_row"] = -1
    elif mode == "row_huge":
        payload["source_row"] = 10**100  # no narrowing integer conversion
    elif mode == "source_hash":
        payload["source_sha256"] = "A" * 64
    elif mode == "body_hash":
        payload["body_sha256"] = "b" * 64
    elif mode == "actual_body_changed":
        versions[0]["body"] += " Later source revision."
    elif mode == "stored_body_hash_invalid":
        versions[0]["body_hash"] = "not-a-sha256"
    elif mode == "optional_version_correct":
        payload["version_id"] = "version-1"
    elif mode == "optional_version_stale":
        payload["version_id"] = "stale-version"
    elif mode == "optional_version_null":
        payload["version_id"] = None
    elif mode == "status":
        payload["status"] = "approved"
    elif mode == "basis":
        payload["basis"] = "title_only"
    elif mode == "taxonomy":
        payload["taxonomy_mapping"] = "mapped_to_native"
    elif mode == "labels_order":
        payload["labels"].reverse()
    elif mode == "all_false":
        payload["values"] = dict.fromkeys(KEYS, False)
        payload["labels"] = []
    elif mode in {"duplicate_valid", "duplicate_one_valid"}:
        annotations.append(deepcopy(annotations[0]))
        annotations[-1]["ordinal"] = 1
        if mode == "duplicate_one_valid":
            annotations[-1]["payload"]["status"] = "not_valid"
    elif mode == "other_scheme":
        other = deepcopy(annotations[0])
        other["ordinal"] = 1
        other["payload"]["version"] = "unrelated-scheme"
        annotations.append(other)
    elif mode == "out_of_scope":
        other = deepcopy(annotations[0])
        other["version_id"] = "out-of-scope-version"
        annotations.append(other)
    elif mode == "two_record_ids":
        rows.append({**rows[0], "record_id": "record-2"})
    elif mode == "duplicate_row":
        rows.append(deepcopy(rows[0]))
    elif mode == "mixed":
        rows.append({**rows[0], "record_id": "native-record", "dataset": "native"})
    elif mode == "native_only":
        rows[0]["dataset"] = "native"
    elif mode == "empty":
        rows.clear()
    elif mode == "missing_version":
        versions.clear()
    elif mode == "c_sort":
        rows = [{**rows[0], "record_id": key} for key in ["z", "é", "Z", "α", "a"]]
    elif mode == "ignored_fields":
        payload.update(explanations={"private": "ignored"}, source_path="private/local/path", raw=[1, 2])
    return {"annotations": annotations, "versions": versions, "rows": rows}


@pytest.fixture(scope="module")
def readonly_pg():
    if os.environ.get("OBS_RUN_SOCIAL_SNAPSHOT_BATCH") != "1":
        pytest.skip("Readonly synthetic PostgreSQL parity opt-in is absent")
    settings = Settings.from_env()
    options = conninfo_to_dict(settings.database_url)
    if options.get("host", "") not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("Readonly parity requires the explicit local database")
    with psycopg.connect(settings.database_url, connect_timeout=5) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        connection.execute("SET LOCAL statement_timeout='30s'")
        yield connection


def snapshot_text(connection, builder, data, *, include_counts=True):
    prefix = """WITH annotations AS MATERIALIZED (
        SELECT item->>'version_id' AS version_id,(item->>'ordinal')::integer AS ordinal,
            item->'payload' AS payload FROM jsonb_array_elements(%s::jsonb) item
    ), record_versions AS MATERIALIZED (
        SELECT item->>'version_id' AS version_id,item->>'body_hash' AS body_hash,
            item->>'body' AS body FROM jsonb_array_elements(%s::jsonb) item
    ), snapshot_fixture AS MATERIALIZED (
        SELECT item->>'record_id' AS record_id,item->>'version_id' AS version_id,
            item->>'dataset' AS dataset FROM jsonb_array_elements(%s::jsonb) item
    ), """
    statement = prefix + builder("SELECT * FROM snapshot_fixture", include_counts=include_counts)[5:]
    params = (Jsonb(data["annotations"]), Jsonb(data["versions"]), Jsonb(data["rows"]))
    return connection.execute("SELECT row_to_json(snapshot)::text FROM (" + statement + ") snapshot",
                              params).fetchone()[0]


MODES = (
    "valid", "missing", "values_array", "values_string", "values_number", "values_null",
    "missing_key", "extra_key", "non_boolean", "row_float", "row_string", "row_zero",
    "row_negative", "row_huge", "source_hash", "body_hash", "actual_body_changed",
    "stored_body_hash_invalid", "optional_version_correct", "optional_version_stale",
    "optional_version_null", "status", "basis", "taxonomy", "labels_order", "all_false",
    "duplicate_valid", "duplicate_one_valid", "other_scheme", "out_of_scope", "two_record_ids",
    "duplicate_row", "mixed", "native_only", "empty", "missing_version", "c_sort", "ignored_fields",
)


@pytest.mark.integration
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("include_counts", [False, True])
def test_batch_result_text_exactly_equals_frozen_scalar_snapshot(readonly_pg, mode, include_counts):
    data = fixture(mode)
    expected = snapshot_text(readonly_pg, legacy_snapshot_sql, data, include_counts=include_counts)
    actual = snapshot_text(readonly_pg, social_source_snapshot_sql, data, include_counts=include_counts)
    assert actual == expected
    if include_counts:
        result = json.loads(actual)
        if mode in {"duplicate_valid", "duplicate_one_valid", "non_boolean", "values_null", "row_float"}:
            assert result["valid_annotation_records"] == 0
            assert result["unknown_annotation_records"] == 1
            assert all(key.endswith(":unknown") for key in result["state_counts"])
        if mode == "two_record_ids":
            assert result["total"] == result["valid_annotation_records"] == 2
        if mode in {"empty", "native_only", "missing_version"}:
            assert result["total"] == 0 and result["state_counts"] == {}


@pytest.mark.integration
def test_ignored_fields_do_not_change_fingerprint_but_actual_body_does(readonly_pg):
    def read(mode):
        return json.loads(snapshot_text(readonly_pg, social_source_snapshot_sql, fixture(mode)))

    assert read("valid") == read("ignored_fields")
    assert read("actual_body_changed")["source_state_version"] != read("valid")["source_state_version"]


@pytest.mark.integration
def test_narrow_scope_does_not_evaluate_unused_public_projection(readonly_pg):
    """An unused scalar expression must be pruned before selected-row materialization."""
    def with_unused_projection(public_sql, **options):
        public_sql = (
            "SELECT record_id,version_id,dataset,(SELECT 1/0) AS unused_public_projection "
            "FROM snapshot_fixture"
        )
        return social_source_snapshot_sql(public_sql, **options)

    expected = snapshot_text(readonly_pg, legacy_snapshot_sql, fixture())
    assert snapshot_text(readonly_pg, with_unused_projection, fixture()) == expected


@pytest.mark.integration
def test_missing_annotation_still_binds_unknown_fingerprint_to_actual_body(readonly_pg):
    original = fixture("missing")
    changed = deepcopy(original)
    changed["versions"][0]["body"] += " Changed while the stored hash remains unchanged."
    assert changed["versions"][0]["body_hash"] == original["versions"][0]["body_hash"]
    results = []
    for data in (original, changed):
        expected = snapshot_text(readonly_pg, legacy_snapshot_sql, data)
        actual = snapshot_text(readonly_pg, social_source_snapshot_sql, data)
        assert actual == expected
        results.append(json.loads(actual))
    assert all(result["unknown_annotation_records"] == 1 for result in results)
    assert results[0]["state_counts"] == results[1]["state_counts"]
    assert results[0]["source_state_version"] != results[1]["source_state_version"]


@pytest.mark.integration
def test_invalid_candidate_ignores_arbitrary_private_fields(readonly_pg):
    original = fixture("duplicate_one_valid")
    changed = deepcopy(original)
    for annotation in changed["annotations"]:
        annotation["payload"].update(raw={"arbitrary": "new"},
                                     explanations={"private": "new explanation"},
                                     source_path="changed/private/path")
    results = []
    for data in (original, changed):
        expected = snapshot_text(readonly_pg, legacy_snapshot_sql, data)
        actual = snapshot_text(readonly_pg, social_source_snapshot_sql, data)
        assert actual == expected
        results.append(json.loads(actual))
    assert all(result["unknown_annotation_records"] == 1 for result in results)
    assert results[0] == results[1]

