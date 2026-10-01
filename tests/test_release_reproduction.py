"""Release verification must catch missing wheel files and unsafe DB authority."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_current_release.py"
_SPEC = importlib.util.spec_from_file_location("release_verification", _SCRIPT)
verification = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verification)


def make_wheel(tmp_path, wheel_content=b"same"):
    root = tmp_path / "source"
    source = root / "src" / "observatory"
    (source / "migrations").mkdir(parents=True)
    (source / "assets").mkdir()
    (source / "__init__.py").write_bytes(b"same")
    (source / "migrations" / "0001_baseline.sql").write_bytes(b"SELECT 1;")
    (source / "assets" / "graph.js").write_bytes(b"const ready = true;")
    (source / "assets" / "tools.svg").write_bytes(b"<svg/>")
    wheel = tmp_path / "fixture.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("observatory/__init__.py", wheel_content)
        archive.writestr("observatory/migrations/0001_baseline.sql", b"SELECT 1;")
        archive.writestr("observatory/assets/graph.js", b"const ready = true;")
        archive.writestr("observatory/assets/tools.svg", b"<svg/>")
    return root, wheel


def test_inventory_covers_code_assets_and_migrations(tmp_path):
    root, wheel = make_wheel(tmp_path)
    assert len(verification.package_inventory(root, wheel)) == 4
    (root / "src" / "observatory" / "new.py").write_bytes(b"new")
    with pytest.raises(verification.VerificationError, match="wheel_package_inventory_mismatch"):
        verification.package_inventory(root, wheel)


def test_inventory_refuses_stale_wheel_bytes(tmp_path):
    root, wheel = make_wheel(tmp_path, b"stale")
    with pytest.raises(verification.VerificationError, match="wheel_source_mismatch"):
        verification.package_inventory(root, wheel)


@pytest.mark.parametrize("url", [
    "postgresql://localhost/observatory", "postgresql://localhost/obs_test_production-other",
    "postgresql://remote.example/obs_test", "dbname=obs_test host=127.0.0.1 hostaddr=203.0.113.1",
    "dbname=obs_test", "invalid connection string",
])
def test_database_authority_refuses_main_remote_and_implicit_hosts(url):
    with pytest.raises(verification.VerificationError):
        verification.test_database_url({"OBS_TEST_DATABASE_URL": url})


def test_database_authority_never_falls_back_to_application_url():
    with pytest.raises(verification.VerificationError, match="missing_test_database_url"):
        verification.test_database_url({"OBS_DATABASE_URL": "postgresql://localhost/obs_test"})
    url = "postgresql://localhost/obs_test_release"
    assert verification.test_database_url({"OBS_TEST_DATABASE_URL": url}) == (url, "obs_test_release")


def test_python_network_guard_refuses_model_transport():
    with pytest.raises(verification.VerificationError, match="offline_network_connection_refused"):
        verification.deny_python_network("socket.connect", (None, ("api.openai.com", 443)))
    verification.deny_python_network("import", ())


def test_application_table_inventory_includes_current_migrations_and_ledger():
    names = verification.application_table_names()
    assert {"schema_migrations", "records", "record_versions", "claims2_results"} <= set(names)
    assert names == sorted(set(names))


def test_application_table_inventory_tracks_new_migration_tables(monkeypatch):
    from observatory import migrations

    monkeypatch.setattr(migrations, "discover_migrations", lambda: [SimpleNamespace(sql=(
        "CREATE TABLE IF NOT EXISTS records (id text);\n"
        "CREATE TABLE new_review_history (id text);"
    ))])
    assert verification.application_table_names() == [
        "new_review_history", "records", "schema_migrations",
    ]


@pytest.mark.parametrize("declaration", [
    'CREATE TABLE IF NOT EXISTS "records" (id text);',
    "CREATE TABLE public.records (id text);",
    "CREATE TABLE archived_records AS SELECT * FROM records;",
])
def test_application_table_inventory_refuses_unsupported_names(monkeypatch, declaration):
    from observatory import migrations

    monkeypatch.setattr(migrations, "discover_migrations",
                        lambda: [SimpleNamespace(sql=declaration)])
    with pytest.raises(verification.VerificationError,
                       match="unsupported_migration_table_declaration"):
        verification.application_table_names()


class CatalogConnection:
    """Only read catalog queries are allowed unless a test explicitly records DDL."""

    def __init__(self, *, conflicts=(), resolved=()):
        self.conflicts = conflicts
        self.resolved = resolved
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, parameters=None):
        text = query if isinstance(query, str) else query.as_string()
        self.queries.append(text)
        if text == "SELECT current_database() AS name":
            rows = [{"name": "obs_test"}]
        elif "pg_extension" in text:
            rows = [{"extname": "vector"}]
        elif "n.nspname='public'" in text:
            rows = [row for row in self.conflicts if row["relname"] in parameters[0]]
        elif "to_regclass" in text:
            rows = self.resolved
        else:
            pytest.fail("Unexpected SQL; preflight must stop before DDL")
        return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: rows[0] if rows else None)


@pytest.mark.parametrize("name", ["records", "schema_migrations", "claims2_results"])
def test_public_conflict_stops_before_schema_creation_or_cli(monkeypatch, tmp_path, name):
    import psycopg

    connection = CatalogConnection(conflicts=[{"relname": name}])
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: connection)
    cli_calls = []
    monkeypatch.setattr(verification, "run_cli", lambda *args: cli_calls.append(args))
    with pytest.raises(verification.VerificationError,
                       match="public_application_relation_conflict"):
        verification.verify_database(tmp_path, "dbname=obs_test host=127.0.0.1", "obs_test")
    assert not cli_calls
    assert not any("CREATE SCHEMA" in query for query in connection.queries)


def test_public_preflight_ignores_unrelated_relations():
    connection = CatalogConnection(conflicts=[{"relname": "unrelated_notes"}])
    verification.require_no_public_application_relations(connection, ["records", "schema_migrations"])


@pytest.mark.parametrize("kind", ["r", "v", "m", "f", "S", "i", "p"])
def test_public_preflight_refuses_any_same_name_relation(kind):
    connection = CatalogConnection(conflicts=[{"relname": "records", "kind": kind}])
    with pytest.raises(verification.VerificationError,
                       match="public_application_relation_conflict"):
        verification.require_no_public_application_relations(connection, ["records"])


def test_schema_guard_accepts_complete_target_table_resolution():
    names = verification.application_table_names()
    connection = CatalogConnection(resolved=[
        {"name": name, "schema": "obs_release_selected", "kind": "r"} for name in names
    ])
    verification.require_application_table_schema(connection, "obs_release_selected", names)


@pytest.mark.parametrize("rows", [
    [],
    [{"name": "records", "schema": None, "kind": None}],
    [{"name": "records", "schema": "public", "kind": "r"}],
    [{"name": "records", "schema": "pg_temp_1", "kind": "r"}],
    [{"name": "records", "schema": "obs_release_selected", "kind": "v"}],
    [{"name": "records", "schema": "obs_release_selected", "kind": "f"}],
    [{"name": "records", "schema": "obs_release_selected", "kind": "p"}],
    [{"name": "different_table", "schema": "obs_release_selected", "kind": "r"}],
    [{"name": "records", "schema": "obs_release_selected", "kind": "r"}] * 2,
])
def test_schema_guard_refuses_missing_fallback_or_wrong_relation(rows):
    connection = CatalogConnection(resolved=rows)
    with pytest.raises(verification.VerificationError,
                       match="application_table_schema_mismatch"):
        verification.require_application_table_schema(connection, "obs_release_selected", ["records"])


def test_wrong_namespace_stops_before_repeated_migration_or_fixture_import(monkeypatch, tmp_path):
    import psycopg

    from observatory.db import Database

    schema = "obs_release_selected"
    names = verification.application_table_names()

    class SchemaConnection(CatalogConnection):
        def __init__(self):
            super().__init__(resolved=[
                {"name": name, "schema": "public" if name == "records" else schema, "kind": "r"}
                for name in names
            ])
            self.ddl = []

        def execute(self, query, parameters=None):
            text = query if isinstance(query, str) else query.as_string()
            if text.startswith(("CREATE SCHEMA", "DROP SCHEMA")):
                self.ddl.append(text)
                return SimpleNamespace()
            if text == "SELECT current_schema() AS name":
                return SimpleNamespace(fetchone=lambda: {"name": schema})
            return super().execute(query, parameters)

        def commit(self):
            pass

        def close(self):
            pass

    connection = SchemaConnection()
    monkeypatch.setattr(verification, "uuid4", lambda: SimpleNamespace(hex="selected"))
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: connection)
    monkeypatch.setattr(Database, "connect", lambda *args, **kwargs: connection)
    cli_calls = []

    def first_migration_only(*args):
        cli_calls.append(args)
        return {"current_version": 3, "pending": []}

    monkeypatch.setattr(verification, "run_cli", first_migration_only)
    with pytest.raises(verification.VerificationError,
                       match="application_table_schema_mismatch"):
        verification.verify_database(tmp_path, "dbname=obs_test host=127.0.0.1", "obs_test")
    assert cli_calls == [("migrate",)]
    assert len(connection.ddl) == 2
    assert all('"obs_release_selected"' in statement for statement in connection.ddl)
