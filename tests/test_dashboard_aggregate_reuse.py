"""Aggregate/page contract checks and opt-in read-only PostgreSQL CTE fixtures."""

import os
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
from postgres_fixtures import configured_test_url, verified_test_connection
from psycopg.types.json import Jsonb

from observatory.analytics import (
    historical_label_distribution,
    sponsor_publisher_matrix,
    yearly_timeline,
)
from observatory.db import Database
from observatory.models import Filters
from observatory.service import summarize
from observatory.social_annotations import (
    SCHEME,
    STATUS,
    social_state_id,
)


class RecordingDatabase(Database):
    def __init__(self):
        super().__init__("unused-offline")
        self.calls = []
        self.connections = 0

    def connect(self, vector=False):
        self.connections += 1
        db = self

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                db.calls.append((sql, deepcopy(params)))
                value = {"total": 0, "retrievable": 0, "unknown_dates": 0, "page_offset": 0,
                         "page_rows": [], "groups": [], "relationships": [], "months": [],
                         "years": [], "labels": [], "native_labels": {"total": 0, "count": 0},
                         "inferred": [], "combined": {"collections": [
                             {"dataset": dataset, "total": 0, "retrievable": 0, "unknown_dates": 0}
                             for dataset in ("native", "social")
                         ], "companies": [], "timeline": []}}
                return SimpleNamespace(fetchone=lambda: value)

        return Connection()

    @staticmethod
    def _social_historical_labels(conn, select, params):
        return {"fixture_scope": True}


@pytest.mark.parametrize("dataset", ["native", "social", "all"])
def test_one_aggregate_keeps_full_parameterized_selection_and_narrow_materialization(dataset):
    filters = Filters(dataset=dataset, sponsors=["literal'); DROP TABLE records; --"],
                      record_ids=["one", "two"], date_from=date(2020, 1, 1),
                      include_unknown_dates=True)
    if dataset == "social":
        filters.accounts = ["Account"]
        filters.labels = [social_state_id("green_binary", "unknown")]
    expected_source, expected_params = Database("")._public_query(filters)
    db = RecordingDatabase()
    result = db.dashboard(filters)
    assert db.connections == 1 and len(db.calls) == 2
    assert db.calls[0] == ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY", None)
    statement, params = db.calls[1]
    assert expected_source in statement
    assert params == [*expected_params, 0, 20, 20, 20, dataset == "all"]
    assert filters.sponsors[0] not in statement
    narrow = statement.split("filtered AS MATERIALIZED (", 1)[1].split("), totals AS (", 1)[0]
    assert "FROM public_source" in narrow
    for unused in ("social_historical_states", "body", "raw", "url", "archive_url", "SELECT *"):
        assert unused not in narrow
    assert "public_source AS NOT MATERIALIZED" in statement
    assert "page_members AS MATERIALIZED" in statement
    assert "JOIN public_source p USING(record_id,version_id)" in statement
    assert "to_jsonb(p) ORDER BY m.page_ordinal" in statement
    for aggregate in ("groups", "relationships", "months", "years", "label_records", "native_labels", "inferred"):
        assert f"), {aggregate} AS (" in statement
    assert result["page"] == {"rows": [], "total": 0, "offset": 0}
    assert ("social_historical_labels" in result) == (dataset != "native")
    assert ("combined" in result) == (dataset == "all")


def test_huge_offset_and_page_limit_preserve_old_bounds_without_bigint_overflow():
    db = RecordingDatabase()
    db.dashboard(Filters(), offset=10**100, limit=999)
    assert db.calls[1][1][-5:] == [2**63-1, 100, 100, 100, False]


@pytest.mark.parametrize("kwargs", [{"limit": True}, {"offset": -1}, {"limit": 0},
                                    {"sort_by": "title; DROP TABLE records"}, {"descending": "yes"}])
def test_invalid_page_options_fail_before_connection(kwargs):
    db = RecordingDatabase()
    with pytest.raises(ValueError):
        db.dashboard(Filters(), **kwargs)
    assert db.connections == 0 and db.calls == []


