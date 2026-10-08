"""Synthetic schema-only migration checks on an explicit private cluster."""

from uuid import uuid4

import pytest
from postgres_fixtures import configured_test_url, verified_test_connection
from psycopg import sql

from observatory.migrations import discover_migrations, migration_status, run_migrations

pytestmark = pytest.mark.integration


@pytest.fixture()
def connection():
    url = configured_test_url()
    schema = "obs_migration_test_" + uuid4().hex
    with verified_test_connection(url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with verified_test_connection(url, schema) as conn:
            yield conn
    finally:
        with verified_test_connection(url) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.parametrize("version", [m.version for m in discover_migrations()])
def test_adopts_each_complete_prefix_and_preserves_synthetic_rows(connection, version):
    for migration in discover_migrations()[:version]:
        connection.execute(migration.sql)
    connection.execute("INSERT INTO records VALUES ('synthetic','native','v1',true)")
    connection.execute("INSERT INTO record_versions(version_id,record_id,body,body_hash,payload) "
                       "VALUES ('v1','synthetic','fabricated text','synthetic-hash','{}')")
    connection.execute("CREATE TABLE unrelated_fixture (id integer)")
    connection.commit()
    before = connection.execute("SELECT * FROM record_versions").fetchall()
    result = run_migrations(connection)
    assert result["current_version"] == len(discover_migrations())
    assert result["applied"] == [m.version for m in discover_migrations()]
    assert result["adopted"] == list(range(1, version + 1))
    assert connection.execute("SELECT * FROM record_versions").fetchall() == before
    assert run_migrations(connection)["applied"] == []
    assert migration_status(connection)["pending"] == []
    assert connection.execute("SELECT count(*) AS n FROM pg_namespace "
                              "WHERE nspname LIKE 'obs_migration_reference_%'").fetchone()["n"] == 0


def test_equivalent_index_and_constraint_names_are_adopted(connection):
    for migration in discover_migrations():
        connection.execute(migration.sql)
    connection.execute("ALTER INDEX chunks_source_observation RENAME TO equivalent_observation_index")
    connection.execute("ALTER TABLE chunks RENAME CONSTRAINT chunks_source_observation_complete TO equivalent_constraint")
    assert run_migrations(connection)["current_version"] == len(discover_migrations())


@pytest.mark.parametrize("change", [
    "ALTER TABLE chunks ADD COLUMN source_observation_id text",
    "DROP INDEX chunks_current",
    "ALTER TABLE records ALTER COLUMN active DROP NOT NULL",
    "ALTER TABLE chunks DROP CONSTRAINT chunks_check",
    "CREATE INDEX unexpected_chunk_index ON chunks(text_hash)",
    "ALTER SEQUENCE imports_import_id_seq INCREMENT BY 2",
    "CREATE TABLE unexpected_child(extra text) INHERITS(records)",
])
def test_refuses_partial_or_incompatible_untracked_structure(connection, change):
    # Every migration before 0005, whose column the first change adds by hand.
    for migration in discover_migrations()[:4]:
        connection.execute(migration.sql)
    connection.execute(change)
    connection.commit()
    with pytest.raises(ValueError, match="partial or incompatible"):
        with connection.transaction():
            run_migrations(connection)
    assert connection.execute("SELECT to_regclass(format('%I.schema_migrations',current_schema())) AS name").fetchone()["name"] is None
    assert connection.execute("SELECT count(*) AS n FROM pg_namespace "
                              "WHERE nspname LIKE 'obs_migration_reference_%'").fetchone()["n"] == 0


@pytest.mark.parametrize("change", [
    "ALTER TABLE chunks ALTER COLUMN source_version_id TYPE varchar(64)",
    "ALTER TABLE chunks DROP CONSTRAINT chunks_source_observation_complete",
    "DROP INDEX chunks_source_observation",
    "ALTER TABLE chunks ADD CONSTRAINT unexpected_provenance CHECK(source_version_id IS NULL)",
])
def test_refuses_incompatible_complete_observation_schema(connection, change):
    for migration in discover_migrations():
        connection.execute(migration.sql)
    connection.execute(change)
    connection.commit()
    with pytest.raises(ValueError, match="partial or incompatible"):
        with connection.transaction():
            run_migrations(connection)


def test_does_not_adopt_or_replay_data_migrations_as_structural_evidence(connection, tmp_path):
    migrations = discover_migrations()
    for migration in migrations:
        connection.execute(migration.sql)
        (tmp_path / migration.name).write_text(migration.sql, encoding="utf-8")
    connection.commit()
    next_version = len(migrations) + 1
    (tmp_path / f"{next_version:04d}_data.sql").write_text(
        "INSERT INTO records VALUES ('must-not-run','native','v1',true);", encoding="utf-8",
    )
    with pytest.raises(ValueError, match="schema-only"):
        with connection.transaction():
            run_migrations(connection, tmp_path)
    assert connection.execute("SELECT count(*) AS n FROM records").fetchone()["n"] == 0
