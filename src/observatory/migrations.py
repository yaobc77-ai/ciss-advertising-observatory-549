"""Ordered, checksum-verified PostgreSQL migrations; caller owns the transaction."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from psycopg import sql

MIGRATION_DIRECTORY = Path(__file__).with_name("migrations")
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_SCHEMA_STATEMENT = re.compile(
    # A transaction-local memory setting speeds index builds and changes no structure.
    r"^(?:CREATE\s+(?:EXTENSION|TABLE|(?:UNIQUE\s+)?INDEX)\b|ALTER\s+TABLE\b"
    r"|SET\s+LOCAL\s+maintenance_work_mem\s*=\s*'\d+MB'$)",
    re.IGNORECASE,
)


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


def _schema_structure(conn, schema: str) -> dict:
    """Catalog-only signature; object names do not affect equivalent DDL."""
    def normalized(value):
        if isinstance(value, str):
            return value.replace(f'"{schema}".', "").replace(f"{schema}.", "")
        return value

    tables = conn.execute("""
        SELECT c.oid,c.relname,c.relkind,c.relpersistence,c.relrowsecurity,c.relforcerowsecurity,
               c.relispartition,pg_get_partkeydef(c.oid) AS partition_key,
               pg_get_expr(c.relpartbound,c.oid,true) AS partition_bound
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f')
          AND c.relname <> 'schema_migrations' ORDER BY c.relname
    """, (schema,)).fetchall()
    result = {}
    for table in tables:
        oid = table["oid"]
        columns = conn.execute("""
            SELECT a.attnum,a.attname,format_type(a.atttypid,a.atttypmod) AS type,
                   a.attnotnull,a.attidentity,a.attgenerated,
                   pg_get_expr(d.adbin,d.adrelid) AS default_expr,
                   a.attcollation::regcollation::text AS collation
            FROM pg_attribute a LEFT JOIN pg_attrdef d
              ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum
        """, (oid,)).fetchall()
        constraints = conn.execute("""
            SELECT contype,convalidated,condeferrable,condeferred,
                   pg_get_constraintdef(oid,true) AS definition
            FROM pg_constraint WHERE conrelid=%s
        """, (oid,)).fetchall()
        indexes = conn.execute("""
            SELECT i.indisvalid,i.indisready,i.indislive,i.indisunique,i.indisprimary,
                   i.indisreplident,i.indnullsnotdistinct,
                   am.amname,pg_get_indexdef(i.indexrelid) AS definition
            FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
              JOIN pg_am am ON am.oid=c.relam WHERE i.indrelid=%s
        """, (oid,)).fetchall()
        triggers = conn.execute("""
            SELECT tgenabled,pg_get_triggerdef(oid,true) AS definition
            FROM pg_trigger WHERE tgrelid=%s AND NOT tgisinternal
        """, (oid,)).fetchall()
        policies = conn.execute("""
            SELECT polcmd,polpermissive,polroles,
                   pg_get_expr(polqual,polrelid) AS qual,
                   pg_get_expr(polwithcheck,polrelid) AS with_check
            FROM pg_policy WHERE polrelid=%s
        """, (oid,)).fetchall()
        sequences = conn.execute("""
            SELECT a.attname,format_type(s.seqtypid,NULL) AS type,
                   s.seqstart,s.seqincrement,s.seqmax,s.seqmin,s.seqcache,s.seqcycle
            FROM pg_depend d JOIN pg_sequence s ON s.seqrelid=d.objid
              JOIN pg_attribute a ON a.attrelid=d.refobjid AND a.attnum=d.refobjsubid
            WHERE d.classid='pg_class'::regclass AND d.refclassid='pg_class'::regclass
              AND d.refobjid=%s AND d.deptype IN ('a','i')
        """, (oid,)).fetchall()
        inheritance = conn.execute("""
            SELECT inhparent::regclass::text AS parent,inhrelid::regclass::text AS child,inhseqno
            FROM pg_inherits WHERE inhparent=%s OR inhrelid=%s
        """, (oid, oid)).fetchall()
        # Normalize index names only. The key, method, predicate and included
        # columns remain part of the comparison.
        for index in indexes:
            index["definition"] = re.sub(
                r"^CREATE (UNIQUE )?INDEX \S+ ON ",
                lambda match: "CREATE " + (match[1] or "") + "INDEX ON ",
                index["definition"],
            )
        def rows(items):
            return sorted(
                (tuple((key, normalized(value)) for key, value in item.items()) for item in items),
                key=repr,
            )
        result[table["relname"]] = {
            "table": tuple((key, value) for key, value in table.items() if key not in {"oid", "relname"}),
            "columns": rows(columns), "constraints": rows(constraints),
            "indexes": rows(indexes), "triggers": rows(triggers), "policies": rows(policies),
            "sequences": rows(sequences),
            "inheritance": rows(inheritance),
        }
    return result


def _adoptable_prefix(conn, migrations: list[Migration]) -> int:
    """Prove an untracked schema matches a complete historical prefix.

    Replay into an owned reference schema rather than guessing from a column or
    blindly suppressing DuplicateColumn. No business rows are read or changed.
    Unrelated tables may coexist; every object on migration-managed tables must
    match. The caller's search path is restored even when validation fails.
    """
    target = conn.execute("SELECT current_schema() AS name").fetchone()["name"]
    original = conn.execute("SHOW search_path").fetchone()["search_path"]
    reference = "obs_migration_reference_" + uuid4().hex
    if not _schema_structure(conn, target):
        return 0
    for migration in migrations:
        # Structural equality cannot prove that a data backfill already ran.
        # Fail closed for unsupported SQL instead of replaying it as evidence.
        without_comments = re.sub(r"/\*.*?\*/|--[^\n]*", "", migration.sql, flags=re.DOTALL)
        statements = [statement.strip() for statement in without_comments.split(";") if statement.strip()]
        if any(not _SCHEMA_STATEMENT.match(statement)
               or re.search(r"\bAS\s+(?:SELECT|WITH|VALUES)\b", statement, re.IGNORECASE)
               for statement in statements):
            raise ValueError("Structural adoption requires supported schema-only migration SQL")
    states = []
    # A savepoint removes the reference and restores search_path on SQL errors.
    with conn.transaction():
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(reference)))
        conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(reference)))
        for migration in migrations:
            conn.execute(migration.sql)
            states.append(_schema_structure(conn, reference))
        managed = set(states[-1])
        existing = conn.execute("""
            SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND c.relkind IN ('r','p') AND c.relname=ANY(%s)
            ORDER BY c.relname
        """, (target, sorted(managed))).fetchall()
        if existing:
            conn.execute(sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(
                sql.SQL(",").join(sql.Identifier(target, row["relname"]) for row in existing),
            ))
        conn.execute("SELECT set_config('search_path',%s,true)", (original,))
        live = _schema_structure(conn, target)
        actual = {name: structure for name, structure in live.items() if name in managed}
        adopted = 0 if not actual else None
        for version, expected in reversed(list(enumerate(states, 1))):
            if actual == expected:
                adopted = version
                break
        if adopted is None:
            raise ValueError(
                "Untracked database does not match a complete migration schema; "
                "refusing to adopt a partial or incompatible installation"
            )
        conn.execute("SELECT set_config('search_path',%s,true)", (original,))
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(reference)))
    return adopted


def run_migrations(conn, directory: Path = MIGRATION_DIRECTORY) -> dict:
    """Apply missing files atomically in the surrounding connection transaction.

    Untracked installations are adopted only after structural verification.
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
    adopted_versions = []
    if not applied:
        adopted = _adoptable_prefix(conn, migrations)
        for migration in migrations[:adopted]:
            conn.execute(
                "INSERT INTO schema_migrations(version,name,checksum) VALUES (%s,%s,%s)",
                (migration.version, migration.name, migration.checksum),
            )
            newly_applied.append(migration.version)
            adopted_versions.append(migration.version)
        applied = _applied(conn)
    for migration in migrations[len(applied):]:
        conn.execute(migration.sql)
        conn.execute(
            "INSERT INTO schema_migrations(version,name,checksum) VALUES (%s,%s,%s)",
            (migration.version, migration.name, migration.checksum),
        )
        newly_applied.append(migration.version)
    return {"applied": newly_applied, "adopted": adopted_versions, "current_version": len(migrations)}
