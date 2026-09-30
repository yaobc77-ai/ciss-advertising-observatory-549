"""Migration/import checks use only an explicitly configured obs_test* database."""

import json
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from observatory.config import Settings
from observatory.db import Database
from observatory.import_records import load_records
from observatory.migrations import MIGRATION_DIRECTORY, migration_status, run_migrations
from observatory.models import Filters

pytestmark = pytest.mark.integration


@pytest.fixture()
def db():
    Settings.from_env()
    url = os.environ.get("OBS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("OBS_TEST_DATABASE_URL is not configured")
    assert conninfo_to_dict(url).get("dbname", "").startswith("obs_test"), "Refusing non-test database"
    schema = f"obs_pipeline_test_{uuid4().hex}"
    with psycopg.connect(url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        db = Database(make_conninfo(url, options=f"-c search_path={schema},public"))
        db.initialize()
        yield db
    finally:
        with psycopg.connect(url) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def batch_file(tmp_path, year, *, dataset="native"):
    path = tmp_path / f"{year}.jsonl"
    path.write_text(json.dumps({
        "record_id": f"source:{year}", "dataset": dataset,
        "url": f"https://example.org/{year}", "published_at": f"{year}-01-01",
        "body": "Preserve the original body and date.",
        "platform": "Instagram" if dataset == "social" else "",
    }) + "\n", encoding="utf-8")
    return path


def test_upsert_new_year_preserves_old_and_repeat_is_unchanged(db, tmp_path):
    old = load_records(batch_file(tmp_path, 2025))
    new = load_records(batch_file(tmp_path, 2026))
    db.import_batch(old)
    report = db.import_batch(new)
    assert report["deactivated"] == []
    assert {row["record_id"] for row in db.public_rows(Filters())} == {"source:2025", "source:2026"}
    assert db.import_batch(new)["unchanged"] == 1
    assert db.import_batch(new, snapshot_dataset="native")["deactivated"] == ["source:2025"]
    assert len(db.public_rows(Filters())) == 1


def test_record_id_cannot_move_between_datasets(db, tmp_path):
    db.import_batch(load_records(batch_file(tmp_path, 2026)))
    with pytest.raises(ValueError, match="dataset"):
        db.import_batch(load_records(batch_file(tmp_path, 2026, dataset="social")))
    assert len(db.public_rows(Filters())) == 1
    assert not db.public_rows(Filters(dataset="social"))


def test_migrations_adopt_existing_data_then_are_idempotent(db, tmp_path):
    db.import_batch(load_records(batch_file(tmp_path, 2025)))
    before = db.public_rows(Filters())
    with db.connect() as conn:
        # Simulate the pre-migration installation: schema and corpus already exist.
        conn.execute("DELETE FROM schema_migrations")
        assert run_migrations(conn)["applied"] == [1, 2]
    db.initialize()
    with db.connect() as conn:
        assert run_migrations(conn)["applied"] == []
        assert migration_status(conn)["pending"] == []
    assert db.public_rows(Filters()) == before


def test_migration_tamper_blocks_before_applying_new_sql(db, tmp_path):
    for file in MIGRATION_DIRECTORY.glob("*.sql"):
        (tmp_path / file.name).write_bytes(file.read_bytes())
    baseline = tmp_path / "0001_baseline.sql"
    baseline.write_text(baseline.read_text(encoding="utf-8") + "\n-- unauthorized modification\n", encoding="utf-8")
    with pytest.raises(ValueError, match="was modified"):
        with db.connect() as conn:
            run_migrations(conn, tmp_path)
    with db.connect() as conn:
        assert migration_status(conn)["current_version"] == 2


def test_failed_migration_rolls_back_sql_and_tracker(db, tmp_path):
    for file in MIGRATION_DIRECTORY.glob("*.sql"):
        (tmp_path / file.name).write_bytes(file.read_bytes())
    (tmp_path / "0003_failure.sql").write_text(
        "CREATE TABLE prototype_rollback_probe(id integer); SELECT 1/0;", encoding="utf-8",
    )
    with pytest.raises(psycopg.errors.DivisionByZero):
        with db.connect() as conn:
            run_migrations(conn, tmp_path)
    with db.connect() as conn:
        assert conn.execute("SELECT to_regclass('prototype_rollback_probe') AS name").fetchone()["name"] is None
        assert migration_status(conn)["current_version"] == 2
