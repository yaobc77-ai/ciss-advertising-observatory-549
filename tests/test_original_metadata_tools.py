"""Independent synthetic metadata fixtures; no evaluation/source corpus reads."""

from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from observatory.db import Database
from observatory.models import Filters
from observatory.original_metadata import METADATA_FIELDS, project_record_metadata
from observatory.research_tools import ToolCatalog


def safe_url(value):
    try:
        parsed = urlsplit(value or "")
        return value if (parsed.scheme in {"http", "https"} and parsed.hostname
                         and not parsed.username and not parsed.password) else ""
    except (TypeError, ValueError):
        return ""


def origin(kind="original_metadata", priority=0, *, cells=None, provenance=None):
    return {"origin_kind": kind, "priority": priority, "cells": cells or [],
            "provenance": provenance or []}


def cell(field, value, *, key=None, container="metadata"):
    return {"field": field, "value": value, "present": True,
            "payload_field_path": ["raw", container, key or field]}


def synthetic_row():
    return {"record_id": "synthetic-metadata-17", "dataset": "native",
            "version_id": "a" * 64, "body_hash": "b" * 64,
            "title": "Display Nimbus", "publisher": "Normalized outlet",
            "sponsor": "nimbus lab", "url": "https://synthetic.example/article",
            "source_date": "2024-03-02", "effective_date": "2024-03-02",
            "keyword": "Efficiency", "display_disclosure": "Paid collaboration",
            "metadata_origins": [origin(cells=[
                cell("title", "  Nimbus Research  ", key="title"),
                cell("publisher", "Outlet RAW"), cell("sponsor", "Nimbus LAB"),
                cell("original_url", "https://synthetic.example/article", key="url"),
                cell("publication_date", "02/03/2024", key="date"),
                cell("collection_search_term", "eFFiciency", key="keyword"),
                cell("disclosure_language", "  Paid collaboration  ", key="disclosure language"),
                cell("disclosure_location", "Above the headline", key="disclosure location"),
            ], provenance=[{"row": 17, "row_basis": "logical_record_including_header",
                            "sha256": "c" * 64, "source": "D:/private/archive.xlsx",
                            "source_asset_id": "private-source-id"}])],
            "raw": {"private": "should not be published"}, "body": "Never return this body."}


def project(row, fields=None, **kwargs):
    return project_record_metadata(row, fields, public_url=safe_url, **kwargs)


def test_exact_original_cells_display_and_provenance_are_distinct():
    result = project(synthetic_row())
    assert result["original_fields"]["title"]["value"] == "  Nimbus Research  "
    assert result["display_fields"]["title"] == "Display Nimbus"
    assert result["original_fields"]["sponsor"]["value"] == "Nimbus LAB"
    assert result["display_fields"]["sponsor"] == "nimbus lab"
    assert result["original_fields"]["publication_date"]["value"] == "02/03/2024"
    assert result["display_fields"]["publication_date"] == "2024-03-02"
    assert result["original_fields"]["collection_search_term"]["value"] == "eFFiciency"
    assert result["original_fields"]["disclosure_language"]["value"] == "  Paid collaboration  "
    assert result["original_fields"]["title"]["provenance"] == {
        "status": "recorded", "references": [{"row": 17, "row_basis": "logical_record_including_header",
                                                  "source_sha256": "c" * 64}], "unknown_fields": []}
    encoded = str(result)
    for private in ("D:/private", "private-source-id", "Never return this body", "should not be published"):
        assert private not in encoded
    assert result["online_truth"] == "not_established"
    assert result["source_refs"][0]["body_hash"] == "b" * 64
    assert result["source_refs"][0]["payload_field_path"] == ["raw", "metadata", "title"]


def test_cleaned_copy_difference_does_not_adjudicate_or_conflict_excel():
    row = synthetic_row()
    row["metadata_origins"].append(origin("baseline_source_copy", 1,
        cells=[cell("sponsor", "nimbus lab", container="baseline")]))
    result = project(row, ["sponsor"])
    field = result["original_fields"]["sponsor"]
    assert field["status"] == "recorded" and field["value"] == "Nimbus LAB"
    assert field["source_copies"][0]["value"] == "nimbus lab"
    assert result["review_required"] is False


