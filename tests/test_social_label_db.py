"""Offline SQL contracts and independent aggregate outputs; no DB is opened."""

from copy import deepcopy
from datetime import date

import pytest

from observatory.db import Database
from observatory.models import Filters
from observatory.social_annotation_sql import (
    social_annotation_projection_sql,
    social_source_snapshot_sql,
)
from observatory.social_annotations import (
    SCHEME,
    SOCIAL_STATES,
    social_label_metadata,
    social_state_id,
)


def test_social_where_reuses_scalar_projection_and_parameterized_or_states():
    selected = [social_state_id("green_binary", "source_true"),
                social_state_id("renewable_energy", "source_false")]
    filters = Filters(dataset="social", labels=selected, accounts=["x' OR true --"],
                      sponsors=["Example company"], platforms=["Twitter"], keywords=["energy"],
                      record_ids=["record-1"], date_from=date(2020, 1, 1),
                      include_unknown_dates=False)
    where, params = Database.where(filters)
    assert social_annotation_projection_sql() in where
    assert "->'states') ?| %s" in where
    assert params[-1] == selected
    assert params[:5] == ["social", ["record-1"], ["Example company"], ["Twitter"], ["x' OR true --"]]
    assert all(value not in where for value in selected + filters.accounts)
    assert "r.active" in where and "countable" in where
    assert "v.body_hash" in where and "sh." not in where
    assert "v.version_id" in where  # no caller-specific outer join is needed


@pytest.mark.parametrize("dataset,labels", [
    ("native", [social_state_id("green_binary", "unknown")]),
    ("all", [social_state_id("green_binary", "unknown")]),
    ("social", ["green.claim"]),
    ("social", [f"{SCHEME}:green_binary:true"]),
    ("social", [social_state_id("green_binary", "source_true"), "green.claim"]),
])
def test_static_where_rejects_invalid_scope_even_after_model_copy(dataset, labels):
    forged = Filters().model_copy(update={"dataset": dataset, "labels": labels})
    with pytest.raises(ValueError):
        Database.where(forged)


def test_native_label_predicate_remains_the_legacy_independent_scheme():
    where, params = Database.where(Filters(labels=["any-old-native-label"]))
    assert "claims-calibrated" in where and params[-1] == ["any-old-native-label"]
    assert SCHEME not in where


def test_strict_sql_sanitizes_json_and_does_not_infer_false_from_bad_annotations():
    sql = social_annotation_projection_sql()
    assert "candidate.n=1" in sql
    assert "CASE WHEN jsonb_typeof(candidate.payload->'values')='object'" in sql
    assert "ELSE '{}'::jsonb END AS source_values" in sql
    assert "jsonb_object_keys(safe.source_values)" in sql and "=13" in sql
    assert "jsonb_typeof(safe.source_values->code.key)='boolean'" in sql
    assert "candidate.payload->'labels'=COALESCE" in sql
    assert "ORDER BY code.ordinal" in sql
    assert "jsonb_typeof(candidate.payload->'source_row')='number'" in sql
    assert "~ '^[1-9][0-9]*$'" in sql  # no truncating/overflowing integer cast
    assert "jsonb_typeof(candidate.payload->'source_sha256')='string'" in sql
    assert "candidate.payload->'body_sha256'=to_jsonb(v.body_hash)" in sql
    assert "candidate.payload->'version_id'=to_jsonb(v.version_id)" in sql
    assert "sha256(convert_to(v.body,'UTF8'))" in sql
    assert "CASE WHEN NOT checked.valid THEN 'unknown'" in sql
    assert "::boolean" not in sql and "LIMIT 1" not in sql
    assert "raw" not in sql and "explanations" not in sql and "source_path" not in sql


def test_public_rows_publish_only_states_and_scheme_metadata():
    sql, params = Database("")._public_query(Filters(dataset="social"))
    assert params == ["social"]
    assert "AS social_historical_states" in sql
    assert "AS social_historical_scheme" in sql and "AS social_historical_status" in sql
    assert "ELSE '[]'::jsonb END AS labels" in sql
    assert "AS body" not in sql and "AS annotations" not in sql and "AS source_token" not in sql
    assert "->'raw'" not in sql and "explanations" not in sql
    native_sql, _ = Database("")._public_query(Filters())
    assert "'[]'::jsonb AS social_historical_states" in native_sql
    assert "sha256(convert_to(v.body" not in native_sql


