"""Operate only the PostgreSQL cluster belonging to this project (Windows)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
BIN = RUNTIME / "postgres" / "Library" / "bin"
DATA = RUNTIME / "pgdata"
MARKER = RUNTIME / "postgres-cluster.json"
SECRET_DIR = RUNTIME / "secrets"
SECRET_FILE = SECRET_DIR / "postgres.json"
ENV_FILE = ROOT / ".env"
LOG = RUNTIME / "postgres.log"
HOST = "127.0.0.1"
PORT = 55432
DATABASE = "observatory"
TEST_DATABASE = "obs_test"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(command: list[str], *, env=None, stdout=None, check=True, background=False):
    result = subprocess.run(
        command,
        env=env,
        stdout=subprocess.DEVNULL if background else (stdout if stdout is not None else subprocess.PIPE),
        stderr=subprocess.DEVNULL if background else subprocess.PIPE,
        text=stdout is None,
        encoding="utf-8" if stdout is None else None,
        errors="replace" if stdout is None else None,
        creationflags=NO_WINDOW,
        cwd=ROOT,
    )
    if check and result.returncode:
        raise RuntimeError(f"{Path(command[0]).name} failed (exit {result.returncode}); check the local PostgreSQL log. No credentials were printed.")
    return result


def restrict_acl(path: Path, *, directory=False):
    """Keep credential files accessible to the current Windows account only."""
    identity = run(["whoami.exe", "/user", "/fo", "csv", "/nh"]).stdout
    match = re.search(r"S-1-\d+(?:-\d+)+", identity)
    if not match:
        raise RuntimeError("Could not determine the current Windows SID to protect credentials.")
    rights = "(OI)(CI)F" if directory else "F"
    run(["icacls.exe", str(path), "/inheritance:r", "/grant:r", f"*{match[0]}:{rights}"])


def private_settings(*, create=False):
    if not SECRET_FILE.exists():
        if not create:
            raise RuntimeError("Project PostgreSQL credentials are missing. Existing data will not be reinitialized.")
        SECRET_DIR.mkdir(parents=True, exist_ok=True)
        restrict_acl(SECRET_DIR, directory=True)
        settings = {
            "admin_user": "observatory_admin",
            "admin_password": secrets.token_urlsafe(36),
            "app_user": "observatory_app",
            "app_password": secrets.token_urlsafe(36),
        }
        with SECRET_FILE.open("x", encoding="utf-8") as stream:
            json.dump(settings, stream, indent=2)
    settings = json.loads(SECRET_FILE.read_text(encoding="utf-8"))
    if not all(settings.get(k) for k in ("admin_user", "admin_password", "app_user", "app_password")):
        raise RuntimeError("Incomplete project credentials; refusing to rotate or replace them automatically.")
    return settings


def connection(database=DATABASE, *, admin=False, password=None):
    settings = private_settings()
    prefix = "admin" if admin else "app"
    return psycopg.connect(
        host=HOST,
        port=PORT,
        dbname=database,
        user=settings[f"{prefix}_user"],
        password=password if password is not None else settings[f"{prefix}_password"],
        connect_timeout=5,
        autocommit=True,
    )


def verify_marker():
    if not MARKER.is_file() or not (DATA / "PG_VERSION").is_file():
        raise RuntimeError("The project cluster has not been initialized. Run Start-Postgres.ps1.")
    marker = json.loads(MARKER.read_text(encoding="utf-8"))
    if Path(marker["data_directory"]).resolve() != DATA.resolve() or marker.get("port") != PORT:
        raise RuntimeError("Cluster ownership marker does not match this project; refusing to operate.")


def is_running():
    return run([str(BIN / "pg_ctl.exe"), "status", "-D", str(DATA)], check=False).returncode == 0


def verify_server():
    verify_marker()
    with connection("postgres", admin=True) as conn:
        row = conn.execute("SELECT current_setting('data_directory'), current_setting('listen_addresses'), current_setting('port')").fetchone()
    if Path(row[0]).resolve() != DATA.resolve() or row[1] != HOST or int(row[2]) != PORT:
        raise RuntimeError("Connected server is not the expected loopback-only project cluster.")


def initialize():
    if (DATA / "PG_VERSION").exists():
        verify_marker()
        return
    if DATA.exists() and any(DATA.iterdir()):
        raise RuntimeError("PostgreSQL data directory is nonempty; refusing to initialize or overwrite it.")
    if MARKER.exists():
        raise RuntimeError("A cluster marker exists without PG_VERSION; inspect the existing state manually.")
    if not (BIN / "initdb.exe").is_file():
        raise RuntimeError("PostgreSQL runtime is missing. Run Setup-Postgres.ps1 first.")
    settings = private_settings(create=True)
    password_file = SECRET_DIR / "initdb-password.txt"
    with password_file.open("x", encoding="utf-8") as stream:
        stream.write(settings["admin_password"] + "\n")
    try:
        run([
            str(BIN / "initdb.exe"), "-D", str(DATA),
            "--username", settings["admin_user"], "--pwfile", str(password_file),
            "--auth=scram-sha-256", "--encoding=UTF8", "--locale=C", "--data-checksums",
        ])
    finally:
        password_file.unlink(missing_ok=True)
    with (DATA / "postgresql.conf").open("a", encoding="utf-8") as stream:
        stream.write(
            "\n# Local observatory project settings\n"
            f"listen_addresses = '{HOST}'\nport = {PORT}\n"
            "password_encryption = 'scram-sha-256'\n"
            "max_connections = 30\nshared_buffers = '128MB'\n"
            "log_statement = 'none'\nlog_min_error_statement = 'panic'\n"
        )
    (DATA / "pg_hba.conf").write_text(
        "# Project-only IPv4 loopback access; all other connections have no matching rule.\n"
        "host all all 127.0.0.1/32 scram-sha-256\n", encoding="utf-8"
    )
    MARKER.write_text(json.dumps({
        "data_directory": str(DATA), "port": PORT, "host": HOST,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")


def provision_database():
    settings = private_settings()
    with connection("postgres", admin=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (settings["app_user"],)).fetchone():
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}").format(
                sql.Identifier(settings["app_user"]), sql.Literal(settings["app_password"])
            ))
        existing = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (DATABASE,)).fetchone()
        if existing is None:
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(DATABASE), sql.Identifier(settings["app_user"])))
        elif existing[0] != settings["app_user"]:
            raise RuntimeError("The existing observatory database has a different owner; refusing to modify it.")
    with connection(admin=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    with connection() as conn:
        if conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname=current_user").fetchone()[0]:
            raise RuntimeError("Application role unexpectedly has superuser privileges.")


def update_database_env(variable, database):
    """Write one known project database URL while retaining all unrelated lines."""
    settings = private_settings()
    url = f"postgresql://{quote(settings['app_user'], safe='')}:{quote(settings['app_password'], safe='')}@{HOST}:{PORT}/{database}"
    existing = ENV_FILE.read_text(encoding="utf-8-sig") if ENV_FILE.exists() else ""
    lines = existing.splitlines(keepends=True)
    replacement = f"{variable}={url}\n"
    updated = []
    replaced = False
    for line in lines:
        if re.match(rf"^\s*(?:export\s+)?{re.escape(variable)}\s*=", line):
            if not replaced:
                updated.append(replacement)
                replaced = True
        else:
            updated.append(line)
    if not replaced:
        if updated and not updated[-1].endswith(("\n", "\r")):
            updated.append("\n")
        updated.append(replacement)
    staging = SECRET_DIR / "project.env.pending"
    staging.write_text("".join(updated), encoding="utf-8")
    os.replace(staging, ENV_FILE)
    restrict_acl(ENV_FILE)


def update_env():
    update_database_env("OBS_DATABASE_URL", DATABASE)


def setup_test_db():
    """Create only the fixed test database; existing test contents are preserved."""
    verify_server()
    settings = private_settings()
    with connection("postgres", admin=True) as conn:
        role = conn.execute(
            "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname=%s",
            (settings["app_user"],),
        ).fetchone()
        if role is None or any(role):
            raise RuntimeError("The expected restricted application role is missing or has elevated privileges; run or review project setup first.")
        owner = conn.execute(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (TEST_DATABASE,)
        ).fetchone()
        if owner is None:
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(TEST_DATABASE), sql.Identifier(settings["app_user"])
            ))
        elif owner[0] != settings["app_user"]:
            raise RuntimeError("The existing obs_test database has a different owner; no database or connection setting was changed.")
    with connection(TEST_DATABASE, admin=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    with connection(TEST_DATABASE) as conn:
        vector = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
        if not vector or conn.execute("SELECT current_database()").fetchone()[0] != TEST_DATABASE:
            raise RuntimeError("The fixed test database could not be verified; its existing contents were retained.")
    update_database_env("OBS_TEST_DATABASE_URL", TEST_DATABASE)
    print("obs_test is ready with pgvector; existing contents were preserved. OBS_TEST_DATABASE_URL was written to the ignored .env (credentials hidden); the main database URL was left unchanged.")


def start():
    initialize()
    if not is_running():
        with socket.socket() as probe:
            if probe.connect_ex((HOST, PORT)) == 0:
                raise RuntimeError("Port 55432 is already occupied; no other process will be stopped.")
        run([str(BIN / "pg_ctl.exe"), "start", "-D", str(DATA), "-l", str(LOG), "-w", "-t", "30"], background=True)
    verify_server()
    provision_database()
    update_env()
    print("PostgreSQL is ready at 127.0.0.1:55432; observatory has pgvector. OBS_DATABASE_URL was written to the ignored .env (credentials hidden).")


def stop():
    verify_marker()
    if not is_running():
        print("The project PostgreSQL cluster is already stopped.")
        return
    verify_server()
    run([str(BIN / "pg_ctl.exe"), "stop", "-D", str(DATA), "-m", "fast", "-w", "-t", "30"])
    print("Stopped only this project's PostgreSQL cluster.")


def health():
    verify_server()
    with connection() as conn:
        version = conn.execute("SELECT version()").fetchone()[0]
        vector = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
        if not vector:
            raise RuntimeError("The vector extension is missing.")
        distance = conn.execute("SELECT '[1,2,3]'::vector <-> '[1,2,3]'::vector").fetchone()[0]
        user = conn.execute("SELECT current_user").fetchone()[0]
    with connection("postgres", admin=True) as conn:
        auth = [row[0] for row in conn.execute("SELECT auth_method FROM pg_hba_file_rules WHERE error IS NULL AND type IS NOT NULL")]
        auth_errors = conn.execute("SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL").fetchone()[0]
    if auth != ["scram-sha-256"] or auth_errors or distance != 0:
        raise RuntimeError("Authentication or vector smoke check failed.")
    try:
        with connection(password="intentionally-invalid-health-check"):
            pass
    except psycopg.OperationalError:
        password_rejected = True
    else:
        raise RuntimeError("An invalid password was accepted.")
    print(json.dumps({
        "status": "healthy", "postgresql": version, "pgvector": vector[0],
        "host": HOST, "port": PORT, "database": DATABASE, "app_role": user,
        "auth": "scram-sha-256", "invalid_password_rejected": password_rejected,
        "vector_distance_smoke": distance,
    }, ensure_ascii=False, indent=2))


def pg_environment():
    settings = private_settings()
    env = os.environ.copy()
    env.update(PGHOST=HOST, PGPORT=str(PORT), PGUSER=settings["admin_user"],
               PGPASSWORD=settings["admin_password"], PGCONNECT_TIMEOUT="5")
    return env


def backup(output_file=None, *, snapshot=None):
    verify_server()
    backup_dir = RUNTIME / "backups"
    backup_dir.mkdir(exist_ok=True)
    destination = Path(output_file).resolve() if output_file else backup_dir / f"observatory-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.dump"
    if not destination.is_relative_to(backup_dir.resolve()):
        raise RuntimeError("Backup output must stay inside this project's .runtime/backups directory.")
    if destination.exists():
        raise RuntimeError("Backup destination already exists; refusing to overwrite it.")
    if snapshot is not None and not re.fullmatch(r"[0-9A-Fa-f]+-[0-9A-Fa-f]+-\d+", snapshot):
        raise ValueError("Invalid exported PostgreSQL snapshot identifier.")
    command = [str(BIN / "pg_dump.exe"), "--format=custom", "--dbname", DATABASE]
    if snapshot is not None:
        command.extend(["--snapshot", snapshot])
    with destination.open("xb") as stream:
        run(command, env=pg_environment(), stdout=stream)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(destination.suffix + ".sha256").write_text(digest + "\n", encoding="ascii")
    print(f"Backup created: {destination}")
    return destination


def restore(backup_file, database):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", database) or database in {DATABASE, "postgres", "template0", "template1"}:
        raise RuntimeError("Choose an explicit new database name: lowercase letters, digits and underscores; existing project/system names are prohibited.")
    archive = Path(backup_file).resolve()
    if not archive.is_file():
        raise RuntimeError("Backup archive does not exist.")
    verify_server()
    with connection("postgres", admin=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
            raise RuntimeError("Target database already exists; restore never overwrites or drops a database.")
        run([str(BIN / "pg_restore.exe"), "--list", str(archive)])
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), sql.Identifier(private_settings()["app_user"])))
    try:
        run([str(BIN / "pg_restore.exe"), "--exit-on-error", "--single-transaction", "--dbname", database, str(archive)], env=pg_environment())
        with connection(database) as conn:
            if not conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone():
                raise RuntimeError("Restored database has no vector extension.")
    except Exception:
        print(f"Restore did not complete. New database {database} was retained for inspection; no existing database was changed.", file=sys.stderr)
        raise
    print(f"Backup restored to new database {database}; observatory and OBS_DATABASE_URL were left unchanged.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for command in ("start", "stop", "health", "setup-test-db"):
        commands.add_parser(command)
    backup_parser = commands.add_parser("backup")
    backup_parser.add_argument("--output-file")
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("--backup-file", required=True)
    restore_parser.add_argument("--database", required=True)
    args = parser.parse_args()
    try:
        if args.action == "backup":
            backup(args.output_file)
        elif args.action == "restore":
            restore(args.backup_file, args.database)
        else:
            {"start": start, "stop": stop, "health": health, "setup-test-db": setup_test_db}[args.action]()
    except psycopg.Error as exc:
        print(f"PostgreSQL operation failed ({type(exc).__name__}, SQLSTATE {exc.sqlstate or 'unavailable'}). Credentials and SQL text were withheld.", file=sys.stderr)
        return 1
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