def test_conflicting_original_values_remain_unresolved():
    row = synthetic_row()
    row["metadata_origins"][0]["cells"].append(cell("sponsor", "Cirrus LAB", key="Sponsor"))
    result = project(row, ["sponsor"])
    field = result["original_fields"]["sponsor"]
    assert field["status"] == "needs_review" and field["value"] is None
    assert {item["value"] for item in field["stored_sources"]} == {"Nimbus LAB", "Cirrus LAB"}
    assert result["review_required"] is True


@pytest.mark.parametrize("value", [None, "", "   ", "N/A", "Unknown"])
def test_blank_original_is_unknown_preserves_value_and_does_not_prove_absence(value):
    row = synthetic_row()
    row["metadata_origins"] = [origin(cells=[cell("disclosure_language", value,
                                                   key="disclosure language")])]
    result = project(row, ["disclosure_language", "disclosure_location"])
    field = result["original_fields"]["disclosure_language"]
    assert field["status"] == "unknown" and field["value"] == value
    assert result["original_fields"]["disclosure_location"]["status"] == "not_recorded"
    assert field["provenance"]["unknown_fields"] == ["row", "source_sha256"]
    assert result["display_fields"]["disclosure_language"] == "Paid collaboration"


@pytest.mark.parametrize("value", ["javascript:alert(1)", "file:///C:/private.xlsx",
                                   "https://name:secret@synthetic.example/x", "//synthetic.example/x"])
def test_unsafe_original_urls_are_not_published(value):
    row = synthetic_row()
    row["metadata_origins"] = [origin(cells=[cell("original_url", value, key="url")])]
    result = project(row, ["original_url"])
    assert result["original_fields"]["original_url"]["status"] == "unsafe_url_withheld"
    assert value not in str(result)


def test_source_links_hidden_applies_to_original_display_and_copies():
    row = synthetic_row()
    row["metadata_origins"].append(origin("baseline_source_copy", 1,
        cells=[cell("original_url", "https://other.example/x", key="url", container="baseline")]))
    result = project(row, ["original_url"], links_enabled=False)
    assert "https://" not in str(result)
    assert result["original_fields"]["original_url"]["status"] == "source_links_hidden"


@pytest.mark.parametrize("value, status", [("x" * 8001, "exceeds_limit"),
                                          ({"private_path": "D:/private"}, "unsupported_value")])
def test_cells_cannot_publish_unbounded_or_nested_raw_values(value, status):
    row = synthetic_row()
    row["metadata_origins"] = [origin(cells=[cell("title", value)])]
    result = project(row, ["title"])
    assert result["original_fields"]["title"]["status"] == status
    assert result["original_fields"]["title"]["value"] is None
    assert "D:/private" not in str(result)


def test_source_inference_does_not_replace_missing_original_date():
    row = synthetic_row()
    row.update(source_date=None, effective_date="2025-06-01", date_basis="inferred:synthetic")
    row["metadata_origins"] = []
    result = project(row, ["publication_date"])
    assert result["original_fields"]["publication_date"]["status"] == "not_recorded"
    assert result["display_fields"]["publication_date"] is None


def test_social_disagreement_does_not_promote_selected_original_copy():
    row = synthetic_row()
    row.update(dataset="social", metadata_conflicting_fields=["sponsor", "published_at"])
    result = project(row, ["sponsor", "publication_date"])
    assert all(item["status"] == "needs_review" and item["value"] is None
               for item in result["original_fields"].values())
    assert result["review_required"] is True


