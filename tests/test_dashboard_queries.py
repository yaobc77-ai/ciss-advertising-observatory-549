"""Dashboard aggregate/page equivalence; integration uses an isolated test schema."""

import os
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from observatory.analytics import (
    historical_label_distribution,
    sponsor_publisher_matrix,
    sponsor_publisher_matrix_from_counts,
    yearly_timeline,
)
from observatory.config import Settings
from observatory.db import Database
from observatory.models import Filters, RecordInput
from observatory.service import Service, summarize


@pytest.fixture(scope="module")
def dashboard_db():
    Settings.from_env()
    url = os.environ.get("OBS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("OBS_TEST_DATABASE_URL is not configured")
    assert conninfo_to_dict(url).get("dbname", "").startswith("obs_test"), (
        "Refusing writes outside a disposable test database"
    )
    # Own schema prevents concurrent integration tests from truncating this data.
    schema = "dashboard_test_" + uuid4().hex
    base = Database(url)
    with base.connect() as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    db = Database(make_conninfo(url, options=f"-c search_path={schema}"))
    try:
        with db.connect() as conn:
            conn.execute("""CREATE TABLE records (
                record_id text PRIMARY KEY, dataset text, current_version text, active boolean DEFAULT true);
                CREATE TABLE record_versions (version_id text PRIMARY KEY, payload jsonb);
                CREATE TABLE annotations (version_id text, ordinal integer, payload jsonb);
                CREATE TABLE date_inferences (version_id text, method text, tier text,
                    inferred_date date, precision text, review_state text, evidence jsonb);""")
            for index in range(125):
                record = RecordInput(
                    record_id=f"native-{index:03}", dataset="native",
                    sponsor=("exxonmobil", "cera", "", "O'Reilly")[index % 4],
                    publisher=("Forbes", "CNBC", "")[index % 3],
                    keyword=("energy", "oil")[index % 2],
                    title=f"Article {index:03}",
                    published_at=None if index % 5 == 0 else date(2020 + index % 3, 4, 5),
                    url="https://example.org/article", archive_url="javascript:alert(1)",
                    retrievable=index % 7 != 0,
                    raw={"secret": "must not leave the public projection"},
                    annotations=[{"version": "claims-calibrated", "labels": (
                        ["green.claim", "ff.claim", "green.claim"] if index % 2 else []
                    )}],
                )
                _insert(conn, record)
            _insert(conn, RecordInput(record_id="social-1", dataset="social", platform="Facebook", account="Example"))
            _insert(conn, RecordInput(record_id="uncountable", dataset="native", countable=False))
            _insert(conn, RecordInput(record_id="inactive", dataset="native"))
            conn.execute("UPDATE records SET active=false WHERE record_id='inactive'")
            conn.execute("INSERT INTO record_versions VALUES ('old-native',%s)", (
                Jsonb({"sponsor": "Old sponsor", "countable": True}),
            ))
        yield db
    finally:
        with base.connect() as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _insert(conn, record):
    version = f"v-{record.record_id}"
    conn.execute("INSERT INTO records(record_id,dataset,current_version) VALUES (%s,%s,%s)",
                 (record.record_id, record.dataset, version))
    conn.execute("INSERT INTO record_versions VALUES (%s,%s)",
                 (version, Jsonb(record.model_dump(mode="json"))))
    for index, annotation in enumerate(record.annotations):
        conn.execute("INSERT INTO annotations VALUES (%s,%s,%s)",
                     (version, index, Jsonb(annotation)))


@pytest.mark.integration
@pytest.mark.parametrize("filters", [
    Filters(), Filters(dataset="all"), Filters(dataset="social"),
    Filters(sponsors=["O'Reilly"]), Filters(sponsors=["(Unknown)"]),
    Filters(sponsors=["x' OR 1=1 --"]), Filters(labels=["green.claim"]),
    Filters(date_from=date(2021, 1, 1), date_to=date(2022, 12, 31), include_unknown_dates=False),
    Filters(record_ids=["native-001", "native-002"]),
])
def test_sql_aggregates_match_existing_record_semantics(dashboard_db, filters):
    rows = dashboard_db.public_rows(filters)
    result = dashboard_db.dashboard(filters)
    assert result["stats"] == summarize(rows)
    assert result["timeline"] == yearly_timeline(rows)
    assert result["labels"] == historical_label_distribution(rows)
    assert result["matrix"] == sponsor_publisher_matrix(rows)
    assert result["page"] == {"rows": rows[:20], "total": len(rows), "offset": 0}


@pytest.mark.integration
def test_pages_are_bounded_stable_and_do_not_expose_internal_fields(dashboard_db):
    filters = Filters()
    all_rows = dashboard_db.public_rows(filters)
    pages = [dashboard_db.public_page(filters, offset=index, limit=20)
             for index in range(0, len(all_rows), 20)]
    assert [row for page in pages for row in page["rows"]] == all_rows
    assert all(page["total"] == 125 for page in pages)
    assert len(dashboard_db.public_page(filters, limit=999)["rows"]) == 100
    stale_page = dashboard_db.public_page(filters, offset=999)
    assert stale_page["offset"] == 120
    assert stale_page["rows"] == all_rows[120:]
    empty_page = dashboard_db.public_page(Filters(record_ids=["does-not-exist"]), offset=999)
    assert empty_page == {"rows": [], "total": 0, "offset": 0}
    assert "must not leave" not in str(pages)
    assert "body" not in pages[0]["rows"][0]
    clamped = dashboard_db.dashboard(filters, offset=999)
    assert clamped["page"]["offset"] == 120
    assert len(clamped["page"]["rows"]) == 5
    ascending = dashboard_db.public_page(filters, sort_by="title", descending=False)
    assert ascending["rows"][0]["record_id"] == "native-000"


@pytest.mark.integration
def test_facets_and_network_are_source_listed_counts(dashboard_db):
    rows = dashboard_db.public_rows(Filters())
    result = dashboard_db.facets("native")
    for name, field in (("sponsors", "sponsor"), ("publishers", "publisher"),
                        ("platforms", "platform"), ("keywords", "keyword")):
        assert result[name] == sorted({row.get(field) or "(Unknown)" for row in rows})
    assert result["labels"] == ["ff.claim", "green.claim"]
    network = dashboard_db.network(Filters(), limit=2)
    expected = summarize(rows)["relationships"]
    assert network == {
        "relationships": expected[:2], "total_relationships": len(expected),
        "total_records": len(rows), "truncated": True,
    }
    with pytest.raises(ValueError, match="native"):
        dashboard_db.network(Filters(dataset="social"))


@pytest.mark.integration
def test_dashboard_uses_one_snapshot_during_concurrent_import(dashboard_db, monkeypatch):
    original_connect = dashboard_db.connect
    before = dashboard_db.public_rows(Filters())
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
            if "AS unknown_dates" in statement and not inserted:
                with original_connect() as other:
                    _insert(other, RecordInput(record_id="concurrent", dataset="native", sponsor="Concurrent"))
                inserted = True
            return result

    monkeypatch.setattr(dashboard_db, "connect", Connection)
    try:
        result = dashboard_db.dashboard(Filters())
        assert inserted
        assert result["stats"] == summarize(before)
        assert result["matrix"] == sponsor_publisher_matrix(before)
        assert result["page"]["total"] == len(before)
    finally:
        with original_connect() as conn:
            conn.execute("DELETE FROM records WHERE record_id='concurrent'")
            conn.execute("DELETE FROM record_versions WHERE version_id='v-concurrent'")


@pytest.mark.parametrize("kwargs", [
    {"limit": 0}, {"limit": -1}, {"limit": True}, {"offset": -1},
    {"offset": 1.5}, {"sort_by": "date;DROP TABLE records"}, {"descending": "DESC"},
])
def test_bad_pagination_arguments_fail_before_connect(kwargs):
    with pytest.raises(ValueError):
        Database("").public_page(Filters(), **kwargs)
    with pytest.raises(ValueError):
        Database("").dashboard(Filters(), **kwargs)


def test_weighted_matrix_matches_records_without_expanding_large_counts():
    weighted = sponsor_publisher_matrix_from_counts([
        {"sponsor": "cera", "publisher": "Forbes", "count": 1000000},
        {"sponsor": "", "publisher": "CNBC", "count": 2},
    ])
    assert weighted["total"] == 1000002
    assert weighted["row_totals"] == [1000000, 2]
    assert weighted["notes"]
    with pytest.raises(ValueError):
        sponsor_publisher_matrix_from_counts([{"count": -1}])


@pytest.mark.parametrize("show_links", [True, False])
def test_service_sanitizes_paged_results_and_uses_aggregate_methods(show_links):
    def page(*args):
        return {"rows": [{"url": "https://example.org/a", "archive_url": "javascript:alert(1)"}], "total": 1}

    db = SimpleNamespace(
        public_page=page,
        dashboard=lambda *args: {"page": page(), "stats": {"total": 1}},
        facets=lambda dataset: {"labels": ["green.claim"]},
    )
    service = Service(Settings(show_source_links=show_links), db=db, rag=object())
    for result in (service.page(Filters()), service.dashboard(Filters())["page"]):
        assert result["rows"][0]["url"] == ("https://example.org/a" if show_links else "")
        assert result["rows"][0]["archive_url"] == ""
    assert service.statistics(Filters()) == {"total": 1}
    assert service.facets("native") == {"labels": ["green.claim"]}
