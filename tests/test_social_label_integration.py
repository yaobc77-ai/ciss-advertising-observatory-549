"""Opt-in source-state counts and members in one owned local test schema.

All inputs are fabricated. Expected members come from the fixture construction,
not from the SQL projection under test or from a fresh classification.
"""

import hashlib
import json
import os
from copy import deepcopy
from datetime import date

import pytest
from psycopg.types.json import Jsonb
from test_social_accounts_integration import isolated_db as isolated_db
from test_social_accounts_integration import post

from observatory.config import Settings
from observatory.models import Filters, ImportBatch
from observatory.research_tools import ToolCatalog
from observatory.service import Service
from observatory.social_annotations import (
    SCHEME,
    SOCIAL_LABELS,
    STATUS,
    social_annotation_details,
    social_state_id,
)

pytestmark = pytest.mark.integration
CHECKS = {"source_state_full_aggregates", "source_state_members_and_paging",
          "source_state_version_changes", "source_state_mcp_scope", "source_state_snapshot_consistency"}
BAD_MODES = ("missing", "boolean", "missing_key", "extra_key", "row_float", "row_string",
             "source_hash", "status", "positive_list", "values_array", "values_scalar",
             "body_hash", "duplicate", "stale")


def source_post(identifier, *, positive=False, mode="valid", **changes):
    item = post(identifier, **changes)
    values = dict.fromkeys(SOCIAL_LABELS, False)
    if positive:
        values["green_binary"] = values["decreasing_emissions"] = True
    annotation = {
        "version": SCHEME, "status": STATUS,
        "basis": "supplied_source_post_id_and_exact_body",
        "taxonomy_mapping": "source_keys_preserved_no_native_label_mapping",
        "values": values, "labels": [key for key in SOCIAL_LABELS if values[key]],
        "body_sha256": hashlib.sha256(item.body.encode()).hexdigest(),
        "source_sha256": "a" * 64, "source_row": 1,
        "explanations": {"explanation": "PRIVATE-P20-EXPLANATION"},
        "source_path": "PRIVATE-P20-PATH",
    }
    annotations = [annotation]
    if mode == "missing":
        annotations = []
    elif mode == "boolean":
        values["green_binary"] = "false"
    elif mode == "missing_key":
        del values["renewable_energy"]
    elif mode == "extra_key":
        values["invented"] = False
    elif mode == "row_float":
        annotation["source_row"] = 1.0
    elif mode == "row_string":
        annotation["source_row"] = "1"
    elif mode == "source_hash":
        annotation["source_sha256"] = "A" * 64
    elif mode == "status":
        annotation["status"] = "approved"
    elif mode == "positive_list":
        annotation["labels"] = ["green_binary"]
    elif mode == "values_array":
        annotation["values"] = []
    elif mode == "values_scalar":
        annotation["values"] = True
    elif mode == "body_hash":
        annotation["body_sha256"] = "b" * 64
    elif mode == "duplicate":
        annotations.append(deepcopy(annotation))
    elif mode == "stale":
        annotation["version_id"] = "old-source-version"
    return item.model_copy(update={"annotations": annotations})


@pytest.fixture(scope="module")
def label_db(isolated_db):
    if os.environ.get("OBS_RUN_SOCIAL_LABEL_INTEGRATION") != "1":
        pytest.skip("Explicit source-state integration opt-in is absent")
    db = isolated_db
    db.receipt["schema_version"] = "social-source-state-exploration-sql-v1"
    db.receipt["limits"].append("Fabricated source states and trusted scopes only; no real posts admitted or semantics evaluated.")
    positives = [source_post(f"p20-true-{n}", positive=True) for n in range(2)]
    negatives = [source_post(f"p20-false-{n:02}") for n in range(25)]
    unknowns = [source_post(f"p20-unknown-{mode}", mode=mode) for mode in BAD_MODES]
    rows = positives + negatives + unknowns
    outsiders = [source_post("p20-pending", countable=False, retrievable=False),
                 source_post("p20-inactive"), source_post("p20-native", dataset="native")]
    db.import_batch(ImportBatch(records=rows + outsiders))
    with db.connect() as conn:
        conn.execute("UPDATE records SET active=false WHERE record_id=%s", ("p20-inactive",))
    return {"db": db, "rows": rows, "ids": [row.record_id for row in rows],
            "true": {row.record_id for row in positives},
            "false": {row.record_id for row in negatives},
            "unknown": {row.record_id for row in unknowns}}


def scope(fixture, **changes):
    return Filters(dataset="social", record_ids=fixture["ids"], **changes)


