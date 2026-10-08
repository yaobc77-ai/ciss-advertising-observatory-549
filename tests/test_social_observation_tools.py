"""MCP reads saved observations without choosing a conflicting text as truth."""

import json

import pytest
from test_research_tools import social_catalog
from test_social_observation_db import source_fixture

from observatory.models import Filters
from observatory.research_tools import ToolCatalog
from observatory.social_source_binding import bind_search_row


def observed_catalog():
    catalog, db, row = social_catalog()
    payload, sources, found = source_fixture()
    row.update(record_id=payload["record_id"], version_id=found["version_id"], url=payload["url"],
               body=payload["body"], social_source_observations=sources,
               body_hash=payload["raw"]["body_sha256"])
    item = bind_search_row(found)
    db.search_report = lambda *args, **kwargs: {
        "evidence": [item], "diagnostics": {"status": "ok", "terms": ["carbon"]}}
    return catalog, db, row, item, sources


def test_mcp_retrieves_extra_source_text_with_observation_references():
    catalog, _, _, item, _ = observed_catalog()
    result = catalog.call("search_records", {"query": "carbon"})
    assert result["rejected_evidence"] == 0 and len(result["evidence"]) == 1
    assert result["evidence"][0]["text"] == item.text
    assert result["source_refs"][0]["source_observation_id"] == item.source_observation_id
    assert result["source_refs"][0]["source_body_hash"] == item.source_body_hash
    assert result["evidence"][0]["source_conflicts"] == ["body"]
    assert "PRIVATE" not in json.dumps(result)


def test_conflicting_record_default_lists_sources_and_requests_selection():
    catalog, _, row, _, sources = observed_catalog()
    result = catalog.call("get_record", {"record_id": row["record_id"]})
    assert result["text_status"] == "source_observation_selection"
    assert "body" not in result and result["source_observations"][1]["source_observation_id"] == sources[1]["source_observation_id"]


def test_explicit_observation_read_uses_its_own_offsets_and_hash():
    catalog, _, row, _, sources = observed_catalog()
    result = catalog.call("get_record", {"record_id": row["record_id"],
        "source_observation_id": sources[1]["source_observation_id"], "body_start": 18, "body_limit": 12})
    assert result["body"]["text"] == sources[1]["body"][18:30]
    assert result["source_refs"][0]["source_version_id"] == sources[1]["source_version_id"]
    assert result["body"]["body_hash"] == sources[1]["source_body_hash"]


@pytest.mark.parametrize("damage", [{"retrievable": False}, {"version_id": "e" * 64},
    {"social_source_observations": []}, {"social_source_observations": ["bad"]}])
def test_mcp_rejects_current_source_drift(damage):
    catalog, _, row, _, _ = observed_catalog()
    row.update(damage)
    result = catalog.call("search_records", {"query": "carbon"})
    assert result["evidence"] == [] and result["rejected_evidence"] == 1


def test_nonexistent_observation_cannot_fall_back_to_display_body():
    catalog, _, row, _, _ = observed_catalog()
    result = catalog.call("get_record", {"record_id": row["record_id"], "source_observation_id": "junkipedia:999"})
    assert result["status"] != "ok" and "body" not in result


def test_observation_selection_preserves_trusted_record_filter():
    catalog, _, row, _, _ = observed_catalog()
    catalog = ToolCatalog(catalog.service, Filters(dataset="social", record_ids=["different:post"]))
    result = catalog.call("get_record", {"record_id": row["record_id"], "source_observation_id": "junkipedia:102"})
    assert result["status"] != "ok"
