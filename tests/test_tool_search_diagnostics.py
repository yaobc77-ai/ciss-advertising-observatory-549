"""Fresh synthetic search contracts; no SQL or provider is invoked.

Production Database._coverage runs against scripted cursor rows, then the
production Service, ToolCatalog and registered MCP callback are exercised.
"""
import hashlib

import anyio
import pytest
from mcp.types import CallToolRequestParams

from observatory.config import Settings
from observatory.db import Database
from observatory.mcp_server import build_mcp_server
from observatory.models import Evidence, Filters
from observatory.research_tools import ToolCatalog
from observatory.service import Service


class Cursor:
    def __init__(self, value):
        self.value = value

    def fetchall(self):
        return self.value

    def fetchone(self):
        return self.value


class CoverageConnection:
    """Scripted result rows only; this is not a SQL engine."""
    def __init__(self, scope_records=1, scope_chunks=1):
        self.scope_records, self.scope_chunks = scope_records, scope_chunks

    def execute(self, statement, params):
        if "tsvector_to_array" in statement:
            return Cursor([{"term": term, "lexemes": [term]} for term in params[0]])
        if "AS scope_records" in statement:
            return Cursor({"scope_records": self.scope_records, "scope_chunks": self.scope_chunks})
        if "matching_records" in statement:
            return Cursor([{"term": term, "matching_records": 0, "returned_ids": []}
                           for term in params[-1]])
        raise AssertionError("Unexpected mocked coverage statement")


def production_coverage(*, partial=False, indexed=True):
    return Database._coverage(CoverageConnection(1 if indexed else 0, 1 if indexed else 0),
                              ["meteorite", "中文"] if partial else ["meteorite"],
                              " FROM synthetic_index", "TRUE", [], [])


BODY = "A synthetic notice reports a small prototype."
SOURCE = {"record_id": "contract-source-1", "version_id": "contract-version-1",
          "dataset": "native", "title": "Independent prototype notice",
          "body": BODY, "body_hash": hashlib.sha256(BODY.encode()).hexdigest(),
          "retrievable": True, "publisher": "Lantern Dispatch", "sponsor": "Juniper Lab",
          "date": None, "url": "", "archive_url": ""}


class NoProvider:
    @property
    def client(self):
        raise AssertionError("A model provider must never be called")


class SyntheticDB:
    def __init__(self, diagnostics, evidence=()):
        self.diagnostics, self.evidence = diagnostics, list(evidence)

    def connect(self, *args, **kwargs):
        raise AssertionError("No database connection is authorized")

    def health(self):
        return {"status": "ok", "data_version": "contract-data-v1",
                "statistics_version": "contract-stats-v1", "countable_record_counts": {"native": 1}}

    def facets(self, dataset):
        return {"publishers": ["Lantern Dispatch"], "sponsors": ["Juniper Lab"],
                "accounts": [], "platforms": [], "keywords": [], "labels": []}

    def public_rows(self, filters):
        return []

    def search_report(self, query, filters, limit=5):
        return {"evidence": self.evidence, "diagnostics": self.diagnostics}

    def versioned_record(self, filters, record_id):
        return dict(SOURCE) if record_id == SOURCE["record_id"] else None


def catalog_for(diagnostics, evidence=(), *, web_enabled=True):
    service = Service(Settings(web_search_enabled=web_enabled, show_source_links=True),
                      db=SyntheticDB(diagnostics, evidence), rag=NoProvider())
    return ToolCatalog(service, Filters(include_inferred_dates=False))


def call_search(catalog, query="meteorite"):
    return catalog.call("search_records", {"query": query})


def test_healthy_empty_actual_coverage_status_produces_one_use_ticket():
    result = call_search(catalog_for(production_coverage()))
    assert result["status"] == "ok", result
    assert len(result["web_fallback_ticket"]) == 32
    assert result["search_status"] == "empty", result
    assert result["diagnostics"]["status"] == "available"
    assert result["diagnostics"]["missing_from_scope"] == ["meteorite"]


def test_partial_coverage_empty_is_partial_and_never_a_web_miss():
    result = call_search(catalog_for(production_coverage(partial=True)))
    assert result["status"] == "ok", result
    assert result["search_status"] == "partial", result
    assert result["diagnostics"]["ignored_terms"] == ["中文"]
    assert "web_fallback_ticket" not in result


def test_zero_indexed_scope_does_not_authorize_external_absence_lookup():
    result = call_search(catalog_for(production_coverage(indexed=False)))
    assert "web_fallback_ticket" not in result
    assert result["search_status"] == "partial", result
    assert result["diagnostics"]["scope_chunks"] == 0


