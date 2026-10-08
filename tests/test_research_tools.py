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
        return {"status": "ok", "record_counts": dict(Counter(row["dataset"] for row in self.rows)) if self.loaded else {},
                "data_version": self.version}

    def select(self, filters):
        self.reads.append(filters.model_copy(deep=True))
        result = []
        for row in deepcopy(self.rows):
            if not row.get("active", True) or not row.get("countable", True):
                continue
            if filters.dataset not in ("all", row["dataset"]):
                continue
            if any(getattr(filters, dimension) and row.get(dimension[:-1]) not in getattr(filters, dimension)
                   for dimension in ("publishers", "sponsors", "platforms", "keywords")):
                continue
            if filters.record_ids and row["record_id"] not in filters.record_ids:
                continue
            if filters.labels and not set(filters.labels).intersection(row.get("labels", [])):
                continue
            if filters.date_presence == "known" and not row["date"] or filters.date_presence == "missing" and row["date"]:
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

    def versioned_record(self, filters, record_id):
        if filters.record_ids and record_id not in filters.record_ids:
            return None
        scoped = filters.model_copy(update={"record_ids": [record_id]}, deep=True)
        rows = self.select(scoped)
        return rows[0] if rows else None

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
                "diagnostics": {"status": "available", "terms": ["emissions"], "private_sql": "never-public"}}


def make_catalog(base=None, *, links=True):
    db = SourceDB()
    service = Service(Settings(show_source_links=links), db=db, rag=object())
    return ToolCatalog(service, base or Filters()), db


def test_catalog_strict_transport_schemas_are_bounded_and_closed():
    catalog, _ = make_catalog()
    definitions = catalog.definitions()
    assert len(definitions) == 10

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
    result = catalog.call("record_statistics", {"filters": {"publishers": ["Imaginary News"]}})
    assert result["status"] == "clarify"
    assert "collections" not in result
    assert not catalog.alias_resolutions


def test_unique_alias_in_filters_maps_to_source_value_with_audit():
    # Models pass names such as "NYT" straight into filters (2026-10-01 S06/S14/S17/S24).
    catalog, _ = make_catalog()
    result = catalog.call("record_statistics", {"filters": {"publishers": ["NYT"]}})
    assert result["collections"][0]["total"] == 1
    assert result["filters"]["publishers"] == ["The New York Times"]
    assert result["alias_resolutions"] == [
        {"field": "publishers", "requested": "NYT", "source_value": "The New York Times"}]


def test_alias_matching_several_source_spellings_is_never_merged():
    # The fixture stores both "exxonmobil" and "ExxonMobil"; neither is chosen for "Exxon Mobil".
    catalog, _ = make_catalog()
    result = catalog.call("record_statistics", {"filters": {"sponsors": ["Exxon Mobil"]}})
    assert result["status"] == "clarify"
    assert not catalog.alias_resolutions


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


@pytest.mark.parametrize("query", ["NYT", "nytimes.com", "NY Times", "纽约时报"])
def test_publisher_aliases_resolve_to_the_exact_source_spelling(query):
    # Same alias table as the rule planner, so both answer paths agree.
    catalog, _ = make_catalog()
    result = catalog.call("resolve_entity", {"query": query, "entity_type": "publisher"})
    assert result["status"] == "ok"
    assert [c["source_value"] for c in result["candidates"]] == ["The New York Times"]


def test_unknown_filter_message_is_user_facing():
    catalog, _ = make_catalog()
    result = catalog.call("record_statistics", {"filters": {"publishers": ["Imaginary News"]}})
    assert result["status"] == "clarify"
    assert "resolve_entity" not in result["message"]


def test_alias_and_exact_name_select_the_same_records():
    alias, _ = make_catalog()
    exact, _ = make_catalog()
    by_alias = alias.call("record_statistics", {"filters": {"publishers": ["NYT"]}})
    by_name = exact.call("record_statistics", {"filters": {"publishers": ["The New York Times"]}})
    assert by_alias["filters"] == by_name["filters"]
    assert by_alias["collections"] == by_name["collections"]
    assert "alias_resolutions" not in by_name


def test_publisher_alias_is_not_applied_to_the_sponsor_field():
    catalog, _ = make_catalog()
    result = catalog.call("record_statistics", {"filters": {"sponsors": ["NYT"]}})
    assert result["status"] == "clarify"
    assert not catalog.alias_resolutions


def test_alias_audit_belongs_to_one_request():
    first, _ = make_catalog()
    first.call("record_statistics", {"filters": {"publishers": ["NYT"]}})
    second, _ = make_catalog()
    result = second.call("record_statistics", {"filters": {"publishers": ["The Washington Post"]}})
    assert first.alias_resolutions and not second.alias_resolutions
    assert "alias_resolutions" not in result


