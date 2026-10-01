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
    claims = sub.add_parser("claims-audit", help="Read saved CLAIMS outputs and original versions without publishing labels")
    claims.add_argument("--bundle", type=Path, required=True, help="Upstream paragraph src/data directory")
    claims.add_argument("--out", type=Path, required=True, help="New private output directory")
    claims.add_argument("--dataset", choices=["native", "social"], default="native")
    claims.add_argument("--projection", choices=["strict", "upstream-ascii-v1"], default="strict",
                        help="Explicitly enable the versioned lossy upstream character projection")
    review = sub.add_parser("claims-review-template", help="Create a pending review file from a completed private audit")
    review.add_argument("--audit", type=Path, required=True)
    review.add_argument("--out", type=Path, required=True, help="New private JSON file; never overwritten")
    source_review = sub.add_parser("claims-source-review-packet", help="Create a private offline review packet for candidate article URLs")
    source_review.add_argument("--input-csv", type=Path, required=True, help="Original upstream id/article_id/text CSV")
    source_review.add_argument("--input-id", required=True, help="Paragraph ID within that CSV, not an Observatory record ID")
    source_review.add_argument("--lookup", type=Path, required=True, help="Saved source discovery receipt JSON")
    source_review.add_argument("--captures", type=Path, help="Optional saved-text capture manifest; no pages are fetched")
    source_review.add_argument("--excerpt-start", type=int, help="Original input position, required when the searched excerpt repeats")
    source_review.add_argument("--out", type=Path, required=True, help="New private output directory")
    claim_import = sub.add_parser("claims-import", help="Validate reviewed CLAIMS results; writing requires --apply")
    claim_import.add_argument("--audit", type=Path, required=True)
    claim_import.add_argument("--bundle", type=Path, required=True)
    claim_import.add_argument("--review", type=Path, required=True)
    claim_import.add_argument("--apply", action="store_true", help="Commit source-checked published assignments")
    claim_import.add_argument("--out")
    claim_query = sub.add_parser("claims-matches", help="List published taxonomy assignments and their original quotes")
    claim_query.add_argument("--dataset", choices=["native", "social", "all"], default="native")
    claim_query.add_argument("--nc-id", action="append", default=[])
    claim_query.add_argument("--sc-id", action="append", default=[])
    claim_query.add_argument("--record-id", action="append", default=[])
    claim_query.add_argument("--publisher", action="append", default=[])
    claim_query.add_argument("--sponsor", action="append", default=[])
    claim_query.add_argument("--taxonomy")
    claim_query.add_argument("--review-state", choices=["automatic_unverified", "human_supported"])
    claim_query.add_argument("--offset", type=int, default=0)
    claim_query.add_argument("--limit", type=int, default=20)
    claim_query.add_argument("--out")
    retract = sub.add_parser("claims-retract", help="Withdraw a published assignment while retaining its audit history")
    retract.add_argument("candidate_key")
    retract.add_argument("--reviewer", required=True)
    retract.add_argument("--reason", required=True)
    retract.add_argument("--reviewed-at", required=True, help="ISO timestamp with timezone")
    retract.add_argument("--out")
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
    if args.command == "claims-source-review-packet":
        from .claims_source_review import write_source_review_packet

        emit(write_source_review_packet(args.input_csv, args.input_id, args.lookup, args.out,
                                        captures=args.captures, excerpt_start=args.excerpt_start))
        return
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
    elif args.command == "claims-audit":
        from .claims_linkage import run_linkage_audit

        emit(run_linkage_audit(db, args.bundle, args.out, dataset=args.dataset,
                              projection=args.projection))
    elif args.command == "claims-review-template":
        from .claims_publication import write_review_template

        emit(write_review_template(args.audit, args.out))
    elif args.command == "claims-import":
        from .claims_publication import prepare_claims_import
        from .claims_store import ClaimsStore

        prepared = prepare_claims_import(args.audit, args.bundle, args.review)
        emit(ClaimsStore(db).import_prepared(prepared, apply=args.apply), args.out)
    elif args.command == "claims-matches":
        from .claims_store import ClaimsStore

        filters = Filters(dataset=args.dataset, record_ids=args.record_id,
                          publishers=args.publisher, sponsors=args.sponsor)
        emit(ClaimsStore(db).matches(filters, nc_ids=args.nc_id, sc_ids=args.sc_id,
                                    taxonomy=args.taxonomy, review_state=args.review_state,
                                    offset=args.offset, limit=args.limit), args.out)
    elif args.command == "claims-retract":
        from .claims_store import ClaimsStore

        emit(ClaimsStore(db).retract(args.candidate_key, reviewer=args.reviewer,
                                   reason=args.reason, reviewed_at=args.reviewed_at), args.out)
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