class SyntheticService:
    def __init__(self, row=None, *, links=True):
        self.row = deepcopy(row or synthetic_row())
        self.settings = SimpleNamespace(show_source_links=links)
        self.reads = []
        self.db = SimpleNamespace(original_record_metadata=self.read)

    def read(self, filters, record_id):
        self.reads.append((deepcopy(filters), record_id))
        return deepcopy(self.row)

    def health(self):
        return {"status": "ok", "data_version": "synthetic-v1",
                "countable_record_counts": {"native": 1, "social": 1}}

    def facets(self, dataset):
        return {"publishers": ["Normalized outlet"], "sponsors": ["nimbus lab"],
                "platforms": [], "accounts": [], "keywords": ["Efficiency"], "labels": []}

    def _public_rows(self, rows):
        for row in rows:
            row["url"] = safe_url(row.get("url")) if self.settings.show_source_links else ""
        return rows


def test_tool_available_in_model_and_mcp_catalog_and_reads_current_filtered_id():
    service = SyntheticService()
    filters = Filters(dataset="native", publishers=["Normalized outlet"], sponsors=["nimbus lab"],
                      date_from=date(2024, 1, 1), include_unknown_dates=False)
    catalog = ToolCatalog(service, filters)
    assert "get_record_metadata" in {item["name"] for item in catalog.definitions()}
    assert "get_record_metadata" in {item["name"] for item in catalog.mcp_definitions()}
    result = catalog.call("get_record_metadata", {"record_id": "synthetic-metadata-17",
                                                 "fields": ["sponsor"]})
    assert result["status"] == "ok"
    assert service.reads[0][0] == filters
    assert result["original_fields"]["sponsor"]["value"] == "Nimbus LAB"


@pytest.mark.parametrize("arguments", [
    {"record_id": "synthetic-metadata-17", "fields": ["body"]},
    {"record_id": "synthetic-metadata-17", "source_path": "D:/private/archive.xlsx"},
    {"record_id": "synthetic-metadata-17", "url": "https://other.example"},
    {"record_id": "synthetic-metadata-17", "fields": []},
])
def test_tool_rejects_unlisted_reads_and_paths(arguments):
    service = SyntheticService()
    result = ToolCatalog(service, Filters()).call("get_record_metadata", arguments)
    assert result["status"] == "invalid_request" and service.reads == []


def test_tool_cannot_expand_record_selection():
    service = SyntheticService()
    result = ToolCatalog(service, Filters(record_ids=["synthetic-other"])).call(
        "get_record_metadata", {"record_id": "synthetic-metadata-17"})
    assert result["status"] == "clarify" and service.reads == []


def test_database_projection_is_single_parameterized_current_scope_read():
    database = Database("")
    captured = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params):
            captured.append((sql, params))
            return SimpleNamespace(fetchone=lambda: {"synthetic": True})

    database.connect = Connection
    filters = Filters(dataset="native", publishers=["Normalized outlet"], date_presence="missing")
    assert database.original_record_metadata(filters, "synthetic-metadata-17") == {"synthetic": True}
    sql, params = captured[0]
    assert "v.version_id=r.current_version" in sql
    assert "v.record_id=p.record_id" in sql
    assert "r.record_id=ANY(%s)" in sql
    assert "synthetic-metadata-17" not in sql and ["synthetic-metadata-17"] in params
    assert "Normalized outlet" not in sql and ["Normalized outlet"] in params
    assert "AS metadata_origins" in sql and "'disclosure location'" in sql
    assert "source_asset_id" not in sql and "q->'source'" not in sql
    assert "v.body," not in sql and "v.payload AS" not in sql
    assert filters.record_ids == []
    assert len(captured) == 1


def test_database_outside_selected_record_ids_performs_no_read():
    database = Database("")
    database.connect = lambda: pytest.fail("No read permitted outside current record selection")
    assert database.original_record_metadata(Filters(record_ids=["synthetic-other"]),
                                            "synthetic-metadata-17") is None


def test_field_whitelist_is_explicit_and_bounded():
    assert len(METADATA_FIELDS) == 8
    with pytest.raises(ValueError, match="Unknown metadata field"):
        project(synthetic_row(), ["private_path"])