def fixture_rows():
    rows = []
    for number in range(47):
        native = number % 3 == 0
        record_id = f"fixture-{number:03}"
        published = None if number % 7 == 0 else f"{2020 + number % 3}-04-05"
        rows.append({
            "record_id": record_id, "version_id": f"version-{number:03}",
            "dataset": "native" if native else "social", "title": f"Title {number % 4}",
            "publisher": ("Outlet A", "Outlet B", "")[number % 3],
            "sponsor": ("Sponsor A", "Sponsor B", "")[number % 3],
            "platform": "" if native else ("Facebook", "Twitter")[number % 2],
            "account": "" if native else ("Account A", "Account B")[number % 2],
            "keyword": ("energy", "oil")[number % 2], "date": published,
            "source_date": published, "effective_date": published,
            "date_basis": "source" if published else "missing", "inferred_date": None,
            "inferred_tier": None, "retrievable": number % 5 != 0,
            "url": f"https://example.test/{record_id}", "archive_url": "",
            "collection_scope": None if native else "collected_company_posts",
            "count_unit": None if native else "platform_canonical_original_post_url",
            "paid_ad_status": None if native else "not_verified",
            "labels": [" green.claim ", "green.claim", "ff.claim", " "] if native and number % 2 else [],
            "social_historical_states": [] if native else [social_state_id("green_binary", "unknown")],
            "social_historical_scheme": None if native else SCHEME,
            "social_historical_status": None if native else STATUS,
        })
    return rows


@pytest.fixture
def readonly_pg():
    """Explicit opt-in, only literal CTE data; no tables or business writes."""
    if os.getenv("OBS_RUN_DASHBOARD_AGGREGATE") != "1":
        pytest.skip("Set OBS_RUN_DASHBOARD_AGGREGATE=1 for local read-only SQL fixtures")
    url = configured_test_url()
    try:
        conn = verified_test_connection(url)
    except Exception as exc:
        pytest.fail(f"Read-only fixture connection failed ({type(exc).__name__})", pytrace=False)
    try:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout='30s'")
        yield conn
    finally:
        conn.rollback()
        conn.close()


class FixtureDatabase(Database):
    """The production aggregate SQL operates on safe literal public-source rows."""
    def __init__(self, connection, rows):
        super().__init__("unused-fixture")
        self.connection, self.rows, self.calls = connection, rows, []

    def connect(self, vector=False):
        db = self

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                db.calls.append((sql, params))
                return db.connection.execute(sql, params)

        return Connection()

    def _public_query(self, filters):
        columns = (
            "record_id text,dataset text,version_id text,title text,publisher text,sponsor text,"
            "platform text,account text,keyword text,date text,source_date text,effective_date text,"
            "date_basis text,inferred_date text,inferred_tier text,retrievable boolean,"
            "url text,archive_url text,collection_scope text,count_unit text,paid_ad_status text,"
            "labels jsonb,social_historical_scheme text,social_historical_status text"
        )
        # A correlated scalar exposes whether metadata pruning works. Its
        # generate_series plan must run only for the social page, never all rows.
        projection = f"""SELECT r.*,CASE WHEN dataset='social' THEN (
            SELECT jsonb_agg('{social_state_id('green_binary', 'unknown')}'::text)
            FROM generate_series(1,1) marker(n) WHERE length(r.record_id)>0
        ) ELSE '[]'::jsonb END AS social_historical_states
        FROM jsonb_to_recordset(%s::jsonb) AS r({columns})
        WHERE (%s='all' OR dataset=%s)"""
        supplied = [{**row, "body": "PRIVATE_FIXTURE_BODY_DO_NOT_RETURN",
                     "raw": {"secret": "PRIVATE_FIXTURE_PAYLOAD_DO_NOT_RETURN"}} for row in self.rows]
        return projection, [Jsonb(supplied), filters.dataset, filters.dataset]

    def _social_historical_labels(self, conn, select, params):
        total = sum(row["dataset"] == "social" for row in self.rows)
        return {"total": total, "unknown_annotation_records": total, "source_state_version": "a" * 64}