def test_missing_publication_dates_can_be_counted_on_their_own():
    # "How many of these ads have no publication date?" was answered with the
    # whole collection (2026-10-01 U19: 263 instead of 22).
    catalog, _ = make_catalog()
    result = catalog.call("record_statistics", {"filters": {"date_presence": "missing"}})
    assert result["status"] == "ok"
    assert result["filters"]["date_presence"] == "missing"
    assert result["collections"][0]["total"] == 1


def test_missing_dates_cannot_be_read_when_the_selection_excludes_them():
    catalog, _ = make_catalog(Filters(include_unknown_dates=False))
    result = catalog.call("record_statistics", {"filters": {"date_presence": "missing"}})
    assert result["status"] == "clarify"
    both = make_catalog()[0].call("record_statistics", {"filters": {
        "date_presence": "missing", "date_from": "2020-01-01", "date_to": "2020-12-31"}})
    assert both["status"] == "clarify"


@pytest.mark.parametrize("base", [
    Filters(include_inferred_dates=False, date_presence="known", include_unknown_dates=False,
            date_from=date(2020, 1, 1), date_to=date(2020, 12, 31)),
    Filters(include_inferred_dates=True, date_presence="missing"),
])
def test_tools_cannot_change_the_trusted_publication_date_basis(base):
    # Both directions can admit records excluded by the trusted date selection.
    catalog, db = make_catalog(base)
    result = catalog.call("record_statistics", {"filters": {
        "include_inferred_dates": not base.include_inferred_dates}})
    assert result["status"] == "clarify"
    assert "publication-date basis" in result["message"]
    assert catalog.base_filters == base
    assert not db.reads


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_tools_preserve_omitted_or_matching_publication_date_basis(enabled, explicit):
    base = Filters(include_inferred_dates=enabled)
    catalog, _ = make_catalog(base)
    arguments = {"filters": {"include_inferred_dates": enabled}} if explicit else {}
    result = catalog.call("record_statistics", arguments)
    assert result["status"] == "ok"
    assert result["filters"]["include_inferred_dates"] is enabled
    assert catalog.base_filters == base


def social_catalog(base=None, *, links=True):
    catalog, db = make_catalog(base or Filters(dataset="social"), links=links)
    row = source("junkipedia:42", "", "ExxonMobil", body="甲😀We support lower emissions.乙")
    row.update(dataset="social", platform="Twitter", account="ExxonMobil account",
               sponsor_basis="company_affiliation_not_verified_paid_sponsor",
               url="https://twitter.com/exxonmobil/status/42",
               archive_url="https://www.junkipedia.org/posts/42",
               annotations=[{"ordinal": 0, "payload": {"version": "claims-social-export-v1",
                   "labels": ["green", "climate_science"], "values": {"green": True},
                   "body_sha256": row["body_hash"], "explanations": "never-public",
                   "source_path": "C:/private/social"}}])
    db.rows.append(row)
    return catalog, db, row


@pytest.mark.parametrize("dataset", ["social", "all"])
def test_social_versioned_record_preserves_scope_source_and_unicode(dataset):
    base = Filters(dataset=dataset, sponsors=["ExxonMobil"], platforms=["Twitter"],
                   keywords=["energy"], record_ids=["junkipedia:42"],
                   date_from=date(2020, 1, 1), date_to=date(2020, 12, 31))
    catalog, db, row = social_catalog(base)
    result = catalog.call("get_record", {"record_id": row["record_id"], "body_start": 1, "body_limit": 5})
    assert result["status"] == "ok"
    assert result["body"]["text"] == row["body"][1:6]
    assert result["source_refs"][0]["version_id"] == row["version_id"]
    assert result["source_refs"][0]["body_hash"] == row["body_hash"]
    assert result["record"]["dataset"] == "social"
    assert result["record"]["platform"] == "Twitter"
    assert result["record"]["account"] == row["account"]
    assert result["record"]["company_affiliation"] == "ExxonMobil"
    assert result["record"]["sponsor_basis"] == row["sponsor_basis"]
    assert catalog.base_filters == base
    assert all(read.dataset == dataset for read in db.reads)


