"""Synthetic public record/MCP views of version-bound social source labels.

Recorded SQL shapes and independent fake membership are checked here, without
PostgreSQL, real source import, models or network. These are not label-quality
or real database-execution tests.
"""

import hashlib
import json
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
from flask import Flask
from test_social_accounts_tools import AccountDB

from observatory.config import Settings
from observatory.knowledge_graph import build_graph
from observatory.models import Filters
from observatory.records import RecordDetails, register_record_routes
from observatory.research_tools import ToolCatalog
from observatory.service import Service
from observatory.social_archive import SOCIAL_LABELS

PRIVATE = "PRIVATE-ANNOTATION-SOURCE-PATH"
SCHEME = "claims-social-export-v1"


def source_row(*, all_false=False):
    body = "Synthetic post about renewable energy — CO₂."
    digest = hashlib.sha256(body.encode()).hexdigest()
    values = {key: not all_false and key in {"green_binary", "renewable_energy"}
              for key in SOCIAL_LABELS}
    payload = {
        "version": SCHEME, "status": "historical_automatic_unverified",
        "values": values, "labels": [key for key in SOCIAL_LABELS if values[key]],
        "basis": "supplied_source_post_id_and_exact_body",
        "source_sha256": "a" * 64, "source_row": 7, "body_sha256": digest,
        "taxonomy_mapping": "source_keys_preserved_no_native_label_mapping",
        "explanations": {"green_explanation": "An old generated description, not a source quote.",
                         "fossil_fuel_explanation": "No explicit source code was selected.",
                         "explanation": None, "private": PRIVATE},
        "source": PRIVATE, "raw": {"private": PRIVATE}, "reviewer": PRIVATE,
    }
    return {
        "record_id": "s1", "version_id": "v-current", "dataset": "social",
        "title": "Synthetic post", "publisher": "", "sponsor": "Company affiliation",
        "platform": "X", "account": "Shared Name", "date": "2020-03-01",
        "date_basis": "source", "keyword": "energy", "body": body, "body_hash": digest,
        "retrievable": True, "countable": True, "active": True,
        "url": "https://example.org/post", "archive_url": "https://archive.example.org/post",
        "sponsor_basis": "company_affiliation_not_verified_paid_sponsor",
        "annotations": [{"ordinal": 0, "payload": payload}],
        "issues": [], "retrieval_ranges": [[0, len(body)]], "retrieval_end": None,
        "labels": [], "raw": {"source": PRIVATE}, "provenance": [{"source": PRIVATE}],
    }


class CurrentRecordTransport:
    """An explicit current-pointer oracle, not an SQL interpreter."""

    def __init__(self, rows):
        self.versions = {row["version_id"]: deepcopy(row) for row in rows}
        self.current = {row["record_id"]: row["version_id"] for row in rows}
        self.calls = []
        self.result = None

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, parameters):
        self.calls.append((statement, parameters))
        assert statement.lstrip().startswith("SELECT")
        assert "v.version_id=r.current_version" in statement
        assert "r.record_id=%s AND r.active AND (v.payload->>'countable')::boolean" in statement
        assert "FROM annotations a WHERE a.version_id=v.version_id" in statement
        assert "ORDER BY a.ordinal" in statement and "'ordinal',a.ordinal" in statement
        assert "v.payload->'raw'" not in statement and "v.payload->'provenance'" not in statement
        row = self.versions.get(self.current.get(parameters[0]))
        self.result = deepcopy(row) if row and row.get("active") and row.get("countable") else None
        return self

    def fetchone(self):
        return self.result


def details(tmp_path, row, *, links=True):
    db = CurrentRecordTransport([row])
    return RecordDetails(db, SimpleNamespace(show_source_links=links), tmp_path), db


def tools(row, *, base=None, links=True):
    db = AccountDB()
    db.rows = [deepcopy(row)]
    db.countable_counts = {"social": int(bool(row.get("active") and row.get("countable"))), "native": 0}
    service = Service(Settings(show_source_links=links), db=db, rag=object())
    return ToolCatalog(service, base or Filters(dataset="social")), db


def assert_history(value, *, state="bound", all_false=False):
    assert value["scheme"] == SCHEME and value["status"] == "historical_automatic_unverified"
    assert value["validation_state"] == state and len(value["values"]) == 13
    assert {item["key"] for item in value["values"]} == set(SOCIAL_LABELS)
    if state != "bound":
        assert all(item["value"] is None and item["state"] == "unknown" for item in value["values"])
        assert value["explanations"] == [] and "source_sha256" not in value["provenance"]
    elif all_false:
        assert all(item["value"] is False and item["state"] == "source_false" for item in value["values"])
    else:
        by_key = {item["key"]: item for item in value["values"]}
        assert by_key["renewable_energy"]["state"] == "source_true"
        assert by_key["fossil_fuel_binary"]["state"] == "source_false"
    assert "not reviewed" in value["note"] and "native" in value["note"]
    assert PRIVATE not in json.dumps(value)