def test_distribution_uses_all_current_records_and_keeps_false_distinct(label_db):
    f, db = label_db, label_db["db"]
    output = db.dashboard(scope(f), limit=1)
    distribution = output["social_historical_labels"]
    assert distribution["scheme"] == SCHEME and distribution["status"] == STATUS
    assert distribution["total"] == 41 and distribution["valid_annotation_records"] == 27
    assert distribution["unknown_annotation_records"] == 14
    assert len(output["page"]["rows"]) == 1 and output["page"]["total"] == 41
    assert [item["key"] for item in distribution["items"]] == list(SOCIAL_LABELS)
    for item in distribution["items"]:
        expected_true = 2 if item["key"] in {"green_binary", "decreasing_emissions"} else 0
        assert item["source_true"] == expected_true
        assert item["source_false"] == 27 - expected_true
        assert item["unknown"] == 14
        assert item["source_true"] + item["source_false"] + item["unknown"] == 41
    assert output["labels"]["total"] == 0  # social labels never inflate native distribution
    public = db.public_rows(scope(f))
    assert len(public) == 41 and all(len(row["social_historical_states"]) == 13 for row in public)
    assert "PRIVATE-P20" not in json.dumps(public)
    assert not any("body" in row or "explanations" in row for row in public)
    empty = db.dashboard(scope(f, accounts=["No such fixture account"]))["social_historical_labels"]
    assert empty["total"] == 0 and all(item["unknown"] == 0 for item in empty["items"])
    db.checked("source_state_full_aggregates", all_41_current_members=True, valid_27_unknown_14=True,
               all_false_not_missing=True, thirteen_independent_denominators=True, malformed_json_safe=True)


def test_all_39_states_match_independent_members_and_full_scope(label_db):
    f, db = label_db, label_db["db"]
    for key in SOCIAL_LABELS:
        for state in ("source_true", "source_false", "unknown"):
            expected = f["unknown"] if state == "unknown" else (
                f["true"] if state == "source_true" and key in {"green_binary", "decreasing_emissions"}
                else f["false"] if state == "source_false" and key in {"green_binary", "decreasing_emissions"}
                else set() if state == "source_true" else f["true"] | f["false"])
            filters = scope(f, labels=[social_state_id(key, state)], accounts=["Alpha"],
                            sponsors=["Company One"], platforms=["Twitter"],
                            date_from=date(2020, 3, 1), date_to=date(2020, 3, 1), include_unknown_dates=False)
            members = db.public_rows(filters)
            assert {row["record_id"] for row in members} == expected
            assert all(social_state_id(key, state) in row["social_historical_states"] for row in members)
    selected = scope(f, labels=[social_state_id("decreasing_emissions", "source_false")])
    whole = db.public_rows(selected)
    first, second = db.public_page(selected, limit=20), db.public_page(selected, offset=20, limit=20)
    assert first["total"] == second["total"] == 25
    assert first["rows"] + second["rows"] == whole
    union = scope(f, labels=[social_state_id("decreasing_emissions", "source_true"),
                            social_state_id("renewable_energy", "source_false")])
    assert {row["record_id"] for row in db.public_rows(union)} == f["true"] | f["false"]
    report = db.search_report("carbon beacon", scope(f, labels=[social_state_id("green_binary", "source_true")]), limit=10)
    assert {row.record_id for row in report["evidence"]} == f["true"]
    assert db.versioned_record(selected, "p20-true-0") is None
    assert db.versioned_record(selected, "p20-false-00")["record_id"] == "p20-false-00"
    db.checked("source_state_members_and_paging", all_39_states=True, full_25_members_two_pages=True,
               all_scope_dimensions_preserved=True, or_semantics=True, keyword_source_reads_scoped=True)


