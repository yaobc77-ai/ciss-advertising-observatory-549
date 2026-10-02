"""Dump the whole local database and restore it into an empty hosted PostgreSQL.

`dump` writes a full custom-format archive plus its SHA-256 and the table row
counts taken right after the dump. `restore` loads it into an empty target and
compares every table with those counts; `verify` repeats only the comparison.
The target connection string is read only from REMOTE_DATABASE_URL.
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_postgres as lp  # noqa: E402


def table_counts(conn):
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1"
        ).fetchall()
    ]
    return {t: conn.execute(f'SELECT count(*) FROM public."{t}"').fetchone()[0] for t in tables}


def local_connection():
    env = lp.pg_environment()
    return psycopg.connect(
        dbname=lp.DATABASE, host=env["PGHOST"], port=env["PGPORT"],
        user=env["PGUSER"], password=env["PGPASSWORD"],
    )


def dump():
    lp.verify_server()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = lp.RUNTIME / "backups" / f"observatory-full-{stamp}.dump"
    with target.open("xb") as stream:
        lp.run(
            [str(lp.BIN / "pg_dump.exe"), "--format=custom", "--no-owner",
             "--no-privileges", "--dbname", lp.DATABASE],
            env=lp.pg_environment(), stdout=stream,
        )
    with local_connection() as local:
        counts = table_counts(local)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(".dump.sha256").write_text(digest + "\n", encoding="ascii")
    target.with_suffix(".counts.json").write_text(json.dumps(counts, indent=2), encoding="utf-8")
    print(json.dumps({"dump": str(target), "sha256": digest, "counts": counts}, indent=2))


def verify(url, archive):
    expected = json.loads(archive.with_suffix(".counts.json").read_text(encoding="utf-8"))
    with psycopg.connect(url, connect_timeout=10) as remote:
        remote_counts = table_counts(remote)
        vector = remote.execute(
            "SELECT extversion FROM pg_extension WHERE extname='vector'"
        ).fetchone()
        server = remote.execute("SHOW server_version").fetchone()[0]
    mismatches = {
        t: {"expected": n, "remote": remote_counts.get(t)}
        for t, n in expected.items()
        if remote_counts.get(t) != n
    }
    print(json.dumps({
        "remote_server_version": server,
        "remote_pgvector": vector[0] if vector else None,
        "remote_counts": remote_counts,
        "mismatches": mismatches,
        "status": "ok" if not mismatches else "count_mismatch",
    }, indent=2))
    return not mismatches


def restore(url, archive):
    expected = archive.with_suffix(".dump.sha256").read_text(encoding="ascii").strip()
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise SystemExit("Dump checksum mismatch; refusing to restore.")
    with psycopg.connect(url, connect_timeout=10) as remote:
        existing = remote.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
        ).fetchone()[0]
        if existing:
            raise SystemExit(
                f"Target already has {existing} public tables; refusing to overwrite. "
                "Use `verify` to re-check a finished restore."
            )
        remote.execute("CREATE EXTENSION IF NOT EXISTS vector")
        remote.commit()
    # One transaction: a failed restore leaves the target without restored tables.
    lp.run(
        [str(lp.BIN / "pg_restore.exe"), "--no-owner", "--no-privileges",
         "--exit-on-error", "--single-transaction", "--dbname", url, str(archive)]
    )
    return verify(url, archive)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("dump")
    for name in ("restore", "verify"):
        sub.add_parser(name).add_argument("archive", type=Path)
    args = parser.parse_args()
    if args.command == "dump":
        return dump()
    url = os.environ.get("REMOTE_DATABASE_URL", "")
    if not url:
        raise SystemExit("Set REMOTE_DATABASE_URL to the hosted database's public connection URL.")
    action = restore if args.command == "restore" else verify
    if not action(url, args.archive.resolve()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
