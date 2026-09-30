"""Full graph delivery retains scope, eligibility and public source policy."""

import json
from types import SimpleNamespace

import pytest

from observatory.models import Filters
from observatory.service import Service


class MapDB:
    def __init__(self):
        self.calls = []

    def knowledge_map_rows(self, filters):
        self.calls.append(filters.model_dump(mode="json"))
        return [{"record_id": f"r-{i}", "version_id": f"v-{i}", "dataset": "native",
                 "title": f"Article {i}", "sponsor": "exxonmobil", "publisher": "News",
                 "date": None, "retrievable": i % 2 == 0, "body_hash": "a" * 64,
                 "url": "https://example.org/ad", "archive_url": "file:///private.pdf",
                 "raw": {"credential": "never-public"}, "body": "never-public"}
                for i in range(137)]


def test_full_map_passes_every_filter_and_covers_records_beyond_old_page_limit():
    db = MapDB()
    service = Service(SimpleNamespace(show_source_links=True), db=db, rag=object())
    filters = Filters(publishers=["News"], sponsors=["exxonmobil"], labels=["green_binary"],
                      keywords=["energy"], include_unknown_dates=False)
    graph = service.knowledge_map(filters)
    assert db.calls == [filters.model_dump(mode="json")]
    assert graph["filters"] == filters.model_dump(mode="json")
    assert graph["coverage"]["total_records"] == 137
    assert graph["coverage"]["truncated"] is False
    assert graph["summary_edges"][0]["count"] == 137
    assert len(graph["summary_edges"][0]["witnesses"]) == 137
    assert "never-public" not in json.dumps(graph) and "private.pdf" not in json.dumps(graph)


def test_link_toggle_preserves_counted_witnesses_without_external_links():
    service = Service(SimpleNamespace(show_source_links=False), db=MapDB(), rag=object())
    graph = service.knowledge_map(Filters())
    assert "https://example.org/ad" not in json.dumps(graph)
    assert len(graph["records"]) == 137
    assert all(row["url"] == "" for row in graph["records"])
    assert graph["summary_edges"][0]["count"] == 137


def test_social_map_does_not_read_native_records_or_present_missing_data_as_empty():
    db = MapDB()
    service = Service(SimpleNamespace(show_source_links=True), db=db, rag=object())
    with pytest.raises(ValueError, match="native"):
        service.knowledge_map(Filters(dataset="social"))
    assert db.calls == []