def ordered(rows, column, descending):
    # Independent stable ordering: NULL always last; ID ascending breaks ties.
    rows = sorted(rows, key=lambda row: row["record_id"])
    known = [row for row in rows if row[column] is not None]
    missing = [row for row in rows if row[column] is None]
    return sorted(known, key=lambda row: row[column], reverse=descending) + missing


@pytest.mark.integration
@pytest.mark.parametrize("dataset", ["native", "social", "all"])
@pytest.mark.parametrize("sort_by,descending", [("date", True), ("title", False), ("account", True),
                                               ("retrievable", False), ("record_id", True)])
def test_readonly_literal_sql_matches_record_oracle(readonly_pg, dataset, sort_by, descending):
    rows = fixture_rows()
    selected = [row for row in rows if dataset == "all" or row["dataset"] == dataset]
    db = FixtureDatabase(readonly_pg, rows)
    result = db.dashboard(Filters(dataset=dataset), offset=20, limit=20,
                          sort_by=sort_by, descending=descending)
    assert result["stats"] == summarize(selected)
    assert result["timeline"] == yearly_timeline(selected)
    assert result["labels"] == historical_label_distribution([row for row in selected if row["dataset"] == "native"])
    assert result["matrix"] == sponsor_publisher_matrix(selected)
    offset = min(20, max(0, ((len(selected)-1)//20)*20))
    assert result["page"] == {"total": len(selected), "offset": offset,
                              "rows": ordered(selected, sort_by, descending)[offset:offset+20]}
    assert "PRIVATE_FIXTURE" not in str(result)
    assert all("page_ordinal" not in row and "body" not in row and "raw" not in row
               for row in result["page"]["rows"])


@pytest.mark.integration
@pytest.mark.parametrize("rows,offset,limit", [([], 999, 20), (fixture_rows(), 10**100, 20),
                                             (fixture_rows(), 999, 999)])
def test_readonly_literal_sql_clamps_empty_stale_and_large_pages(readonly_pg, rows, offset, limit):
    result = FixtureDatabase(readonly_pg, rows).dashboard(Filters(dataset="all"), offset=offset, limit=limit)
    bounded_limit = min(limit, 100)
    expected_offset = min(offset, max(0, ((len(rows)-1)//bounded_limit)*bounded_limit))
    assert result["page"] == {"total": len(rows), "offset": expected_offset,
                              "rows": ordered(rows, "date", True)[expected_offset:expected_offset+bounded_limit]}
    if not rows:
        assert result["stats"]["total"] == result["stats"]["unknown_dates"] == 0
        assert result["labels"]["items"] == result["timeline"] == []


@pytest.mark.integration
def test_readonly_literal_sql_retains_effective_dates_and_inference_counts(readonly_pg):
    rows = fixture_rows()
    rows[0].update(effective_date="2018-10-01", date_basis="inferred:archive", inferred_tier="B",
                   inferred_date="2018-10-01")
    result = FixtureDatabase(readonly_pg, rows).dashboard(Filters(dataset="all", include_inferred_dates=True))
    effective = [{**row, "date": row["effective_date"]} for row in rows]
    assert result["stats"] == summarize(effective)
    assert result["timeline"] == yearly_timeline(effective)
    assert result["stats"]["inferred_dates"] == {"B": 1}
    assert result["page"]["rows"] == ordered(rows, "date", True)[:20]


@pytest.mark.integration
def test_readonly_explain_social_scalar_runs_only_for_twenty_page_rows(readonly_pg):
    db = FixtureDatabase(readonly_pg, fixture_rows())
    result = db.dashboard(Filters(dataset="social"), limit=20)
    statement, params = db.calls[1]
    plan = readonly_pg.execute("EXPLAIN (ANALYZE,FORMAT JSON) " + statement, params).fetchone()["QUERY PLAN"][0]["Plan"]

    def marker_loops(node):
        own = [node["Actual Loops"]] if node.get("Function Name") == "generate_series" else []
        return own + [loops for child in node.get("Plans", []) for loops in marker_loops(child)]

    assert result["page"]["total"] > len(result["page"]["rows"]) == 20
    assert marker_loops(plan) == [20]
