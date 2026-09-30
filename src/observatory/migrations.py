"""Ordered, checksum-verified PostgreSQL migrations; caller owns the transaction."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

MIGRATION_DIRECTORY = Path(__file__).with_name("migrations")
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    sql: str


def discover_migrations(directory: Path = MIGRATION_DIRECTORY) -> list[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME.fullmatch(path.name)
        if not match:
            raise ValueError(f"Invalid migration filename: {path.name}")
        # Normalize Git's platform line endings before computing the immutable hash.
        sql = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        migrations.append(Migration(
            int(match[1]), path.name,
            hashlib.sha256(sql.encode("utf-8")).hexdigest(), sql,
        ))
    if not migrations or [m.version for m in migrations] != list(range(1, len(migrations) + 1)):
        raise ValueError("Migration versions must be unique and consecutive starting at 0001")
    return migrations


def _applied(conn) -> list[dict]:
    if conn.execute(
        "SELECT to_regclass(format('%I.schema_migrations',current_schema())) AS name"
    ).fetchone()["name"] is None:
        return []
    return conn.execute(
        "SELECT version,name,checksum,applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()


def _validate(migrations: list[Migration], applied: list[dict]) -> None:
    if [row["version"] for row in applied] != list(range(1, len(applied) + 1)):
        raise ValueError("Applied migrations are not an ordered prefix")
    if len(applied) > len(migrations):
        raise ValueError("Database schema is newer than this application's migration files")
    for row, migration in zip(applied, migrations):
        if row["name"] != migration.name or row["checksum"] != migration.checksum:
            raise ValueError(
                f"Applied migration {migration.version:04d} was modified; restore it and add a new migration"
            )


def migration_status(conn, directory: Path = MIGRATION_DIRECTORY) -> dict:
    """Read state without creating tables or applying pending migrations."""
    migrations = discover_migrations(directory)
    applied = _applied(conn)
    _validate(migrations, applied)
    return {
        "current_version": len(applied),
        "pending": [m.version for m in migrations[len(applied):]],
        "migrations": [
            {
                "version": m.version, "name": m.name, "checksum": m.checksum,
                "status": "applied" if m.version <= len(applied) else "pending",
            }
            for m in migrations
        ],
    }


def run_migrations(conn, directory: Path = MIGRATION_DIRECTORY) -> dict:
    """Apply missing files atomically in the surrounding connection transaction.

    The baseline uses idempotent DDL and adopts pre-migration installations.
    Existing records, immutable versions, usage and retrieval state are preserved.
    """
    migrations = discover_migrations(directory)
    conn.execute("SELECT pg_advisory_xact_lock(54901)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version integer PRIMARY KEY CHECK(version > 0),
            name text NOT NULL UNIQUE,
            checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    applied = _applied(conn)
    _validate(migrations, applied)
    newly_applied = []
    for migration in migrations[len(applied):]:
        conn.execute(migration.sql)
        conn.execute(
            "INSERT INTO schema_migrations(version,name,checksum) VALUES (%s,%s,%s)",
            (migration.version, migration.name, migration.checksum),
        )
        newly_applied.append(migration.version)
    return {"applied": newly_applied, "current_version": len(migrations)}
