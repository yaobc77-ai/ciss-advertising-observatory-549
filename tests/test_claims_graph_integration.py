"""Synthetic claims and graph reads share a source and publication snapshot."""

import pytest
from test_claims_store_integration import db as db
from test_claims_store_integration import prepare, prepared_store

from observatory.claims_store import ClaimsStore
from observatory.knowledge_graph import build_graph
from observatory.models import Filters

pytestmark = pytest.mark.integration


def test_graph_reads_exact_assignments_for_only_its_selected_article_page(db, tmp_path):
    files, store = prepared_store(db, tmp_path)
    store.import_prepared(prepare(files), apply=True)
    page = db.knowledge_page(Filters(), limit=1)
    claims = page["claims2"]
    assert len(page["rows"]) == 1 and claims["total_records"] == 1
    assert claims["coverage_summary"]["scope_records"] == 1
    assert [record["record_id"] for record in claims["records"]] == [page["rows"][0]["record_id"]]
    graph = build_graph(page["rows"], claims2=claims)
    assert graph["claims2"]["shown_match_records"] == 1
    for edge in graph["edges"]:
        assert edge["record_ids"] == [page["rows"][0]["record_id"]]
    all_matches = store.matches(Filters(), limit=1)
    assert all_matches["total_records"] == 2 and all_matches["coverage_summary"]["scope_records"] == 2
    assert sum(row["assignment_count"] for row in all_matches["category_counts"]) == 3
    assert all(row["record_count"] <= 2 for row in all_matches["category_counts"])


def test_retraction_during_graph_read_is_consistent_until_next_snapshot(db, tmp_path, monkeypatch):
    files, store = prepared_store(db, tmp_path)
    store.import_prepared(prepare(files), apply=True)
    original_connect = db.connect
    retracted = False

    class Connection:
        def __enter__(self):
            self.conn = original_connect()
            self.conn.__enter__()
            return self

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

        def execute(self, statement, params=None):
            nonlocal retracted
            value = self.conn.execute(statement, params)
            if "COALESCE((SELECT jsonb_agg" in statement and not retracted:
                retracted = True
                from observatory.db import Database

                ClaimsStore(Database(db.url)).retract(files["candidate_keys"][0],
                    reviewer="Synthetic reviewer", reason="Synthetic withdrawal",
                    reviewed_at="2026-09-30T00:00:00Z")
            return value

    monkeypatch.setattr(db, "connect", Connection)
    old_page = db.knowledge_page(Filters())
    assert old_page["claims2"]["total_matches"] == 3
    old_graph = build_graph(old_page["rows"], claims2=old_page["claims2"])
    assert old_graph["claims2"]["shown_assignments"] == 3
    monkeypatch.setattr(db, "connect", original_connect)
    new_page = db.knowledge_page(Filters())
    assert new_page["claims2"]["total_matches"] == 2
    assert new_page["claims2"]["claims_version"] != old_page["claims2"]["claims_version"]


def test_empty_and_unknown_selections_do_not_return_unfiltered_claims(db, tmp_path):
    files, store = prepared_store(db, tmp_path)
    store.import_prepared(prepare(files), apply=True)
    page = db.knowledge_page(Filters(record_ids=["not-present"]))
    assert not page["rows"] and page["total"] == 0
    assert not page["claims2"]["records"] and page["claims2"]["total_records"] == 0
    assert page["claims2"]["coverage_summary"]["scope_records"] == 0
    unlabelled = store.matches(Filters(), nc_ids=["NC_999"])
    assert unlabelled["total_matches"] == 0 and not unlabelled["category_counts"]
    assert unlabelled["coverage_summary"]["scope_records"] == 2
    assert not unlabelled["coverage_summary"]["classification_completion_known"]
