"""Knowledge graph input reads only filtered current records in PostgreSQL."""

from datetime import date

import pytest
from test_pipeline_integration import db as db

from observatory.models import Filters, ImportBatch, RecordInput

pytestmark = pytest.mark.integration


def record(record_id, **kwargs):
    return RecordInput(record_id=record_id, dataset="native", body="Stored original body.",
                       raw={"secret": "private-marker"}, **kwargs)


def test_graph_projection_uses_current_version_and_filter_semantics(db):
    db.import_batch(ImportBatch(records=[
        record("a", sponsor="old", published_at=date(2020, 1, 1)),
        record("b", sponsor="exxonmobil", published_at=None),
        record("excluded", countable=False), record("inactive"),
        RecordInput(record_id="social", dataset="social"),
    ]))
    db.import_batch(ImportBatch(records=[record("a", sponsor="exxonmobil", publisher="Forbes",
        published_at=date(2025, 1, 1), keyword="energy",
        annotations=[{"version": "claims-calibrated", "labels": ["green_binary"]}])]))
    with db.connect() as conn:
        conn.execute("UPDATE records SET active=false WHERE record_id='inactive'")
    page = db.knowledge_page(Filters(), limit=5)
    assert page["total"] == 2
    assert [r["record_id"] for r in page["rows"]] == ["a", "b"]
    assert page["rows"][0]["sponsor"] == "exxonmobil"
    assert page["rows"][0]["annotations"][0]["payload"]["labels"] == ["green_binary"]
    assert "private-marker" not in str(page)
    assert page["rows"][0]["body"] == "Stored original body."
    filters = Filters(sponsors=["exxonmobil"], publishers=["Forbes"], keywords=["energy"],
                      labels=["green_binary"], record_ids=["a"], include_unknown_dates=False)
    assert [r["record_id"] for r in db.knowledge_page(filters)["rows"]] == ["a"]
    assert db.knowledge_page(Filters(sponsors=["x' OR 1=1 --"]))["total"] == 0
    assert db.knowledge_page(Filters(include_unknown_dates=False))["total"] == 1
    with pytest.raises(ValueError, match="native"):
        db.knowledge_page(Filters(dataset="social"))


def test_graph_page_bounds_and_stable_pagination(db):
    db.import_batch(ImportBatch(records=[record(f"r{i:02}") for i in range(23)]))
    assert len(db.knowledge_page(Filters(), limit=100)["rows"]) == 20
    page = db.knowledge_page(Filters(), limit=5, offset=999)
    assert page["offset"] == 20 and len(page["rows"]) == 3
    all_ids = [row["record_id"] for offset in range(0, 23, 5)
               for row in db.knowledge_page(Filters(), offset=offset)["rows"]]
    assert all_ids == [f"r{i:02}" for i in range(23)]
    assert db.knowledge_page(Filters(record_ids=["missing"]), offset=99)["offset"] == 0
    with pytest.raises(ValueError):
        db.knowledge_page(Filters(), offset=-1)
    with pytest.raises(ValueError):
        db.knowledge_page(Filters(), limit=True)


def test_graph_count_and_version_rows_share_one_snapshot(db, monkeypatch):
    db.import_batch(ImportBatch(records=[record("a", sponsor="old-source")]))
    original_connect = db.connect
    inserted = False

    class Connection:
        def __enter__(self):
            self.conn = original_connect()
            self.conn.__enter__()
            return self

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

        def execute(self, statement, params=None):
            nonlocal inserted
            result = self.conn.execute(statement, params)
            if "SELECT count(*) AS total" in statement and not inserted:
                inserted = True
                from observatory.db import Database

                writer = Database(db.url)
                writer.import_batch(ImportBatch(records=[record("a", sponsor="new-source"), record("b")]))
            return result

    monkeypatch.setattr(db, "connect", Connection)
    page = db.knowledge_page(Filters())
    assert page["total"] == 1 and len(page["rows"]) == 1
    assert page["rows"][0]["sponsor"] == "old-source"
    monkeypatch.setattr(db, "connect", original_connect)
    refreshed = db.knowledge_page(Filters())
    assert refreshed["total"] == 2
    assert refreshed["rows"][0]["sponsor"] == "new-source"