@pytest.mark.parametrize("tool", ["get_record", "get_record_sources"])
@pytest.mark.parametrize("base", [
    Filters(dataset="native"),
    Filters(dataset="social", record_ids=["another-post"]),
    Filters(dataset="social", date_from=date(2021, 1, 1)),
    Filters(dataset="social", date_to=date(2019, 12, 31)),
    Filters(dataset="social", date_presence="missing"),
    Filters(dataset="social", publishers=["The Washington Post"]),
    Filters(dataset="social", sponsors=["bp"]),
    Filters(dataset="social", platforms=["Instagram"]),
    Filters(dataset="social", keywords=["another-keyword"]),
    Filters(dataset="social", labels=["claims-social-export-v1:green_binary:source_true"]),
])
def test_social_details_cannot_read_outside_any_active_filter(base, tool):
    catalog, _, row = social_catalog(base)
    assert catalog.call(tool, {"record_id": row["record_id"]})["status"] == "clarify"


@pytest.mark.parametrize("tool", ["get_record", "get_record_sources"])
@pytest.mark.parametrize("state", [{"countable": False}, {"active": False}])
@pytest.mark.parametrize("dataset", ["native", "social"])
def test_details_do_not_admit_pending_or_inactive_records(tool, state, dataset):
    if dataset == "social":
        catalog, _, row = social_catalog()
    else:
        catalog, db = make_catalog()
        row = db.rows[0]
    row.update(state)
    assert catalog.call(tool, {"record_id": row["record_id"]})["status"] == "clarify"


def test_social_details_reject_native_ids_and_missing_version():
    catalog, _, row = social_catalog()
    assert catalog.call("get_record", {"record_id": "r1"})["status"] == "clarify"
    row["version_id"] = ""
    assert catalog.call("get_record", {"record_id": row["record_id"]})["status"] == "clarify"


@pytest.mark.parametrize("links", [True, False])
def test_social_sources_are_direct_public_references_without_native_graph_or_private_labels(links, monkeypatch):
    catalog, _, row = social_catalog(links=links)
    monkeypatch.setattr("observatory.research_tools.build_graph", lambda *a, **kw: pytest.fail("social source reads must not build a native graph"))
    result = catalog.call("get_record_sources", {"record_id": row["record_id"]})
    assert result["status"] == "ok"
    assert len(result["source_artifacts"]) == (2 if links else 0)
    assert result["record"]["url"] == (row["url"] if links else "")
    assert result["record"]["archive_url"] == (row["archive_url"] if links else "")
    assert result["claims_status"] == "historical_unverified"
    assert result["annotations"] == []
    assert result["warnings"][0]["code"] == "social_annotations_unverified"
    assert all(ref["provenance"]["version_id"] == row["version_id"] for ref in result["source_relations"])
    serialized = json.dumps(result)
    assert "never-public" not in serialized and "C:/private" not in serialized
    projected = result["social_historical_annotation"]
    assert projected["scheme"] == "claims-social-export-v1"
    assert projected["validation_state"] == "invalid"
    assert len(projected["values"]) == 13
    assert all(item["state"] == "unknown" and item["value"] is None for item in projected["values"])
    assert projected["explanations"] == []
    if not links:
        assert "twitter.com" not in serialized and "junkipedia.org" not in serialized


@pytest.mark.parametrize("bad_url", ["file:///C:/private/social", "https://user:secret@example.org/post", "javascript:alert(1)"])
def test_social_sources_omit_unsafe_links_and_unknown_relation_basis(bad_url):
    catalog, _, row = social_catalog()
    row.update(url=bad_url, archive_url=bad_url, sponsor_basis="C:/private/never-public")
    result = catalog.call("get_record_sources", {"record_id": row["record_id"]})
    assert result["status"] == "ok"
    assert result["source_artifacts"] == [] and result["source_relations"] == []
    assert result["record"]["sponsor_basis"] == "not_recorded"
    assert result["record"]["company_affiliation"] == ""
    assert "never-public" not in json.dumps(result)


@pytest.mark.parametrize("dataset", ["social", "all"])
def test_social_search_now_validates_current_version_hash_offsets_and_dataset(dataset):
    catalog, _, row = social_catalog(Filters(dataset=dataset, record_ids=["junkipedia:42"]))
    result = catalog.call("search_records", {"query": "emissions"})
    assert len(result["evidence"]) == 1 and result["rejected_evidence"] == 0
    assert result["source_refs"][0]["verification_status"] == "exact_character_match"
    assert result["source_refs"][0]["body_hash"] == row["body_hash"]


@pytest.mark.parametrize("damage", [
    {"version_id": "stale-version"}, {"dataset": "native"}, {"dataset": "unexpected"},
    {"start": -1}, {"start": 1}, {"end": 999}, {"text": "fabricated text"},
    {"record_id": "r1"},
])
def test_social_search_rejects_injected_wrong_versions_collections_coordinates_and_ids(damage):
    catalog, db, _ = social_catalog()
    original = db.search_report

    def damaged(*args, **kwargs):
        result = original(*args, **kwargs)
        result["evidence"][0] = result["evidence"][0].model_copy(update=damage)
        return result

    db.search_report = damaged
    result = catalog.call("search_records", {"query": "emissions"})
    # Every retrieved passage was rejected: fail closed, never report a database miss.
    assert result["status"] == "unavailable" and result["search_status"] == "failed"
    assert result["evidence"] == [] and result["rejected_evidence"] == 1