@pytest.mark.parametrize("query", ["   ", "meteorite"])
def test_unavailable_empty_search_is_failed_not_success(query):
    result = call_search(catalog_for(Database._unavailable_coverage("Synthetic unavailable index", [])), query)
    assert result["status"] in {"invalid_request", "unavailable"}, result
    assert "web_fallback_ticket" not in result


def test_valid_bound_evidence_with_unavailable_coverage_is_retained_as_partial():
    evidence = Evidence(evidence_id="contract-evidence-1", record_id=SOURCE["record_id"],
                        version_id=SOURCE["version_id"], dataset="native", title=SOURCE["title"],
                        text=BODY, start=0, end=len(BODY))
    result = call_search(catalog_for(Database._unavailable_coverage("English coverage unavailable", []), [evidence]))
    assert result["status"] == "ok", result
    assert result["search_status"] == "partial", result
    assert len(result["evidence"]) == 1 and result["source_refs"][0]["verification_status"] == "exact_character_match"
    assert "web_fallback_ticket" not in result


def test_mcp_marks_unavailable_empty_search_as_tool_error():
    server = build_mcp_server(catalog_for(Database._unavailable_coverage("Synthetic unavailable index", [])))
    entry = server._request_handlers["tools/call"]
    result = anyio.run(entry.handler, None, CallToolRequestParams(name="search_records", arguments={"query": "meteorite"}))
    assert result.is_error is True, result.model_dump(mode="json")
    assert result.structured_content["status"] == "unavailable"


def test_web_disabled_never_produces_ticket():
    result = call_search(catalog_for(production_coverage(), web_enabled=False))
    assert "web_fallback_ticket" not in result


def test_database_exception_is_unavailable_and_never_a_ticket():
    catalog = catalog_for(production_coverage())
    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic driver failure")
    catalog.service.db.search_report = fail
    result = call_search(catalog)
    assert result["status"] == "unavailable", result
    assert "web_fallback_ticket" not in result


def test_all_rejected_evidence_is_failed_and_never_a_web_miss():
    evidence = Evidence(evidence_id="contract-stale-evidence", record_id=SOURCE["record_id"],
                        version_id="old-unselected-version", dataset="native", title=SOURCE["title"],
                        text=BODY, start=0, end=len(BODY))
    result = call_search(catalog_for(production_coverage(), [evidence]))
    assert result["status"] == "unavailable" and result["search_status"] == "failed", result
    assert result["rejected_evidence"] == 1 and not result["evidence"]
    assert "web_fallback_ticket" not in result


def test_comparison_with_one_failed_target_is_not_complete():
    catalog = catalog_for(production_coverage())
    catalog.service.db.facets = lambda dataset: {"publishers": [],
        "sponsors": ["Juniper Lab", "Willow Bench"], "accounts": [], "platforms": [], "keywords": [], "labels": []}
    def scoped_report(query, filters, limit=5):
        return {"evidence": [], "diagnostics": production_coverage() if filters.sponsors == ["Juniper Lab"]
                else Database._unavailable_coverage("Synthetic target unavailable", [])}
    catalog.service.db.search_report = scoped_report
    result = catalog.call("search_records", {"query": "meteorite", "comparison_scopes": [
        {"query": "meteorite", "filters": {"sponsors": ["Juniper Lab"]}},
        {"query": "meteorite", "filters": {"sponsors": ["Willow Bench"]}}]})
    assert result["status"] == "unavailable" and result["search_status"] == "failed", result
    assert [group["search_status"] for group in result["retrieval_groups"]] == ["empty", "failed"]
    assert "web_fallback_ticket" not in result


def test_valid_empty_ticket_is_consumed_once_with_only_local_stub():
    catalog = catalog_for(production_coverage())
    ticket = call_search(catalog)["web_fallback_ticket"]
    stub_calls = []
    def local_only(query, filters, visitor):
        stub_calls.append((query, filters.model_dump(mode="json")))
        return {"status": "unavailable", "provider_calls": 0, "cost_usd": 0}
    catalog.service._search_external = local_only
    first = catalog.search_external_sources({"ticket": ticket})
    second = catalog.search_external_sources({"ticket": ticket})
    assert first["provider_calls"] == 0 and second["status"] == "invalid_request"
    assert len(stub_calls) == 1


def test_partial_search_revokes_previous_in_memory_ticket():
    catalog = catalog_for(production_coverage())
    ticket = call_search(catalog)["web_fallback_ticket"]
    catalog.service.db.diagnostics = production_coverage(partial=True)
    result = call_search(catalog)
    assert result["search_status"] == "partial"
    assert catalog.search_external_sources({"ticket": ticket})["status"] == "invalid_request"