def test_source_version_detects_annotation_changes_without_pointer_change(label_db):
    db = label_db["db"]
    item = source_post("p20-fingerprint")
    db.import_batch(ImportBatch(records=[item]))
    filters = Filters(dataset="social", record_ids=[item.record_id])
    first = db.dashboard(filters)["social_historical_labels"]
    current = db.versioned_record(filters, item.record_id)
    changed = deepcopy(current["annotations"][0]["payload"])
    changed["values"]["renewable_energy"] = True
    changed["labels"] = ["renewable_energy"]
    with db.connect() as conn:
        conn.execute("UPDATE annotations SET payload=%s WHERE version_id=%s AND ordinal=0",
                     (Jsonb(changed), current["version_id"]))
    second = db.dashboard(filters)["social_historical_labels"]
    assert first["source_state_version"] != second["source_state_version"]
    assert db.versioned_record(filters, item.record_id)["version_id"] == current["version_id"]
    assert next(row for row in second["items"] if row["key"] == "renewable_energy")["source_true"] == 1
    with db.connect() as conn:
        conn.execute("UPDATE record_versions SET body=body || %s WHERE version_id=%s", (" Changed", current["version_id"]))
    third = db.dashboard(filters)["social_historical_labels"]
    assert third["source_state_version"] != second["source_state_version"]
    assert third["valid_annotation_records"] == 0 and third["unknown_annotation_records"] == 1
    assert all(row["unknown"] == 1 and row["source_false"] == 0 for row in third["items"])
    # jsonb can normalize exponent notation into an integer token. Compare
    # the Python detail view of stored JSON; don't claim to reconstruct the
    # original import's lexical numeric type from the database representation.
    numeric = source_post("p20-numeric-normalized")
    numeric.annotations[0]["source_row"] = 1e20
    db.import_batch(ImportBatch(records=[numeric]))
    numeric_filters = Filters(dataset="social", record_ids=[numeric.record_id])
    stored = db.versioned_record(numeric_filters, numeric.record_id)
    detail = social_annotation_details(stored)
    numeric_summary = db.dashboard(numeric_filters)["social_historical_labels"]
    assert numeric_summary["valid_annotation_records"] == int(detail["validation_state"] == "bound")
    db.checked("source_state_version_changes", annotation_only_edit_detected=True, current_pointer_unchanged=True,
               malformed_current_body_unknown=True, normalized_jsonb_matches_stored_python_view=True)


def test_aggregate_and_page_keep_one_snapshot_during_annotation_update(label_db, monkeypatch):
    db = label_db["db"]
    item = source_post("p20-concurrent-state")
    db.import_batch(ImportBatch(records=[item]))
    filters = Filters(dataset="social", record_ids=[item.record_id])
    first_version = db.social_source_state_version(filters)
    current = db.versioned_record(filters, item.record_id)
    changed = deepcopy(current["annotations"][0]["payload"])
    changed["values"]["green_binary"] = True
    changed["labels"] = ["green_binary"]
    original_connect, inserted = db.connect, False

    class Interleaved:
        def __enter__(self):
            self.conn = original_connect()
            self.conn.__enter__()
            return self

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

        def execute(self, statement, params=None):
            nonlocal inserted
            result = self.conn.execute(statement, params)
            if "AS unknown_dates" in statement and not inserted:
                with original_connect() as separate:
                    separate.execute("UPDATE annotations SET payload=%s WHERE version_id=%s AND ordinal=0",
                                     (Jsonb(changed), current["version_id"]))
                inserted = True
            return result

    with monkeypatch.context() as patch:
        patch.setattr(db, "connect", Interleaved)
        frozen = db.dashboard(filters)
    assert inserted
    distribution = frozen["social_historical_labels"]
    assert distribution["source_state_version"] == first_version
    assert next(row for row in distribution["items"] if row["key"] == "green_binary")["source_false"] == 1
    assert social_state_id("green_binary", "source_false") in frozen["page"]["rows"][0]["social_historical_states"]
    assert db.social_source_state_version(filters) != first_version
    db.checked("source_state_snapshot_consistency", concurrent_annotation_update=True,
               counts_page_and_source_hash_share_snapshot=True, next_snapshot_detects_change=True)


def test_mcp_distribution_and_member_scope_do_not_broaden_selection(label_db):
    f, db = label_db, label_db["db"]
    service = Service(Settings(show_source_links=False, web_search_enabled=False), db=db, rag=object())
    selected = scope(f, labels=[social_state_id("green_binary", "source_true")], accounts=["Alpha"])
    tools = ToolCatalog(service, selected)
    distribution = tools.call("record_statistics", {"group_by": "social_historical_labels"})
    assert distribution["status"] == "ok" and distribution["distribution"]["total"] == 2
    assert distribution["distribution"] == db.dashboard(selected, limit=1)["social_historical_labels"]
    assert distribution["filters"]["labels"] == selected.labels
    assert tools.call("record_statistics", {"filters": {"labels": []}})["collections"][0]["total"] == 2
    refused = tools.call("record_statistics", {"filters": {"labels": [social_state_id("green_binary", "source_false")]}})
    assert refused["status"] in {"clarify", "invalid_request"}
    assert tools.call("get_record", {"record_id": "p20-false-00"})["status"] == "clarify"
    assert tools.call("get_record_sources", {"record_id": "p20-true-0"})["status"] == "ok"
    for dataset in ("native", "all"):
        result = ToolCatalog(service, Filters(dataset=dataset)).call("record_statistics", {"group_by": "social_historical_labels"})
        assert result["status"] in {"clarify", "invalid_request"}
    assert tools.base_filters == selected
    rendered = json.dumps(distribution)
    assert "PRIVATE-P20" not in rendered and "example.invalid/" not in rendered
    db.checked("source_state_mcp_scope", full_source_distribution=True, empty_request_cannot_clear=True,
               different_state_refused=True, current_source_reader_scoped=True, links_hidden=True)