def test_snapshot_sql_has_full_scope_and_safe_current_source_fingerprint():
    public_sql, _ = Database("")._public_query(Filters(dataset="social", accounts=["Account"]))
    sql = social_source_snapshot_sql(public_sql, include_counts=True)
    assert public_sql in sql and "social_states AS MATERIALIZED" in sql
    assert "v.version_id=f.version_id" in sql and "WHERE f.dataset='social'" in sql
    assert "ORDER BY record_id COLLATE \"C\"" in sql
    assert "candidate.n,checked.valid,states.ids" in sql
    assert "CASE WHEN checked.valid THEN candidate.payload->'source_sha256' END" in sql
    assert "AS valid_annotation_records" in sql and "AS unknown_annotation_records" in sql
    assert "AS state_counts" in sql and "AS source_state_version" in sql
    # The original public query can choose one native annotation with LIMIT 1.
    # Only the complete social source scope and aggregate must avoid paging.
    source_scope, aggregate = sql.split("social_states AS MATERIALIZED (", 1)[1].split(
        ") SELECT ", 1
    )
    for full_scope_query in (source_scope, aggregate):
        assert " LIMIT " not in full_scope_query and " OFFSET " not in full_scope_query
    assert "->'raw'" not in sql and "source_path" not in sql and "explanations" not in sql
    version_only = social_source_snapshot_sql(public_sql)
    assert "AS source_state_version" in version_only and "AS state_counts" not in version_only


class Result:
    def __init__(self, one=None, rows=None):
        self.one, self.rows = one, rows or []

    def fetchone(self):
        return deepcopy(self.one)

    def fetchall(self):
        return deepcopy(self.rows)


class SnapshotConnection:
    """Independent query answers describe 2 native and 3 social current rows."""

    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.calls.append((sql, deepcopy(params)))
        if sql.startswith("SET TRANSACTION"):
            return Result()
        if "AS page_rows" in sql:
            return Result({
                "total": 5, "retrievable": 2, "unknown_dates": 1,
                "page_offset": 0, "page_rows": [{"record_id": "one-page-member", "dataset": "social"}],
                "groups": [{"name": "accounts", "value": "Account", "count": 3}],
                "relationships": [], "months": [], "years": [],
                "labels": [{"name": "green.claim", "count": 1}],
                "native_labels": {"total": 2, "count": 1}, "inferred": [], "combined": None,
            })
        if "AS source_state_version FROM social_states" in sql:
            if "AS state_counts" not in sql:
                return Result({"source_state_version": "a" * 64})
            counts = {}
            for item in social_label_metadata():
                counts[social_state_id(item["key"], "source_true")] = int(item["key"] == "renewable_energy")
                counts[social_state_id(item["key"], "source_false")] = 2 - int(item["key"] == "renewable_energy")
                counts[social_state_id(item["key"], "unknown")] = 1
            return Result({"total": 3, "valid_annotation_records": 2, "unknown_annotation_records": 1,
                           "state_counts": counts, "source_state_version": "a" * 64})
        return Result(rows=[])


class SnapshotDatabase(Database):
    def __init__(self):
        super().__init__("")
        self.connection = SnapshotConnection()
        self.connections = 0

    def connect(self, vector=False):
        self.connections += 1
        return self.connection


def test_dashboard_separates_native_denominator_and_complete_social_states_in_one_snapshot():
    db = SnapshotDatabase()
    output = db.dashboard(Filters(dataset="all"), limit=1)
    assert db.connections == 1
    assert db.connection.calls[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert output["page"]["total"] == 5 and len(output["page"]["rows"]) == 1
    assert output["labels"]["total"] == 2 and output["labels"]["unlabeled_records"] == 1
    assert output["labels"]["items"] == [{"name": "green.claim", "count": 1, "percent": 50}]
    social = output["social_historical_labels"]
    assert social["total"] == 3 and social["valid_annotation_records"] == 2
    assert social["unknown_annotation_records"] == 1 and social["source_state_version"] == "a" * 64
    assert len(social["items"]) == 13
    for item in social["items"]:
        assert sum(item[state] for state in SOCIAL_STATES) == 3
        assert item["source_true"] == int(item["key"] == "renewable_energy")
    assert not any("LIMIT %s" in sql for sql, _ in db.connection.calls if "AS state_counts" in sql)


def test_narrow_version_read_uses_one_full_scope_statement_and_same_summary_sql():
    db = SnapshotDatabase()
    filters = Filters(dataset="social", accounts=["Account"], labels=[social_state_id("green_binary", "unknown")])
    assert db.social_source_state_version(filters) == "a" * 64
    assert db.connections == 1 and len(db.connection.calls) == 1
    sql, params = db.connection.calls[0]
    assert params == ["social", ["Account"], filters.labels]
    assert "AS state_counts" not in sql and "AS source_state_version" in sql
    assert "LIMIT %s" not in sql


@pytest.mark.parametrize("dataset", ["native", "all"])
def test_narrow_version_read_rejects_non_social_before_connecting(dataset):
    db = SnapshotDatabase()
    with pytest.raises(ValueError):
        db.social_source_state_version(Filters(dataset=dataset))
    assert db.connections == 0


def test_fixed_social_facets_include_legal_zero_count_states():
    db = SnapshotDatabase()
    options = db.facets("social")["labels"]
    assert len(options) == len(set(options)) == 39
    assert social_state_id("green_binary", "source_false") in options
    assert social_state_id("green_binary", "unknown") in options
