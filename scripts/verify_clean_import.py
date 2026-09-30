"""Reproduce the 0.2.3 source snapshot and legacy600 index in disposable obs_test.

Requires an installed observatory package, an explicit unpacked --root with private
inputs, and OBS_TEST_DATABASE_URL supplied by the caller. Never reads .env or the
application URL. This command clears obs_test business tables; run it exclusively.
Source and wheel installations must be checked in separate environments. Historical
answer runs/versions are not reconstructed by importing this current snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from importlib.metadata import version
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

import observatory
from observatory.chunking import retrieval_spans
from observatory.db import Database, digest
from observatory.evaluate import load_cases, load_snapshot, validate_gold
from observatory.indexing import LEGACY_PROFILE
from observatory.ingest import load_native
from observatory.models import Filters

RELEASE = "0.2.3"
EXPECTED_VERSION = "5114ebc1cf9afe59cdaa715e3ea45166"
EXPECTED_COUNTS = {"stored": 275, "countable": 263, "retrievable": 226, "chunks": 558}
REFERENCE = "outputs/pdf265_publication_validation_20260916.json"
GOLD = "eval/development.jsonl"
MANIFEST_HASHES = {
    "config/native_admissions.json": "b487482be0d0f418c52bf4c0902272811b71acf0391d979ea04feda06eff81c9",
    "config/native_body_ranges.json": "9d05bd39c2d29515daf39b809d97dcd3e3d08c119b2905613e6c0248058f188e",
    "config/native_body_recoveries.json": "0b3d218df3ad62917eaea65ea4eb27ba8272783b17e747f86433448a420f7b85",
}
TABLES = (
    "generation_outputs,answer_runs,usage_ledger,embeddings,imports,"
    "annotations,chunks,record_versions,records,chunk_profile_membership,"
    "retrieval_preparations,retrieval_publications,retrieval_state,retrieval_profiles"
)


class VerificationError(RuntimeError):
    """A fixed, credential-free failure code suitable for the output report."""


def require(condition, code):
    if not condition:
        raise VerificationError(code)


def project_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    require(not path.is_absolute() and ".." not in path.parts, "unsafe_input_path")
    path = (root / path).resolve()
    require(path.is_relative_to(root) and path.is_file(), "missing_or_escaped_input")
    return path


def verify_inputs(root: Path):
    """Check the release contract and all source bytes before any DB connection."""
    root = root.resolve(strict=True)
    reference_path = project_file(root, REFERENCE)
    reference = json.loads(reference_path.read_bytes())
    # This pinned historical report predates index profiles: its data_version was
    # the source-only fingerprint. Never compare that old field to a modern
    # combined source/index data_version.
    require(reference["after"]["data_version"] == EXPECTED_VERSION, "wrong_release_reference")
    require(reference["after"]["record_counts"] == {"native": 275}, "wrong_release_reference")
    require(reference["after"]["chunks"] == 558, "wrong_release_reference")
    require(reference["counts"] == {"countable": 263, "retrievable": 226}, "wrong_release_reference")
    hashes = reference["source_hashes"]
    require(isinstance(hashes, dict) and len(hashes) == 10, "wrong_input_inventory")
    for name, expected in MANIFEST_HASHES.items():
        require(hashes.get(name) == expected, "unexpected_manifest_hash")
    for name, expected in hashes.items():
        actual = hashlib.sha256(project_file(root, name).read_bytes()).hexdigest()
        require(actual == expected, "input_hash_mismatch")
    cases = load_cases(project_file(root, GOLD))
    require(all(case.suite == "development" for case in cases), "wrong_gold_suite")
    require(sum(len(case.support_quote) for case in cases if case.status == "ready") == 15,
            "wrong_gold_span_count")
    return root, hashes, cases


def test_database_url(environ=None):
    """Do not load dotenv, infer a connection, or fall back to OBS_DATABASE_URL."""
    environ = os.environ if environ is None else environ
    url = environ.get("OBS_TEST_DATABASE_URL", "")
    require(bool(url), "missing_test_database_url")
    try:
        name = conninfo_to_dict(url).get("dbname")
    except Exception:
        raise VerificationError("invalid_test_database_url") from None
    require(name == "obs_test", "non_test_database_refused")
    return url


class TestOnlyDatabase(Database):
    """Check the actual database on every connection, including initialization."""

    def __init__(self, url):
        super().__init__(test_database_url({"OBS_TEST_DATABASE_URL": url}))

    def connect(self, vector=False):
        conn = super().connect(vector=vector)
        try:
            require(conn.execute("SELECT current_database() AS name").fetchone()["name"] == "obs_test",
                    "connected_database_refused")
        except BaseException:
            conn.close()
            raise
        # The read-only identity guard starts a transaction. Finish it before
        # repository methods establish their own consistent snapshot.
        conn.commit()
        return conn


def verify_chunk_locators(db):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT c.*,v.body,v.payload FROM chunks c "
            "JOIN records r ON r.current_version=c.version_id AND r.record_id=c.record_id "
            "JOIN record_versions v ON v.version_id=c.version_id WHERE r.active"
        ).fetchall()
    require(len(rows) == EXPECTED_COUNTS["chunks"], "wrong_chunk_count")
    for row in rows:
        start, end = row["start_char"], row["end_char"]
        spans = retrieval_spans(
            row["body"], retrieval_end=row["payload"].get("retrieval_end"),
            retrieval_ranges=row["payload"].get("retrieval_ranges"),
        )
        require(0 <= start < end <= len(row["body"]), "invalid_chunk_offset")
        require(row["body"][start:end] == row["text"], "chunk_text_mismatch")
        require(any(lower <= start < end <= upper for lower, upper in spans), "chunk_crosses_excluded_gap")
        require(row["text_hash"] == digest(row["text"]), "chunk_text_hash_mismatch")
        require(row["chunk_id"] == digest(f"{row['version_id']}:{start}:{end}"), "chunk_identity_mismatch")
    return len(rows)


def verify_snapshot(db, cases):
    health = db.health()
    require(health["record_counts"] == {"native": 275}, "wrong_record_counts")
    require(health.get("source_data_version") == EXPECTED_VERSION, "wrong_source_data_version")
    require(health.get("active_profile") == LEGACY_PROFILE, "wrong_index_profile")
    public = db.public_rows(Filters(dataset="native"))
    counts = {"stored": health["record_counts"]["native"], "countable": len(public),
              "retrievable": sum(bool(row["retrievable"]) for row in public), "chunks": health["chunks"]}
    require(counts == EXPECTED_COUNTS, "wrong_snapshot_counts")
    valid_chunks = verify_chunk_locators(db)
    filtered = {case.id: db.public_rows(case.filters) for case in cases if case.status == "ready"}
    located = validate_gold(cases, load_snapshot(db), filtered)
    valid_gold = sum(len(spans) for spans in located.values())
    require(valid_gold == 15, "wrong_gold_span_count")
    with db.connect() as conn:
        activity = conn.execute(
            "SELECT (SELECT count(*) FROM usage_ledger) AS usage_entries,"
            "(SELECT count(*) FROM embeddings) AS embeddings,"
            "(SELECT count(*) FROM answer_runs) AS answers,"
            "(SELECT count(*) FROM generation_outputs) AS generations"
        ).fetchone()
    require(all(value == 0 for value in activity.values()), "unexpected_paid_or_answer_activity")
    return {"health": health, "counts": counts, "current_chunk_locators_valid": valid_chunks,
            "development_gold_spans_valid": valid_gold, "gold_spans": located,
            "paid_activity": activity}


def run(root: Path, report: dict):
    root, hashes, cases = verify_inputs(root)
    report.update(input_hashes=hashes, gold_sha256=hashlib.sha256(project_file(root, GOLD).read_bytes()).hexdigest())
    batch = load_native(root, require_admissions=True, require_body_reviews=True, require_body_recoveries=True)
    require(batch.source_hashes == hashes, "loaded_input_hashes_changed")
    require(not batch.rejected and len(batch.records) == 275, "invalid_import_batch")
    db = TestOnlyDatabase(test_database_url())
    db.initialize()
    with db.connect() as conn:
        conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    report["test_tables_cleared"] = True
    # Reset all previous candidate/publication state. A previous sentence-active
    # test database must not silently change this fixed historical reproduction.
    db.initialize()
    first = db.import_batch(batch, snapshot_dataset="native")
    report["first_import"] = first
    require(first["new_versions"] == 275 and first["unchanged"] == 0 and not first["deactivated"],
            "unexpected_first_import")
    report["first_validation"] = verify_snapshot(db, cases)
    # Reload the explicit root, rather than reusing the already constructed batch.
    repeat_batch = load_native(root, require_admissions=True, require_body_reviews=True, require_body_recoveries=True)
    require(repeat_batch.source_hashes == hashes and not repeat_batch.rejected, "repeat_inputs_changed")
    repeat = db.import_batch(repeat_batch, snapshot_dataset="native")
    report["repeat_import"] = repeat
    require(repeat["new_versions"] == 0 and repeat["unchanged"] == 275 and not repeat["deactivated"],
            "repeat_import_not_idempotent")
    report["repeat_validation"] = verify_snapshot(db, cases)
    report["status"] = "passed"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Unpacked release root containing the hash-pinned private inputs")
    parser.add_argument("--output", required=True, type=Path, help="New JSON report path; never overwrites an existing report")
    args = parser.parse_args(argv)
    # Reserve the output before clearing anything; an existing report fails safely.
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        stream = args.output.open("x", encoding="utf-8")
    except OSError:
        print("clean_import_output_unavailable", file=sys.stderr)
        return 1
    report = {"snapshot_release": RELEASE, "package_version": version("ciss-observatory"),
              "status": "failed", "expected_source_data_version": EXPECTED_VERSION,
              "expected_index_profile": LEGACY_PROFILE,
              "version_contract": "historical source hash plus explicit legacy600-v1 reproduction",
              "root": str(args.root.resolve()), "package_location": str(Path(observatory.__file__).resolve()),
              "package_module_sha256": {
                  p.name: digest(p.read_text(encoding="utf-8"))
                  for p in sorted(Path(observatory.__file__).parent.glob("*.py"))
              },
              "database": "obs_test", "test_tables_cleared": False,
              "historical_locator_reproduction": "not_applicable_current_snapshot_only",
              "human_semantic_acceptance": "not_evaluated"}
    try:
        run(args.root, report)
    except Exception as exc:
        # Driver/parser errors can include connection details. Never serialize them.
        report["error_type"] = type(exc).__name__
        report["failure_code"] = str(exc) if isinstance(exc, VerificationError) else "verification_failed"
    finally:
        with stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, default=str)
            stream.write("\n")
    print(json.dumps({"status": report["status"], "output": str(args.output.resolve())}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