@pytest.mark.parametrize("total, match_type, expected", [(0, None, "not_found"),
    (1, "exact", "ok"), (2, "exact", "ambiguous"), (12, "literal_contains", "ambiguous")])
def test_title_lookup_reports_candidates_and_ambiguity_without_choosing(total, match_type, expected):
    service = SyntheticService()
    rows = [{**synthetic_row(), "record_id": f"synthetic-candidate-{number}"}
            for number in range(min(total, 2))]
    service.db.find_records = lambda filters, title, limit: {
        "rows": rows, "total_candidates": total, "match_type": match_type}
    result = ToolCatalog(service, Filters()).call("find_records", {"title": "Nimbus Research", "limit": 2})
    assert result["status"] == expected
    assert result["total_candidates"] == total
    assert result["selection_required"] is (total > 1)
    assert result["truncated"] is (total > len(rows))
    assert all("raw" not in item and "body" not in item for item in result["records"])
    assert "Never return this body" not in str(result["records"])
    assert all(item["body_hash"] == "b" * 64 for item in result["records"])


@pytest.mark.parametrize("arguments", [{"title": " "}, {"title": "Nimbus", "limit": 11},
    {"title": "Nimbus", "limit": True}, {"title": "Nimbus", "query": "body"}])
def test_title_lookup_accepts_only_bounded_title_metadata_request(arguments):
    result = ToolCatalog(SyntheticService(), Filters()).call("find_records", arguments)
    assert result["status"] == "invalid_request"


def test_title_sql_is_literal_exact_first_and_has_no_body_projection():
    database, captured = Database(""), []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params):
            captured.append((sql, params))
            return SimpleNamespace(fetchall=lambda: [])

    database.connect = Connection
    filters = Filters(record_ids=["synthetic-metadata-17"], publishers=["Normalized outlet"])
    result = database.find_records(filters, " Nimbus_% ", 3)
    assert result == {"rows": [], "total_candidates": 0, "match_type": None}
    sql, params = captured[0]
    assert "strpos(" in sql and "ILIKE" not in sql and " LIKE " not in sql
    assert "NOT EXISTS (SELECT 1 FROM candidates WHERE title_match='exact')" in sql
    assert "count(*) OVER()" in sql and params[-5:] == ["Nimbus_%"] * 4 + [3]
    assert "v.body," not in sql and "v.version_id=r.current_version" in sql
    assert "Nimbus_%" not in sql


def test_legacy_heldout_tool_is_absent_and_rejected_in_normal_catalog(monkeypatch):
    monkeypatch.setattr("observatory.research_tools.evaluation_active", lambda: False)
    catalog = ToolCatalog(SyntheticService(), Filters())
    for definitions in (catalog.definitions(), catalog.mcp_definitions()):
        assert "get_content_matches" not in {item["name"] for item in definitions}
        assert {"find_records", "get_record_metadata"} <= {item["name"] for item in definitions}
    result = catalog.call("get_content_matches", {})
    assert result["status"] == "invalid_request" and catalog.service.reads == []


def test_tool_withholds_metadata_on_current_data_change():
    service = SyntheticService()
    versions = iter(["synthetic-v1", "synthetic-v2"])
    service.health = lambda: {"status": "ok", "data_version": next(versions),
        "countable_record_counts": {"native": 1}}
    result = ToolCatalog(service, Filters()).call("get_record_metadata", {"record_id": "synthetic-metadata-17"})
    assert result["status"] == "unavailable" and "original_fields" not in result


def test_tool_failure_redacts_database_paths_and_never_substitutes_answer():
    service = SyntheticService()

    def unavailable(*args):
        raise RuntimeError("D:/private import error with secret=value")

    service.db.original_record_metadata = unavailable
    result = ToolCatalog(service, Filters()).call("get_record_metadata", {"record_id": "synthetic-metadata-17"})
    assert result["status"] == "unavailable" and "original_fields" not in result
    assert "D:/private" not in str(result) and "secret=value" not in str(result)