@pytest.mark.parametrize("all_false", [False, True])
def test_record_json_presents_source_values_without_exposing_raw_annotations(tmp_path, all_false):
    row = source_row(all_false=all_false)
    reader, db = details(tmp_path, row)
    app = Flask(__name__)
    register_record_routes(app, reader)
    response = app.test_client().get("/api/records/s1")
    assert response.status_code == 200
    result = response.get_json()
    assert result["record_id"] == "s1" and result["version_id"] == "v-current"
    assert result["body"] == row["body"] and result["body_hash"] == row["body_hash"]
    assert "post text" in result["body_note"] and "complete article" not in result["body_note"]
    assert_history(result["social_historical_annotation"], all_false=all_false)
    assert len(db.calls) == 1 and db.calls[0][1] == ("s1",)
    assert "annotations" not in result and "raw" not in result and "provenance" not in result
    assert PRIVATE not in response.get_data(as_text=True)


def test_record_json_reads_new_current_body_and_cannot_reuse_previous_source_codes(tmp_path):
    original = source_row()
    reader, db = details(tmp_path, original)
    updated = {**deepcopy(original), "version_id": "v-next", "body": "A changed current post."}
    updated["body_hash"] = hashlib.sha256(updated["body"].encode()).hexdigest()
    db.versions["v-next"] = updated
    db.current["s1"] = "v-next"
    result = reader.get("s1")
    assert result["version_id"] == "v-next" and result["body"] == updated["body"]
    assert_history(result["social_historical_annotation"], state="invalid")
    assert original["version_id"] in db.versions  # The old synthetic version remains untouched.


@pytest.mark.parametrize("flag", ["active", "countable"])
def test_record_json_rejects_inactive_and_unadmitted_rows_before_projection(tmp_path, flag):
    row = source_row()
    row[flag] = False
    reader, db = details(tmp_path, row)
    app = Flask(__name__)
    register_record_routes(app, reader)
    assert app.test_client().get("/api/records/s1").status_code == 404
    assert len(db.calls) == 1


@pytest.mark.parametrize("links", [False, True])
def test_record_and_mcp_source_link_toggle_retains_safe_history(tmp_path, links):
    row = source_row()
    reader, _ = details(tmp_path, row, links=links)
    item = reader.get("s1")
    catalog, _ = tools(row, links=links)
    result = catalog.call("get_record_sources", {"record_id": "s1"})
    assert result["status"] == "ok"
    assert_history(item["social_historical_annotation"])
    assert_history(result["social_historical_annotation"])
    assert item["url"] == result["record"]["url"] == (row["url"] if links else "")
    assert item["archive_url"] == result["record"]["archive_url"] == (row["archive_url"] if links else "")
    assert len(result["source_artifacts"]) == len(result["source_relations"]) == (2 if links else 0)
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("mutation,expected", [
    ("missing", "missing"), ("bad_boolean", "invalid"), ("duplicate", "ambiguous"),
    ("old_body", "invalid"), ("bad_current_hash", "body_mismatch"),
])
def test_record_and_mcp_unusable_annotations_are_unknown_without_positive_annotation(tmp_path, mutation, expected):
    row = source_row()
    if mutation == "missing":
        row["annotations"] = []
    elif mutation == "bad_boolean":
        row["annotations"][0]["payload"]["values"]["green_binary"] = "false"
    elif mutation == "duplicate":
        row["annotations"].append(deepcopy(row["annotations"][0]))
    elif mutation == "old_body":
        row["annotations"][0]["payload"]["body_sha256"] = "b" * 64
    else:
        row["body_hash"] = "b" * 64
    reader, _ = details(tmp_path, row)
    history = reader.get("s1")["social_historical_annotation"]
    assert_history(history, state=expected)
    catalog, _ = tools(row)
    result = catalog.call("get_record_sources", {"record_id": "s1"})
    assert result["status"] == "ok" and result["annotations"] == []
    assert result["social_historical_annotation"] == history
    assert result["warnings"][0]["code"] == "social_annotations_unverified"
    if mutation == "bad_current_hash":
        assert result["source_refs"][0]["body_hash"] == ""
        assert history["provenance"]["body_hash"] == ""


