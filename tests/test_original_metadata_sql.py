"""Real metadata SELECTs over fabricated versions in an owned private schema."""

from uuid import uuid4

import pytest
from postgres_fixtures import configured_test_url, verified_test_connection
from psycopg import sql
from psycopg.conninfo import make_conninfo

from observatory.db import Database
from observatory.models import Filters, ImportBatch, RecordInput

pytestmark = pytest.mark.integration


@pytest.fixture()
def database():
    url = configured_test_url()
    schema = "obs_metadata_test_" + uuid4().hex
    with verified_test_connection(url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        db = Database(make_conninfo(url, options=f"-c search_path={schema},public"))
        db.initialize()
        yield db
    finally:
        with verified_test_connection(url) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def record(identifier, original_title, display_title="Display Nimbus"):
    return RecordInput(
        record_id=identifier, dataset="native", title=display_title,
        publisher="Synthetic outlet", sponsor="nimbus", keyword="Air",
        url=f"https://synthetic.example/{identifier}", body="Fabricated SQL metadata fixture.",
        countable=True, retrievable=False,
        raw={"metadata": {"title": original_title, "sponsor": "Nimbus LAB",
                          "disclosure language": "Paid collaboration", "disclosure location": "header"},
             "baseline": {"title": display_title, "sponsor": "nimbus"}},
    )


def test_real_original_metadata_uses_current_version_and_preserves_cell_provenance(database):
    initial = record("synthetic-meta", "Nimbus RAW")
    database.import_batch(ImportBatch(records=[initial]))
    filters = Filters(dataset="native")
    prior = database.original_record_metadata(filters, initial.record_id)
    assert prior["title"] == "Display Nimbus"
    origin = prior["metadata_origins"][0]
    title = next(cell for cell in origin["cells"] if cell["field"] == "title")
    assert title["value"] == "Nimbus RAW" and title["present"] is True
    assert title["payload_field_path"] == ["raw", "metadata", "title"]
    assert origin["origin_kind"] == "original_metadata" and origin["priority"] == 0
    assert prior["metadata_origins"][1]["origin_kind"] == "baseline_source_copy"
    replacement = record(initial.record_id, "Nimbus CURRENT", "Current display")
    database.import_batch(ImportBatch(records=[replacement]))
    current = database.original_record_metadata(filters, initial.record_id)
    assert current["version_id"] != prior["version_id"]
    assert current["title"] == "Current display"
    assert next(cell for cell in current["metadata_origins"][0]["cells"] if cell["field"] == "title")["value"] == "Nimbus CURRENT"
    assert database.original_record_metadata(Filters(dataset="social"), initial.record_id) is None
    assert database.original_record_metadata(Filters(dataset="native", record_ids=["absent"]), initial.record_id) is None


def test_real_find_records_prioritizes_exact_title_and_never_widens_scope(database):
    exact = record("synthetic-exact", "Nimbus RAW")
    contains = record("synthetic-contains", "Nimbus RAW extended")
    display_exact = record("synthetic-display", "Different RAW", "Nimbus RAW")
    database.import_batch(ImportBatch(records=[exact, contains, display_exact]))
    result = database.find_records(Filters(dataset="native"), "Nimbus RAW", 10)
    assert result["total_candidates"] == 2 and result["match_type"] == "exact"
    assert {row["record_id"] for row in result["rows"]} == {exact.record_id, display_exact.record_id}
    assert all(row["title_match"] == "exact" for row in result["rows"])
    restricted = database.find_records(Filters(dataset="native", record_ids=[contains.record_id]), "Nimbus RAW", 10)
    assert restricted["total_candidates"] == 1 and restricted["match_type"] == "literal_contains"
    assert [row["record_id"] for row in restricted["rows"]] == [contains.record_id]
    empty = database.find_records(Filters(dataset="social"), "Nimbus RAW", 10)
    assert empty["rows"] == [] and empty["total_candidates"] == 0
    database.import_batch(ImportBatch(records=[record(exact.record_id, "Changed RAW", "Changed display")]))
    result = database.find_records(Filters(dataset="native", record_ids=[exact.record_id]), "Nimbus RAW", 10)
    assert result["rows"] == [] and result["total_candidates"] == 0
