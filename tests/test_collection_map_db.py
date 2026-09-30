"""Whole collection graph input uses the same SQL eligibility as the dashboard."""

from datetime import date

import pytest
from test_pipeline_integration import db as db

from observatory.models import Filters, ImportBatch, RecordInput

pytestmark = pytest.mark.integration


def test_complete_map_projection_keeps_nonsearchable_records_and_current_versions(db):
    records = [RecordInput(record_id=f"r-{i:03}", dataset="native", sponsor="Sponsor",
                           publisher="News", title=f"Ad {i}", retrievable=False,
                           published_at=date(2025, 1, 1), raw={"token": "private-marker"})
               for i in range(137)]
    records.extend([RecordInput(record_id="social", dataset="social"),
                    RecordInput(record_id="uncountable", dataset="native", countable=False)])
    db.import_batch(ImportBatch(records=records))
    rows = db.knowledge_map_rows(Filters())
    assert len(rows) == len(db.public_rows(Filters())) == 137
    assert {row["record_id"] for row in rows} == {f"r-{i:03}" for i in range(137)}
    assert all("body" not in row and "raw" not in row and "annotations" not in row for row in rows)
    assert "private-marker" not in str(rows)
    assert all(row["retrievable"] is False for row in rows)
    assert all(len(row["body_hash"]) == 64 and row["version_id"] for row in rows)


def test_map_read_obeys_dates_names_labels_record_ids_and_native_scope(db):
    db.import_batch(ImportBatch(records=[
        RecordInput(record_id="current", dataset="native", sponsor="Sponsor", publisher="News",
                    keyword="energy", published_at=date(2025, 2, 1),
                    annotations=[{"version": "claims-calibrated", "labels": ["green_binary"]}]),
        RecordInput(record_id="unknown-date", dataset="native", sponsor="Sponsor", publisher="News"),
        RecordInput(record_id="other", dataset="native", sponsor="Other", publisher="News"),
    ]))
    filters = Filters(sponsors=["Sponsor"], publishers=["News"], labels=["green_binary"],
                      keywords=["energy"], record_ids=["current"], date_from=date(2025, 1, 1),
                      include_unknown_dates=False)
    assert [row["record_id"] for row in db.knowledge_map_rows(filters)] == ["current"]
    assert db.knowledge_map_rows(Filters(sponsors=["x' OR 1=1 --"])) == []
    with pytest.raises(ValueError, match="native"):
        db.knowledge_map_rows(Filters(dataset="all"))
