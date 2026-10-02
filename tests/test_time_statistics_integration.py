"""Time statistics use isolated obs_test schemas, never the advertising database."""

from datetime import date

import pytest
from psycopg.types.json import Jsonb
from test_pipeline_integration import db  # noqa: F401 - shared isolated fixture

from observatory.date_inference import inference_id
from observatory.models import Filters, ImportBatch, RecordInput

pytestmark = pytest.mark.integration


def seed(task_db):
    records = []
    for number, when in enumerate(("2004-01-01", "2004-12-31", "2010-01-01", "2010-12-31", None)):
        records.append(RecordInput(record_id=f"time-{number}", dataset="native",
                                   url=f"https://example.test/time-{number}",
                                   published_at=date.fromisoformat(when) if when else None,
                                   title=f"Source article {number}", body="Original article text.", retrievable=True))
    task_db.import_batch(ImportBatch(records=records))
    with task_db.connect() as conn:
        version = conn.execute("SELECT current_version FROM records WHERE record_id='time-4'").fetchone()["current_version"]
        conn.execute("INSERT INTO date_inferences(inference_id,record_id,version_id,method,tier,inferred_date,precision,evidence) "
                     "VALUES (%s,'time-4',%s,'url_path','A','2005-01-01','day',%s)",
                     (inference_id(version, "url_path"), version, Jsonb({"url": "https://example.test/2005/01/01/article"})))


def test_sql_years_preserve_ties_unknowns_and_original_dates(db):  # noqa: F811
    seed(db)
    original = Filters(include_inferred_dates=False)
    top = db.research_statistics(original, ranking="highest")
    assert top["groups"] == [{"dataset": "native", "name": "2004", "count": 2},
                             {"dataset": "native", "name": "2010", "count": 2}]
    assert top["collections"][0]["unknown_dates"] == 1
    assert top["collections"][0]["inferred_dates"] == 0
    selected = Filters(include_inferred_dates=True)
    full = db.research_statistics(selected)
    assert sum(group["count"] for group in full["groups"]) == 5
    assert full["collections"][0]["unknown_dates"] == 0
    assert full["collections"][0]["inferred_dates"] == 1
    row = next(row for row in db.public_rows(selected) if row["record_id"] == "time-4")
    assert row["date"] is None and row["source_date"] is None and row["effective_date"] == "2005-01-01"
    assert db.dashboard(selected)["stats"]["unknown_dates"] == 0


@pytest.mark.parametrize("supplemented,expected", [(False, [2, 2]), (True, [3, 2])])
def test_sql_two_periods_use_same_date_basis_and_snapshot(db, supplemented, expected):  # noqa: F811
    seed(db)
    filters = Filters(include_inferred_dates=supplemented)
    periods = [
        {"label": "Before 2008", "filters": filters.model_copy(update={"date_to": date(2007, 12, 31), "include_unknown_dates": False})},
        {"label": "2008 onward", "filters": filters.model_copy(update={"date_from": date(2008, 1, 1), "include_unknown_dates": False})},
    ]
    result = db.research_statistics(filters, group_by="none", periods=periods)
    assert [period["collections"][0]["total"] for period in result["periods"]] == expected
    assert result["collections"][0]["unknown_dates"] == (0 if supplemented else 1)
    assert result["snapshot"] == "repeatable_read_read_only"
    assert sum(expected) + result["collections"][0]["unknown_dates"] == result["collections"][0]["total"]