@pytest.mark.parametrize("damage", [
    {"body_hash": "0" * 64}, {"retrievable": False},
    {"retrieval_ranges": [[2, 12]]}, {"retrieval_end": 12}, {"countable": False}, {"active": False},
])
def test_social_search_rejects_changed_or_disallowed_current_source(damage):
    catalog, db, row = social_catalog()
    captured = db.search_report("emissions", catalog.base_filters)
    db.search_report = lambda *args, **kwargs: captured
    row.update(damage)
    result = catalog.call("search_records", {"query": "emissions"})
    # Every retrieved passage was rejected: fail closed, never report a database miss.
    assert result["status"] == "unavailable" and result["search_status"] == "failed"
    assert result["evidence"] == [] and result["rejected_evidence"] == 1


def test_social_excerpt_hash_mismatch_and_out_of_range_fail_closed():
    catalog, _, row = social_catalog()
    assert catalog.call("get_record", {"record_id": row["record_id"], "body_start": 999})["status"] == "clarify"
    row["body_hash"] = "0" * 64
    result = catalog.call("get_record", {"record_id": row["record_id"]})
    assert result["status"] == "unavailable" and "body" not in result
    sources = catalog.call("get_record_sources", {"record_id": row["record_id"]})
    assert sources["source_refs"][0]["verification_status"] == "missing_or_mismatched"
    assert sources["source_refs"][0]["body_hash"] == ""


def test_social_detail_capability_does_not_enable_native_graph_for_social():
    catalog, _, _ = social_catalog()
    schema = catalog.call("get_graph_schema", {})
    assert schema["adapters"]["versioned_record_detail"] == ["native", "social"]
    assert schema["adapters"]["knowledge_graph"] == ["native"]
    assert catalog.call("get_graph_neighborhood", {})["status"] == "unavailable"


def test_all_collection_search_verifies_both_source_kinds_without_merging_them():
    catalog, _, row = social_catalog(Filters(dataset="all"))
    result = catalog.call("search_records", {"query": "emissions"})
    assert result["status"] == "ok" and result["rejected_evidence"] == 0
    assert {ref["dataset"] for ref in result["source_refs"]} == {"native", "social"}
    assert len(result["evidence"]) == 4
    assert next(ref for ref in result["source_refs"] if ref["dataset"] == "social")["record_id"] == row["record_id"]
    native = catalog.call("get_record", {"record_id": "r1"})
    assert native["status"] == "ok" and native["record"]["dataset"] == "native"
    assert "company_affiliation" not in native["record"]


def test_social_details_preserve_inclusive_date_endpoints_and_exclude_unknown_when_requested():
    base = Filters(dataset="social", date_from=date(2020, 3, 1), date_to=date(2020, 3, 1),
                   include_unknown_dates=False)
    catalog, _, row = social_catalog(base)
    assert catalog.call("get_record", {"record_id": row["record_id"]})["status"] == "ok"
    row["date"] = None
    assert catalog.call("get_record_sources", {"record_id": row["record_id"]})["status"] == "clarify"


@pytest.mark.parametrize("links", [True, False])
def test_social_search_never_exposes_private_import_fields_and_honors_link_switch(links):
    catalog, _, _ = social_catalog(links=links)
    result = catalog.call("search_records", {"query": "emissions"})
    assert result["status"] == "ok" and len(result["evidence"]) == 1
    serialized = json.dumps(result)
    assert "never-public" not in serialized and "C:/private" not in serialized
    assert result["evidence"][0]["url"] == ("https://twitter.com/exxonmobil/status/42" if links else "")


@pytest.mark.parametrize("damage", [{"start": True}, {"end": 1.5}, {"retrieval_ranges": [[0, 999]]}])
def test_social_search_corrupt_coordinates_or_retrieval_bounds_fail_closed(damage):
    catalog, db, row = social_catalog()
    captured = db.search_report("emissions", catalog.base_filters)
    db.search_report = lambda *args, **kwargs: captured
    if "retrieval_ranges" in damage:
        row.update(damage)
    else:
        captured["evidence"][0] = captured["evidence"][0].model_copy(update=damage)
    result = catalog.call("search_records", {"query": "emissions"})
    assert result.get("evidence", []) == []
    assert result["status"] in {"ok", "unavailable"}
