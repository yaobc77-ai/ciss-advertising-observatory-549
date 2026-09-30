import argparse
import json
from pathlib import Path

from .config import Settings
from .db import Database
from .models import Filters


def emit(value, out=None):
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def main():
    parser = argparse.ArgumentParser(
        description="CISS Observatory local administration"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sub.add_parser("migrate", help="Apply ordered database migrations and initialize retrieval state")
    sub.add_parser("migration-status", help="Inspect applied and pending migrations without changing data")
    sub.add_parser("health")
    sub.add_parser("serve")
    sub.add_parser("budget")
    imp = sub.add_parser("import-native")
    imp.add_argument("--root", type=Path, default=Path.cwd())
    imp.add_argument("--out", default="outputs/native_import.json")
    imp.add_argument("--mode", choices=["upsert", "snapshot"], default="upsert",
                     help="Upsert preserves other records; snapshot retires missing native records")
    social = sub.add_parser("import-social")
    social.add_argument("path", type=Path)
    social.add_argument("--mapping", type=Path, required=True)
    social.add_argument("--out", default="outputs/social_import.json")
    social.add_argument("--mode", choices=["upsert", "snapshot"], default="upsert")
    records = sub.add_parser("import-records", help="Import validated canonical RecordInput JSONL")
    records.add_argument("path", type=Path)
    records.add_argument("--dataset", choices=["native", "social"],
                         help="Validate the dataset; required for snapshot mode")
    records.add_argument("--mode", choices=["upsert", "snapshot"], default="upsert")
    records.add_argument("--dry-run", action="store_true", help="Validate every row without opening the database")
    records.add_argument("--out", default="outputs/records_import.json")
    sub.add_parser("index")
    for command in ("index-prepare", "index-status", "index-activate"):
        p = sub.add_parser(command)
        p.add_argument("profile", nargs="?" if command == "index-status" else None)
        p.add_argument("--out")
        if command == "index-activate":
            p.add_argument("--expected-source-version", required=True)
    for command in ["search", "answer"]:
        p = sub.add_parser(command)
        p.add_argument("question")
        p.add_argument(
            "--dataset", choices=["native", "social", "all"], default="native"
        )
        p.add_argument("--out")
    args = parser.parse_args()
    settings = Settings.from_env()
    db = Database(settings.database_url)
    if args.command in ("init-db", "migrate"):
        db.initialize()
        from .migrations import migration_status

        with db.connect() as conn:
            emit({"initialized": True, **migration_status(conn)})
    elif args.command == "migration-status":
        from .migrations import migration_status

        with db.connect() as conn:
            emit(migration_status(conn))
    elif args.command == "health":
        emit(db.health())
    elif args.command == "budget":
        from .budget import Budget

        emit(Budget(db, settings).summary())
    elif args.command == "import-native":
        from .ingest import load_native

        emit(
            db.import_batch(
                load_native(
                    args.root, require_admissions=True, require_body_reviews=True,
                    require_body_recoveries=True,
                ),
                snapshot_dataset="native" if args.mode == "snapshot" else None,
            ),
            args.out,
        )
    elif args.command == "import-social":
        from .ingest import load_social

        mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
        batch = load_social(args.path, mapping)
        emit(db.import_batch(
            batch, snapshot_dataset="social" if args.mode == "snapshot" else None,
        ), args.out)
    elif args.command == "import-records":
        from .import_records import load_records

        if args.mode == "snapshot" and args.dataset is None:
            parser.error("import-records --mode snapshot requires --dataset native or social")
        batch = load_records(args.path, dataset=args.dataset)
        if args.dry_run:
            emit({
                "validated": True, "dry_run": True,
                "input_records": len(batch.records), "mode": args.mode,
                "datasets": sorted({record.dataset for record in batch.records}),
                "source_hashes": batch.source_hashes,
            }, args.out)
        else:
            emit(db.import_batch(
                batch, snapshot_dataset=args.dataset if args.mode == "snapshot" else None,
            ), args.out)
    elif args.command == "index":
        from .rag import Rag

        emit(Rag(db, settings).index())
    elif args.command in ("index-prepare", "index-status", "index-activate"):
        from .indexing import IndexManager

        manager = IndexManager(db)
        if args.command == "index-prepare":
            result = manager.prepare(args.profile)
        elif args.command == "index-activate":
            result = manager.activate(
                args.profile, expected_source_data_version=args.expected_source_version,
            )
        else:
            result = manager.status(args.profile)
        emit(result, args.out)
    elif args.command in ("search", "answer"):
        from .service import Service

        service = Service(settings)
        filters = Filters(dataset=args.dataset)
        result = (
            [e.model_dump() for e in service.search(args.question, filters)]
            if args.command == "search"
            else service.answer(args.question, filters, "local-maintainer").model_dump()
        )
        emit(result, args.out)
    elif args.command == "serve":
        from waitress import serve

        from .app import create_app
        from .service import Service

        if settings.secure_cookies and not settings.cookie_secret:
            raise SystemExit("OBS_COOKIE_SECRET is required when OBS_SECURE_COOKIES=true")
        app = create_app(Service(settings), settings)
        proxy = {}
        if settings.trusted_proxy:
            # Honour X-Forwarded-Proto/For only from the hosting platform's proxy.
            proxy = {
                "trusted_proxy": settings.trusted_proxy,
                "trusted_proxy_headers": {"x-forwarded-for", "x-forwarded-proto"},
                "clear_untrusted_proxy_headers": True,
            }
        serve(
            app.server, host=settings.host, port=settings.port, threads=8, **proxy
        )


if __name__ == "__main__":
    main()
