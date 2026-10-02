"""Inferred dates on real PostgreSQL: eligibility, opt-in filtering and review states."""

import json
from datetime import date

import pytest
from psycopg.types.json import Jsonb
from test_pipeline_integration import db  # noqa: F401 - shared obs_test fixture

from observatory.date_inference import (
    apply_inferences,
    inference_id,
    propose_url_inferences,
)
from observatory.import_records import load_records
from observatory.models import Filters

pytestmark = pytest.mark.integration


def import_undated(db, tmp_path):  # noqa: F811 - db is the fixture
    path = tmp_path / "undated.jsonl"
    rows = [
        {"record_id": "url-dated", "dataset": "native",
         "url": "https://www.cnbc.com/advertorial/2016/09/29/explaining-energy.html",
         "body": "An undated article whose URL carries a date."},
        {"record_id": "search-dated", "dataset": "native",
         "url": "https://www.washingtonpost.com/creativegroup/example/responsibly-green/",
         "body": "An undated article without a URL date."},
        {"record_id": "source-dated", "dataset": "native", "published_at": "2016-05-01",
         "url": "https://www.cnbc.com/advertorial/2016/05/01/known.html",
         "body": "An article with a source date."},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    db.import_batch(load_records(path))


def year_2016(db, include):  # noqa: F811
    return {r["record_id"] for r in db.public_rows(Filters(
        date_from=date(2016, 1, 1), date_to=date(2016, 12, 31), include_unknown_dates=False,
        include_inferred_dates=include))}


def test_supplemented_dates_are_opt_in_per_filter_and_used_without_review(db, tmp_path):  # noqa: F811
    import_undated(db, tmp_path)
    with db.connect() as conn:
        proposals = propose_url_inferences(conn)
        assert [p["record_id"] for p in proposals] == ["url-dated"]
        assert proposals[0]["inferred_date"] == date(2016, 9, 29)
        assert apply_inferences(conn, proposals) == 1 and apply_inferences(conn, proposals) == 0
        version = conn.execute("SELECT current_version FROM records WHERE record_id='search-dated'").fetchone()["current_version"]
        conn.execute(
            "INSERT INTO date_inferences(inference_id,record_id,version_id,method,tier,inferred_date,precision,evidence) "
            "VALUES (%s,'search-dated',%s,'web_search','C','2016-06-05','day',%s)",
            (inference_id(version, "web_search"), version,
             Jsonb({"evidence_url": "https://www.washingtonpost.com/creativegroup/example/responsibly-green/",
                    "host_match": True})))

    # No manual review step (user decision 2026-10-01): any row not rejected is used.
    assert year_2016(db, include=False) == {"source-dated"}
    assert year_2016(db, include=True) == {"source-dated", "url-dated", "search-dated"}
    source_only = db.public_rows(Filters(include_inferred_dates=False))
    assert {r["record_id"]: r["effective_date"] for r in source_only} == {
        "url-dated": None, "search-dated": None, "source-dated": "2016-05-01",
    }
    assert db.dashboard(Filters(include_inferred_dates=False))["stats"]["unknown_dates"] == 2
    selected = Filters(include_inferred_dates=True)
    basis = {r["record_id"]: r["date_basis"] for r in db.public_rows(selected)}
    assert basis == {"url-dated": "inferred:url_path", "search-dated": "inferred:web_search", "source-dated": "source"}
    assert db.dashboard(selected)["stats"]["inferred_dates"] == {"A": 1, "C": 1}
    assert db.dashboard(selected)["stats"]["unknown_dates"] == 0

    with db.connect() as conn:
        conn.execute("UPDATE date_inferences SET review_state='rejected' WHERE method='url_path'")
    assert year_2016(db, include=True) == {"source-dated", "search-dated"}
    assert db.dashboard(selected)["stats"]["inferred_dates"] == {"C": 1}
    # Source dates and the published data version are untouched by inferences.
    assert {r["record_id"]: r["date"] for r in db.public_rows(Filters())}["source-dated"] == "2016-05-01"



def test_web_dates_from_another_site_are_not_used(db, tmp_path):  # noqa: F811
    import_undated(db, tmp_path)
    with db.connect() as conn:
        version = conn.execute("SELECT current_version FROM records WHERE record_id='search-dated'").fetchone()["current_version"]
        conn.execute(
            "INSERT INTO date_inferences(inference_id,record_id,version_id,method,tier,inferred_date,precision,evidence) "
            "VALUES (%s,'search-dated',%s,'web_search','C','2016-06-05','day',%s)",
            (inference_id(version, "web_search"), version,
             Jsonb({"evidence_url": "https://sponsor.example/newsroom", "host_match": False})))
    assert "search-dated" not in year_2016(db, include=True)
    assert {r["record_id"]: r["date_basis"] for r in db.public_rows(Filters())}["search-dated"] == "missing"
