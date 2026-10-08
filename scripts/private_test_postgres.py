"""Manage a private loopback PostgreSQL test cluster without using the main cluster.

Credentials and test.env stay in an ignored directory. No command prints a DSN,
loads the project .env, stops an unrelated process, or deletes existing data.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import secrets
import shlex
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

if __package__:
    from .local_postgres import restrict_acl, run
else:
    from local_postgres import restrict_acl, run

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / ".runtime" / "postgres18" / "Library" / "bin"
HOST = "127.0.0.1"
DATABASE = "obs_test"
ADMIN_USER = "obs_private_admin"
APP_USER = "obs_private_app"
TARGET_MAJOR = "18"
MARKER_KIND = "observatory-private-test-v1"
RESERVED_PORTS = {5432, 55432}


def prohibited_ports() -> set[int]:
    """Known main/default ports plus an explicitly supplied main connection port."""
    ports = set(RESERVED_PORTS)
    main_url = os.environ.get("OBS_DATABASE_URL")
    if main_url:
        try:
            target = conninfo_to_dict(main_url)
            port = target.get("port", "5432")
            if not port.isdecimal():
                raise ValueError
            ports.add(int(port))
        except (psycopg.Error, ValueError):
            raise RuntimeError("Cannot determine the explicitly configured main database port; no cluster was operated.") from None
    return ports


def choose_port() -> int:
    for _ in range(32):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((HOST, 0))
            port = probe.getsockname()[1]
        if port not in prohibited_ports():
            return port
    raise RuntimeError("Could not select a free private test port.")


def windows_argv(command_line: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(command_line)
    argc = ctypes.c_int()
    splitter = ctypes.windll.shell32.CommandLineToArgvW
    splitter.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    splitter.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv = splitter(command_line, ctypes.byref(argc))
    if not argv:
        raise RuntimeError("Could not inspect the private server process command line.")
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def process_info(pid: int) -> dict:
    if os.name != "nt":
        raise RuntimeError("Private cluster process verification requires Windows.")
    # pid is an integer from a validated pidfile; no filesystem commands or
    # credentials are interpolated into PowerShell source.
    command = (
        f"$taskProcess = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
        "$taskProcess | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
    )
    result = run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
                  "-WindowStyle", "Hidden", "-Command", command])
    try:
        details = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise RuntimeError("Could not inspect the private server process; no process was stopped.") from None
    if not isinstance(details, dict) or details.get("ProcessId") != pid:
        raise RuntimeError("The recorded private server process does not exist; no process was stopped.")
    return details


class PrivateTestCluster:
    def __init__(self, name: str = "default"):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name):
            raise ValueError("Use a private cluster name containing lowercase letters, digits and underscores.")
        self.name = name
        self.workspace = ROOT.resolve()
        self.directory = self.workspace / ".runtime" / "private_test_postgres" / name
        self.data = self.directory / "data"
        self.marker = self.directory / "cluster.json"
        self.credentials = self.directory / "credentials.json"
        self.env_file = self.directory / "test.env"
        self.log = self.directory / "postgres.log"
        self.assert_paths()

    def assert_paths(self):
        # A junction/symlink anywhere beneath the canonical workspace can turn
        # an apparently private path into a main cluster or an outside path.
        for path in (self.directory, self.data, self.marker, self.credentials,
                     self.env_file, self.log, self.directory / "receipts"):
            if path.resolve() != path or not path.is_relative_to(self.workspace):
                raise RuntimeError("Private test paths must stay at their own canonical workspace location; links are refused.")

    def verify_runtime(self):
        for name in ("postgres.exe", "initdb.exe", "pg_ctl.exe"):
            if not (BIN / name).is_file():
                raise RuntimeError("PostgreSQL 18 runtime is missing; prepare the reviewed project runtime first.")
        result = run([str(BIN / "postgres.exe"), "--version"])
        match = re.search(r"\bPostgreSQL\)?\s+(\d+)(?:\.\d+)*\b", result.stdout)
        if not match or match[1] != TARGET_MAJOR:
            raise RuntimeError("Private tests require PostgreSQL 18; no cluster was started or stopped.")

    def state(self) -> dict:
        self.assert_paths()
        if not self.marker.is_file() or not (self.data / "PG_VERSION").is_file():
            raise RuntimeError("Private test cluster is not initialized; run setup first.")
        state = json.loads(self.marker.read_text(encoding="utf-8"))
        if (state.get("kind") != MARKER_KIND
                or state.get("workspace") != str(self.workspace)
                or state.get("data_directory") != str(self.data)
                or state.get("host") != HOST
                or state.get("database") != DATABASE
                or type(state.get("port")) is not int
                or not 1 <= state["port"] <= 65535
                or state["port"] in prohibited_ports()):
            raise RuntimeError("Private cluster ownership/port marker is invalid; no cluster was operated.")
        if (self.data / "PG_VERSION").read_text(encoding="ascii").strip() != TARGET_MAJOR:
            raise RuntimeError("Private data major version differs; existing data was preserved.")
        self.verify_runtime()
        return state

    def settings(self) -> dict:
        self.assert_paths()
        settings = json.loads(self.credentials.read_text(encoding="utf-8"))
        if (settings.get("admin_user") != ADMIN_USER or settings.get("app_user") != APP_USER
                or not all(isinstance(settings.get(key), str) and settings[key]
                           for key in ("admin_password", "app_password"))):
            raise RuntimeError("Private credentials are incomplete; they were not rotated or replaced.")
        return settings

    def connect(self, database: str = DATABASE, *, admin: bool = False):
        if database not in {DATABASE, "postgres"}:
            raise ValueError("Private cluster commands may connect only to postgres or obs_test.")
        state, settings = self.state(), self.settings()
        prefix = "admin" if admin else "app"
        return psycopg.connect(
            host=HOST, hostaddr=HOST, port=state["port"], dbname=database,
            user=settings[f"{prefix}_user"], password=settings[f"{prefix}_password"],
            connect_timeout=5, autocommit=True,
        )

    def initialize(self):
        self.assert_paths()
        self.verify_runtime()
        if self.marker.exists():
            self.state()
            self.settings()
            return
        if self.directory.exists() and any(self.directory.iterdir()):
            raise RuntimeError("Unmarked private directory is nonempty; refusing to overwrite or reinitialize it.")
        port = choose_port()
        self.directory.mkdir(parents=True, exist_ok=True)
        restrict_acl(self.directory, directory=True)
        settings = {"admin_user": ADMIN_USER, "app_user": APP_USER,
                    "admin_password": secrets.token_urlsafe(36),
                    "app_password": secrets.token_urlsafe(36)}
        with self.credentials.open("x", encoding="utf-8") as stream:
            json.dump(settings, stream, indent=2)
        password_file = self.directory / "initdb-password.txt"
        with password_file.open("x", encoding="utf-8") as stream:
            stream.write(settings["admin_password"] + "\n")
        try:
            run([str(BIN / "initdb.exe"), "-D", str(self.data), "--username", ADMIN_USER,
                 "--pwfile", str(password_file), "--auth=scram-sha-256", "--encoding=UTF8",
                 "--locale=C", "--data-checksums"])
        finally:
            password_file.unlink(missing_ok=True)
        with (self.data / "postgresql.conf").open("a", encoding="utf-8") as stream:
            stream.write(
                "\n# Independent private test cluster\n"
                f"listen_addresses = '{HOST}'\nport = {port}\n"
                "password_encryption = 'scram-sha-256'\n"
                "max_connections = 20\nshared_buffers = '64MB'\n"
                "log_statement = 'none'\nlog_min_error_statement = 'panic'\n"
            )
        (self.data / "pg_hba.conf").write_text(
            "# Private IPv4 loopback only\nhost all all 127.0.0.1/32 scram-sha-256\n", encoding="utf-8")
        with self.marker.open("x", encoding="utf-8") as stream:
            json.dump({"kind": MARKER_KIND, "workspace": str(self.workspace),
                       "data_directory": str(self.data), "host": HOST, "port": port,
                       "database": DATABASE, "created_at": datetime.now(timezone.utc).isoformat()},
                      stream, indent=2)

    def running(self) -> bool:
        self.state()
        result = run([str(BIN / "pg_ctl.exe"), "status", "-D", str(self.data)], check=False)
        if result.returncode not in {0, 3}:
            raise RuntimeError("Private pg_ctl status failed; no process was started or stopped.")
        return result.returncode == 0

    def verify_process(self) -> int:
        state = self.state()
        try:
            lines = (self.data / "postmaster.pid").read_text(encoding="utf-8").splitlines()
            pid = int(lines[0])
            if pid <= 0 or Path(lines[1]).resolve() != self.data or int(lines[3]) != state["port"]:
                raise ValueError
        except (IndexError, ValueError, OSError):
            raise RuntimeError("Private pidfile identity is invalid; no process was stopped.") from None
        details = process_info(pid)
        if Path(details.get("ExecutablePath") or "").resolve() != (BIN / "postgres.exe").resolve():
            raise RuntimeError("Private pidfile identifies an unexpected executable; no process was stopped.")
        args = windows_argv(details.get("CommandLine") or "")
        data_args = []
        for index, arg in enumerate(args):
            if arg in {"-D", "--pgdata"} and index + 1 < len(args):
                data_args.append(args[index + 1])
            elif arg.startswith("--pgdata="):
                data_args.append(arg.split("=", 1)[1])
        if len(data_args) != 1 or Path(data_args[0]).resolve() != self.data:
            raise RuntimeError("Recorded process does not own this private data directory; no process was stopped.")
        return pid

    def verify_server(self) -> int:
        state = self.state()
        pid = self.verify_process()
        with self.connect("postgres", admin=True) as conn:
            row = conn.execute("SELECT current_setting('data_directory'), current_setting('listen_addresses'), current_setting('port'), current_user").fetchone()
        if (Path(row[0]).resolve() != self.data or row[1] != HOST
                or int(row[2]) != state["port"] or row[3] != ADMIN_USER):
            raise RuntimeError("Connected server differs from the owned private cluster; no process was stopped.")
        return pid

    def provision(self):
        self.verify_server()
        settings = self.settings()
        with self.connect("postgres", admin=True) as conn:
            role = conn.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname=%s", (APP_USER,)).fetchone()
            if role is None:
                conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}").format(sql.Identifier(APP_USER), sql.Literal(settings["app_password"])))
            elif any(role):
                raise RuntimeError("Existing private application role has elevated privileges; it was not modified.")
            owner = conn.execute("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s", (DATABASE,)).fetchone()
            if owner is None:
                conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(DATABASE), sql.Identifier(APP_USER)))
            elif owner[0] != APP_USER:
                raise RuntimeError("Existing private obs_test database has a different owner; it was not modified.")
        with self.connect(admin=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        with self.connect() as conn:
            row = conn.execute("SELECT current_database(), current_user, rolsuper,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname=current_user").fetchone()
            if row[:2] != (DATABASE, APP_USER) or any(row[2:]):
                raise RuntimeError("Private application database/role check failed.")

    def write_env(self):
        state, settings = self.state(), self.settings()
        url = (f"postgresql://{quote(APP_USER, safe='')}:{quote(settings['app_password'], safe='')}"
               f"@{HOST}:{state['port']}/{DATABASE}")
        staging = self.directory / "test.env.pending"
        if staging.exists():
            raise RuntimeError("A previous private connection-file write is pending; inspect it before continuing.")
        with staging.open("x", encoding="utf-8") as stream:
            stream.write(f"OBS_TEST_DATABASE_URL={url}\n")
        os.replace(staging, self.env_file)
        restrict_acl(self.env_file)

    def receipt(self, action: str, *, is_running: bool, **extra) -> dict:
        state = self.state()
        result = {"at_utc": datetime.now(timezone.utc).isoformat(), "action": action,
                  "kind": MARKER_KIND, "cluster": self.name, "host": HOST,
                  "port": state["port"], "database": DATABASE,
                  "data_directory": str(self.data), "running": is_running,
                  "connection_file": str(self.env_file), "project_env_written": False,
                  "main_cluster_operated": False, **extra}
        receipt_dir = self.directory / "receipts"
        receipt_dir.mkdir(exist_ok=True)
        path = receipt_dir / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}_{action}_{secrets.token_hex(3)}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["receipt"] = str(path)
        return result

    def setup(self) -> dict:
        self.initialize()
        if not self.running():
            state = self.state()
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if probe.connect_ex((HOST, state["port"])) == 0:
                    raise RuntimeError("Private test port is occupied; no other process was stopped. Use another cluster name or inspect the collision.")
            run([str(BIN / "pg_ctl.exe"), "start", "-D", str(self.data),
                 "-l", str(self.log), "-w", "-t", "30"], background=True)
        self.provision()
        self.write_env()
        return self.receipt("setup", is_running=True, pid=self.verify_server())

    def stop(self) -> dict:
        if not self.running():
            return self.receipt("stop", is_running=False, already_stopped=True)
        pid = self.verify_server()
        # Recheck the filesystem/process immediately before handing pg_ctl a pidfile.
        if self.verify_process() != pid:
            raise RuntimeError("Private server identity changed; no process was stopped.")
        run([str(BIN / "pg_ctl.exe"), "stop", "-D", str(self.data), "-m", "fast", "-w", "-t", "30"])
        if self.running():
            raise RuntimeError("Owned private test cluster is still running; inspect its private log.")
        return self.receipt("stop", is_running=False, pid=pid, data_retained=True)

    def status(self) -> dict:
        if not self.marker.exists():
            self.assert_paths()
            return {"cluster": self.name, "status": "not_initialized", "main_cluster_operated": False}
        running = self.running()
        pid = self.verify_server() if running else None
        return self.receipt("status", is_running=running, pid=pid)

    def health(self) -> dict:
        pid = self.verify_server()
        with self.connect() as conn:
            version = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()
            distance = conn.execute("SELECT '[1,2,3]'::vector <-> '[1,2,3]'::vector").fetchone()[0]
            # Fresh source-independent data; temporary table disappears on close.
            conn.execute("CREATE TEMP TABLE private_synthetic_probe (label text, amount integer)")
            conn.execute("INSERT INTO private_synthetic_probe VALUES ('paper kite', 7), ('wooden boat', 11)")
            total = conn.execute("SELECT sum(amount) FROM private_synthetic_probe").fetchone()[0]
        if not version or distance != 0 or total != 18:
            raise RuntimeError("Private vector/synthetic SQL smoke check failed.")
        return self.receipt("health", is_running=True, pid=pid, pgvector=version[0],
                            vector_distance=distance, synthetic_total=total)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "start", "stop", "status", "health"))
    parser.add_argument("--cluster", default="default")
    args = parser.parse_args(argv)
    try:
        cluster = PrivateTestCluster(args.cluster)
        operation = cluster.setup if args.action in {"setup", "start"} else getattr(cluster, args.action)
        print(json.dumps(operation(), ensure_ascii=False, indent=2))
    except psycopg.Error as exc:
        print(f"Private PostgreSQL operation failed ({type(exc).__name__}, SQLSTATE {exc.sqlstate or 'unavailable'}). Inspect its private log; credentials and SQL were withheld.", file=sys.stderr)
        return 1
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