@pytest.mark.parametrize("scope", [Filters(dataset="social"), Filters(dataset="all")])
@pytest.mark.parametrize("all_false", [False, True])
def test_mcp_bound_source_labels_are_one_safe_historical_projection(scope, all_false):
    row = source_row(all_false=all_false)
    before = deepcopy(row)
    catalog, db = tools(row, base=scope)
    result = catalog.call("get_record_sources", {"record_id": "s1"})
    assert result["status"] == "ok"
    assert_history(result["social_historical_annotation"], all_false=all_false)
    assert result["annotations"] == [result["social_historical_annotation"]]
    assert result["social_historical_annotation"]["provenance"] == {
        "record_id": "s1", "version_id": "v-current", "body_hash": row["body_hash"],
        "source_sha256": "a" * 64, "source_row": 7,
    }
    assert result["claims_status"] == "historical_unverified"
    assert result["record"]["company_affiliation"] == "Company affiliation"
    assert "not proof of paid sponsorship" in result["record"]["relation_note"]
    assert db.rows == [before] and scope == Filters(dataset=scope.dataset)


@pytest.mark.parametrize("scope,expected", [
    (Filters(dataset="native"), "clarify"),
    (Filters(dataset="social", record_ids=["another"]), "clarify"),
    (Filters(dataset="social", accounts=["Other Account"]), "clarify"),
    (Filters(dataset="social", platforms=["YouTube"]), "clarify"),
    (Filters(dataset="social", sponsors=["Other Company"]), "clarify"),
    (Filters(dataset="social", date_from=date(2021, 1, 1)), "clarify"),
    (Filters(dataset="social", include_unknown_dates=False), "ok"),
])
def test_mcp_source_history_keeps_collection_id_account_company_platform_and_date_scope(scope, expected):
    row = source_row()
    catalog, db = tools(row, base=scope)
    if scope.dataset == "native":
        db.countable_counts["native"] = 1  # Another native record may exist; it cannot expose this social record.
    before = scope.model_dump(mode="json")
    result = catalog.call("get_record_sources", {"record_id": "s1"})
    assert result["status"] == expected
    if expected != "ok":
        assert "social_historical_annotation" not in result
    assert scope.model_dump(mode="json") == before


@pytest.mark.parametrize("include_unknown", [False, True])
def test_mcp_missing_date_history_respects_unknown_date_selection(include_unknown):
    row = source_row()
    row.update(date=None, date_basis="missing")
    scope = Filters(dataset="social", include_unknown_dates=include_unknown)
    catalog, _ = tools(row, base=scope)
    result = catalog.call("get_record_sources", {"record_id": "s1"})
    assert result["status"] == ("ok" if include_unknown else "clarify")
    if include_unknown:
        assert_history(result["social_historical_annotation"])
    else:
        assert "social_historical_annotation" not in result and "annotations" not in result
    assert scope.include_unknown_dates is include_unknown


@pytest.mark.parametrize("flag", ["countable", "active"])
def test_mcp_unadmitted_or_withdrawn_social_rows_cannot_expose_source_labels(flag):
    row = source_row()
    row[flag] = False
    catalog, db = tools(row)
    result = catalog.call("get_record_sources", {"record_id": "s1"})
    assert result["status"] == "unavailable"
    assert "social_historical_annotation" not in result and not db.reads


def test_native_record_and_mcp_graph_source_behavior_remain_separate(tmp_path):
    row = source_row()
    row.update(dataset="native", publisher="Native outlet", platform="", account="")
    row["annotations"] = []
    reader, _ = details(tmp_path, row)
    item = reader.get("s1")
    assert "social_historical_annotation" not in item and "complete article" in item["body_note"]
    catalog, db = tools(row, base=Filters(dataset="native"))
    db.countable_counts = {"native": 1, "social": 0}
    result = catalog.call("get_record_sources", {"record_id": "s1"})
    assert result["status"] == "ok" and "social_historical_annotation" not in result
    expected = build_graph([row], links_enabled=True)
    assert result["annotations"] == [node for node in expected["nodes"] if node["type"] in ("Annotation", "Label")]
    assert result["warnings"] == expected["warnings"]


def test_source_history_does_not_add_tools_or_authorize_content_membership():
    catalog, _ = tools(source_row())
    definitions = {tool["name"]: tool for tool in catalog.definitions()}
    assert len(definitions) == 10 and "find_records" in definitions and "get_record_sources" in definitions
    description = definitions["get_record_sources"]["description"]
    assert "never reviewed greenwashing findings" in description
    invalid = catalog.call("get_record_sources", {"record_id": "s1", "label_state": "reviewed"})
    assert invalid["status"] == "invalid_request" and "social_historical_annotation" not in invalid
