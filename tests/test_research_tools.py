"""Read-only tool behavior on independent source rows, with no model calls."""

import hashlib
import json
from collections import Counter
from copy import deepcopy
from datetime import date

import pytest

from observatory.config import Settings
from observatory.knowledge_graph import build_graph
from observatory.models import Evidence, Filters
from observatory.research_tools import ToolCatalog
from observatory.service import Service


def source(record_id, publisher, sponsor, body="Company discusses emissions.", published="2020-03-01"):
    return {"record_id": record_id, "version_id": "version-" + record_id, "dataset": "native",
            "title": "Title " + record_id, "publisher": publisher, "sponsor": sponsor,
            "body": body, "body_hash": hashlib.sha256(body.encode()).hexdigest(),
            "date": published, "retrievable": bool(body), "url": "https://example.org/" + record_id,
            "archive_url": "file:///private/archive.pdf", "platform": "", "keyword": "energy",
            "raw": {"token": "never-public"}, "provenance": [{"path": "C:/private/source"}],
            "annotations": [], "issues": []}


class SourceDB:
    def __init__(self):
        self.rows = [source("r1", "The Washington Post", "exxonmobil"),
                     source("r2", "The Washington Post", "bp", body="", published=None),
                     source("r3", "The New York Times", "exxonmobil", published="2021-04-01"),
                     source("r4", "The Washington Post", "ExxonMobil", published="2020-08-01")]
        self.reads = []
        self.version = "dataset-version"
        self.invalid_evidence = False
        self.loaded = True

    def health(self):
        return {"status": "ok", "record_counts": {"native": len(self.rows)} if self.loaded else {},
                "data_version": self.version}

    def select(self, filters):
        self.reads.append(filters.model_copy(deep=True))
        result = []
        for row in deepcopy(self.rows):
            if filters.dataset not in ("all", row["dataset"]):
                continue
            if any(getattr(filters, dimension) and row.get(dimension[:-1]) not in getattr(filters, dimension)
                   for dimension in ("publishers", "sponsors", "platforms", "keywords")):
                continue
            if filters.record_ids and row["record_id"] not in filters.record_ids:
                continue
            if row["date"]:
                when = date.fromisoformat(row["date"])
                if filters.date_from and when < filters.date_from or filters.date_to and when > filters.date_to:
                    continue
            elif not filters.include_unknown_dates:
                continue
            result.append(row)
        return result

    def facets(self, dataset):
        rows = self.select(Filters(dataset=dataset))
        return {**{dimension: sorted({row.get(dimension[:-1]) or "(Unknown)" for row in rows})
                   for dimension in ("publishers", "sponsors", "platforms", "keywords")}, "labels": []}

    def dashboard(self, filters, offset=0, limit=20, sort_by="date", descending=True):
        rows = self.select(filters)
        stats = {"total": len(rows), "retrievable": sum(row["retrievable"] for row in rows),
                 "unknown_dates": sum(row["date"] is None for row in rows)}
        for dimension in ("publishers", "sponsors", "platforms", "keywords"):
            stats[dimension] = [{"name": name, "count": count} for name, count in
                                Counter(row.get(dimension[:-1]) or "(Unknown)" for row in rows).items()]
        return {"stats": stats, "page": {"rows": rows[offset:offset + limit], "total": len(rows), "offset": offset}}

    def public_rows(self, filters):
        return self.select(filters)

    def knowledge_page(self, filters, offset=0, limit=5):
        assert filters.dataset == "native"
        rows = self.select(filters)
        offset = min(offset, max(0, ((len(rows) - 1) // limit) * limit))
        return {"rows": rows[offset:offset + limit], "total": len(rows), "offset": offset, "limit": limit}

    def search_report(self, question, filters, limit=5):
        rows = [row for row in self.select(filters) if row["retrievable"]][:limit]
        return {"evidence": [Evidence(evidence_id="e" + row["record_id"], record_id=row["record_id"],
                    version_id="wrong" if self.invalid_evidence else row["version_id"], dataset=row["dataset"],
                    title=row["title"], publisher=row["publisher"], sponsor=row["sponsor"],
                    text=row["body"], start=0, end=len(row["body"]), url=row["url"])
                    for row in rows],
                "diagnostics": {"status": "ok", "terms": ["emissions"], "private_sql": "never-public"}}


def make_catalog(base=None, *, links=True):
    db = SourceDB()
    service = Service(Settings(show_source_links=links), db=db, rag=object())
    return ToolCatalog(service, base or Filters()), db


def test_catalog_strict_transport_schemas_are_bounded_and_closed():
    catalog, _ = make_catalog()
    definitions = catalog.definitions()
    assert len(definitions) == 7

    def verify(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node.get("properties", {}))
            assert "default" not in node
            for item in node.values():
                verify(item)
        elif isinstance(node, list):
            for item in node:
                verify(item)

    for definition in definitions:
        assert definition["strict"] is True
        verify(definition["parameters"])


@pytest.mark.parametrize("name,args", [
    ("run_sql", {"sql": "drop table records"}),
    ("record_statistics", {"sql": "select *"}),
    ("record_statistics", {"filters": {"path": "C:/private"}}),
    ("get_record", {"record_id": "../../secret"}),
    ("get_record", {"record_id": "r1", "body_limit": 12001}),
    ("get_record", {"record_id": "r1", "body_start": -1}),
    ("search_records", {"query": "a", "limit": True}),
    ("get_graph_neighborhood", {"limit": 6}),
    ("get_graph_neighborhood", {"offset": -1}),
    ("resolve_entity", {"query": "BP", "entity_type": "company"}),
])
def test_invalid_requests_never_read_database(name, args):
    catalog, db = make_catalog()
    assert catalog.call(name, args)["status"] == "invalid_request"
    assert db.reads == []


def test_counts_include_unsearchable_records_and_full_group_lists():
    catalog, db = make_catalog()
    result = catalog.call("record_statistics", {"filters": {"publishers": ["The Washington Post"]}, "group_by": "sponsors"})
    assert result["status"] == "ok"
    assert result["collections"] == [{"dataset": "native", "total": 3, "retrievable": 2, "unknown_dates": 1}]
    assert {item["name"]: item["count"] for item in result["groups"]} == {"exxonmobil": 1, "ExxonMobil": 1, "bp": 1}
    assert len(result["records"]) == 3
    assert "raw" not in json.dumps(result)
    assert "never-public" not in json.dumps(result)
    assert "C:/private" not in json.dumps(result)
    assert all(read.dataset == "native" for read in db.reads)


def test_filters_cannot_widen_empty_arrays_all_dataset_or_dates():
    original = Filters(publishers=["The Washington Post"], record_ids=["r1", "r2"],
                       date_from=date(2020, 1, 1), date_to=date(2020, 12, 31), include_unknown_dates=False)
    catalog, _ = make_catalog(original)
    result = catalog.call("record_statistics", {"filters": {"dataset": "all", "publishers": [],
        "record_ids": ["r1"], "date_from": "2010-01-01", "date_to": "2030-01-01", "include_unknown_dates": True}})
    assert result["status"] == "ok"
    assert result["filters"]["publishers"] == ["The Washington Post"]
    assert result["filters"]["record_ids"] == ["r1"]
    assert result["filters"]["dataset"] == "native"
    assert result["filters"]["date_from"] == "2020-01-01"
    assert result["filters"]["date_to"] == "2020-12-31"
    assert result["filters"]["include_unknown_dates"] is False
    original.publishers.clear()
    assert catalog.base_filters.publishers == ["The Washington Post"]
    exposed = catalog.base_filters
    exposed.publishers.clear()
    assert catalog.base_filters.publishers == ["The Washington Post"]


@pytest.mark.parametrize("filters", [
    {"dataset": "social"}, {"publishers": ["The New York Times"]},
    {"record_ids": ["r3"]}, {"date_from": "2030-01-01"},
    {"record_ids": ["r1", "r3"]},
])
def test_disjoint_scope_requires_clarification(filters):
    catalog, _ = make_catalog(Filters(publishers=["The Washington Post"], record_ids=["r1"], date_to=date(2020, 12, 31)))
    assert catalog.call("record_statistics", {"filters": filters})["status"] == "clarify"


def test_unknown_source_name_is_not_silently_zero_or_substituted():
    catalog, _ = make_catalog()
    result = catalog.call("record_statistics", {"filters": {"publishers": ["NYT"]}})
    assert result["status"] == "clarify"
    assert "collections" not in result


def test_date_scope_without_unknown_override_excludes_unknown_dates():
    catalog, _ = make_catalog()
    result = catalog.call("record_statistics", {"filters": {"date_from": "2020-01-01", "date_to": "2020-12-31"}})
    assert result["collections"][0]["total"] == 2
    assert result["filters"]["include_unknown_dates"] is False


def test_explicit_date_constraint_cannot_include_unknown_dated_records():
    catalog, _ = make_catalog()
    result = catalog.call("record_statistics", {"filters": {"date_from": "2020-01-01", "date_to": "2020-12-31", "include_unknown_dates": True}})
    assert result["collections"][0]["total"] == 2
    assert result["collections"][0]["unknown_dates"] == 0
    assert result["filters"]["include_unknown_dates"] is False


def test_entity_resolution_case_variants_are_separate_candidates_not_merged():
    catalog, _ = make_catalog()
    result = catalog.call("resolve_entity", {"query": "ExxonMobil", "entity_type": "sponsor"})
    assert result["status"] == "clarify"
    assert result["candidate_count"] == 2
    assert {candidate["source_value"] for candidate in result["candidates"]} == {"exxonmobil", "ExxonMobil"}
    assert len({candidate["entity_id"] for candidate in result["candidates"]}) == 2
    graph = build_graph(SourceDB().rows)
    assert {candidate["entity_id"] for candidate in result["candidates"]} <= {node["id"] for node in graph["nodes"]}


def test_outlet_display_article_lookup_returns_exact_source_spelling():
    catalog, _ = make_catalog()
    result = catalog.call("resolve_entity", {"query": "Washington Post", "entity_type": "publisher"})
    assert result["status"] == "ok"
    assert result["candidates"][0]["source_value"] == "The Washington Post"
    assert catalog.call("resolve_entity", {"query": "Imaginary News", "entity_type": "publisher"})["status"] == "clarify"


def test_entity_context_respects_names_scope_and_marks_truncation():
    catalog, _ = make_catalog(Filters(sponsors=["bp"]))
    result = catalog.entity_context(limit=1)
    assert result["sponsors"] == [{"value": "bp", "display": "BP"}]
    assert result["truncated"]


def test_source_details_private_payloads_and_disabled_links_do_not_escape():
    catalog, db = make_catalog(links=False)
    db.rows[0]["annotations"] = [{"ordinal": 0, "payload": {"version": "claims-calibrated", "labels": ["false_solutions"],
        "path": "C:/private", "explanation": "never-public", "body_sha256": db.rows[0]["body_hash"]}}]
    result = catalog.call("get_record_sources", {"record_id": "r1"})
    serialized = json.dumps(result)
    assert result["status"] == "ok"
    assert result["record"]["url"] == result["record"]["archive_url"] == ""
    assert result["source_artifacts"] == []
    assert "never-public" not in serialized and "C:/private" not in serialized
    assert result["claims_status"] == "historical_unverified"
    assert result["attachments_status"] == "adapter_not_connected"


def test_record_excerpt_unicode_offsets_and_hash_match_unchanged_source():
    catalog, db = make_catalog()
    body = "甲😀Company discusses emissions.乙"
    db.rows[0]["body"] = body
    db.rows[0]["body_hash"] = hashlib.sha256(body.encode()).hexdigest()
    result = catalog.call("get_record", {"record_id": "r1", "body_start": 1, "body_limit": 5})
    assert result["body"]["text"] == body[1:6]
    assert result["body"]["hash_status"] == "matched"
    assert result["body"]["truncated"]
    assert result["source_refs"][0]["body_hash"] == db.rows[0]["body_hash"]
    assert result["source_refs"][0]["version_id"] == "version-r1"


def test_record_hash_mismatch_and_out_of_range_offsets_fail_closed():
    catalog, db = make_catalog()
    assert catalog.call("get_record", {"record_id": "r1", "body_start": 999})["status"] == "clarify"
    db.rows[0]["body_hash"] = "0" * 64
    result = catalog.call("get_record", {"record_id": "r1"})
    assert result["status"] == "unavailable"
    assert "body" not in result


def test_get_record_sources_enforces_all_other_scope_dimensions():
    catalog, _ = make_catalog(Filters(publishers=["The New York Times"]))
    assert catalog.call("get_record_sources", {"record_id": "r1"})["status"] == "clarify"


def test_search_passages_validate_original_hash_version_and_offsets():
    catalog, db = make_catalog()
    result = catalog.call("search_records", {"query": "emissions", "limit": 2})
    assert result["status"] == "ok"
    assert len(result["evidence"]) == 2
    assert all(ref["verification_status"] == "exact_character_match" for ref in result["source_refs"])
    assert "private_sql" not in result["diagnostics"]
    db.invalid_evidence = True
    result = catalog.call("search_records", {"query": "emissions", "limit": 2})
    assert result["evidence"] == []
    assert result["rejected_evidence"] == 2


def test_graph_is_bounded_paged_and_keeps_source_provenance():
    catalog, _ = make_catalog()
    result = catalog.call("get_graph_neighborhood", {"offset": 1, "limit": 1})
    assert result["status"] == "ok"
    assert result["graph"]["coverage"]["shown_records"] == 1
    assert result["graph"]["coverage"]["total_records"] == 4
    assert result["graph"]["records"][0]["record_id"] == "r2"
    assert all(edge["provenance"]["version_id"] == "version-r2" for edge in result["graph"]["edges"])
    assert result["coverage"] == "paged_neighborhood_not_complete_graph"


def test_missing_dataset_and_all_unloaded_are_not_advertising_zero():
    catalog, db = make_catalog(Filters(dataset="social"))
    assert catalog.call("record_statistics", {})["status"] == "unavailable"
    catalog, db = make_catalog(Filters(dataset="all"))
    result = catalog.call("record_statistics", {})
    assert result["status"] == "ok"
    assert [item["dataset"] for item in result["collections"]] == ["native"]
    assert result["missing_datasets"] == ["social"]
    db.loaded = False
    assert catalog.call("record_statistics", {})["status"] == "unavailable"


def test_schema_available_without_db_and_never_promotes_history_to_verdict():
    catalog = ToolCatalog(object(), Filters())
    result = catalog.call("get_graph_schema", {})
    assert result["status"] == "ok"
    assert result["adapters"]["external_fact_check"] == "not_connected"
    assert result["claims_status"] == "historical_annotations_are_not_verified_greenwashing"


def test_driver_exception_does_not_leak_credentials_or_sql():
    catalog, db = make_catalog()

    def fail(*args, **kwargs):
        raise RuntimeError("postgres://secret:token@private-host SELECT private_path")

    db.dashboard = fail
    result = catalog.call("record_statistics", {})
    assert result["status"] == "unavailable"
    assert "secret" not in json.dumps(result)
    assert "SELECT" not in json.dumps(result)


def test_version_change_during_read_abstains_from_publishing_old_result():
    catalog, db = make_catalog()
    dashboard = db.dashboard

    def change(*args, **kwargs):
        result = dashboard(*args, **kwargs)
        db.version = "new-version"
        return result

    db.dashboard = change
    result = catalog.call("record_statistics", {})
    assert result["status"] == "unavailable"
    assert "collections" not in result
