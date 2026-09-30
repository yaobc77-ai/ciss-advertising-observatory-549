"""Compare derived chunks with PostgreSQL lexical search; never write the database.

Run from the project root with the existing environment, for example::

    .venv/Scripts/python.exe scripts/compare_retrieval_chunks.py --output outputs/chunks.json

All database reads share one REPEATABLE READ, READ ONLY transaction. Candidate
chunks exist only in Python and a SELECT CTE. No embedding/generation is called;
cache coverage is inventory, not a hybrid retrieval experiment. Output contains
provided article excerpts and should be reviewed before public redistribution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import IsolationLevel
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from observatory import chunking, segmentation
from observatory.config import Settings
from observatory.db import Database, digest
from observatory.evaluate import (
    EvaluationInvalid,
    load_cases,
    load_snapshot,
    ratio,
    validate_gold,
)
from observatory.indexing import LEGACY_PROFILE
from observatory.models import Filters
from observatory.service import Service

ROOT = Path(__file__).resolve().parents[1]
RETRIEVAL_PROFILE = "pg-english-or-rrf60-lexical-only-v1"
STOP_TERMS = {
    "what", "which", "how", "does", "the", "a", "an", "is", "are", "of", "to",
    "in", "and", "for", "do", "about", "say", "these", "this", "with",
}


@dataclass(frozen=True)
class Variant:
    name: str
    strategy: str
    max_tokens: int
    overlap_tokens: int = 100


VARIANTS = (
    Variant("legacy300", "legacy", 300),
    Variant("legacy400", "legacy", 400),
    Variant("legacy600", "legacy", 600),
    Variant("sentence600", "sentence", 600),
)


class SnapshotDatabase(Database):
    """Reuse repository public-read methods without leaving the shared snapshot."""

    def __init__(self, connection):
        self.connection = connection

    def connect(self, vector=False):
        if vector:
            raise EvaluationInvalid("Vector retrieval is outside this lexical comparison")
        return nullcontext(self.connection)


@contextmanager
def readonly_repository(database_url):
    if not database_url:
        raise EvaluationInvalid("OBS_DATABASE_URL is not configured")
    with psycopg.connect(
        database_url, row_factory=dict_row, connect_timeout=5,
        options="-c default_transaction_read_only=on -c statement_timeout=60000",
    ) as connection:
        connection.isolation_level = IsolationLevel.REPEATABLE_READ
        connection.read_only = True
        readonly = connection.execute("SHOW transaction_read_only").fetchone()
        isolation = connection.execute("SHOW transaction_isolation").fetchone()
        if readonly["transaction_read_only"] != "on" or isolation["transaction_isolation"] != "repeatable read":
            raise EvaluationInvalid("Comparison requires a repeatable read, read-only transaction")
        yield SnapshotDatabase(connection)


def lexical_query(question):
    """Mirror Database.search's OR construction; covered by a parity regression."""
    terms = [
        token for token in re.findall(r"[\w-]+", question.lower())
        if len(token) > 1 and token not in STOP_TERMS
    ][:40]
    return " OR ".join('"' + token + '"' for token in terms)


def build_chunks(snapshot, selected_ids, variant):
    chunks = []
    for rid in sorted(selected_ids):
        record = snapshot[rid]
        payload = record["payload"]
        if not payload.get("retrievable"):
            continue
        ranges = chunking.retrieval_spans(
            record["body"], retrieval_end=payload.get("retrieval_end"),
            retrieval_ranges=payload.get("retrieval_ranges"),
        )
        for part in chunking.chunk_retrieval_body(
            record["body"], max_tokens=variant.max_tokens,
            overlap_tokens=variant.overlap_tokens, strategy=variant.strategy,
            retrieval_end=payload.get("retrieval_end"),
            retrieval_ranges=payload.get("retrieval_ranges"),
        ):
            retained_index = next(
                i for i, (start, end) in enumerate(ranges)
                if start <= part["start"] < part["end"] <= end
            )
            item = {
                **part, "record_id": rid, "version_id": record["version_id"],
                # Preserve production's tie-break for identical geometry. These
                # IDs are derived; no claim that the row exists in chunks.
                "chunk_id": digest(f"{record['version_id']}:{part['start']}:{part['end']}"),
                "text_hash": digest(part["text"]), "retained_range_index": retained_index,
            }
            if not valid_locator(item, snapshot, variant.max_tokens):
                raise EvaluationInvalid("A derived chunk failed original-source validation")
            chunks.append(item)
    if len({row["chunk_id"] for row in chunks}) != len(chunks):
        raise EvaluationInvalid("Duplicate derived chunk IDs")
    return chunks


def valid_locator(item, snapshot, max_tokens):
    record = snapshot.get(item["record_id"])
    if not record or record["version_id"] != item["version_id"]:
        return False
    start, end = item["start"], item["end"]
    body = record["body"]
    if not (0 <= start < end <= len(body)) or body[start:end] != item["text"]:
        return False
    ranges = chunking.retrieval_spans(
        body, retrieval_end=record["payload"].get("retrieval_end"),
        retrieval_ranges=record["payload"].get("retrieval_ranges"),
    )
    index = item["retained_range_index"]
    return (
        0 <= index < len(ranges) and ranges[index][0] <= start < end <= ranges[index][1]
        and item["text_hash"] == digest(item["text"])
        and item["chunk_id"] == digest(f"{item['version_id']}:{start}:{end}")
        and 0 < item["token_count"] == chunking._tokens(item["text"]) <= max_tokens
    )


def select_records(lexical_rows, *, limit=5, chunks_per_record=1):
    """Same single-channel RRF and distinct-record selection as Database.search."""
    if limit < 1 or chunks_per_record < 1:
        raise ValueError("Record and chunk limits must be positive")
    by_id = {row["chunk_id"]: row for row in lexical_rows}
    ranking = {row["chunk_id"]: 1 / (60 + rank) for rank, row in enumerate(lexical_rows, 1)}
    selected = {}
    for cid in sorted(ranking, key=lambda key: (-ranking[key], key)):
        row = by_id[cid]
        rid = row["record_id"]
        if rid not in selected and len(selected) >= limit:
            continue
        group = selected.setdefault(rid, [])
        if len(group) < chunks_per_record:
            group.append({**row, "score": ranking[cid]})
    return [row for group in selected.values() for row in sorted(group, key=lambda item: item["start"])]


def retrieve(connection, chunks, question, filters, *, chunks_per_record):
    query = lexical_query(question)
    if not query or not chunks:
        return [], 0
    where, params = Database.where(filters)
    # Metadata selection, stemming and ranking are PostgreSQL's existing rules.
    # The CTE has no INSERT/UPDATE/DDL, even for temporary tables.
    sql = f"""WITH derived AS (
      SELECT * FROM jsonb_to_recordset(%s::jsonb) AS d(
        chunk_id text, record_id text, version_id text, text text, text_hash text,
        "start" integer, "end" integer, paragraph_ids jsonb, token_count integer,
        retained_range_index integer)
    ), vectors AS (
      SELECT d.*,to_tsvector('english',d.text) AS search_vector FROM derived d
    )
    SELECT c.chunk_id,c.record_id,c.version_id,c.text,c.text_hash,c."start",c."end",
      c.paragraph_ids,c.token_count,c.retained_range_index,
      ts_rank_cd(c.search_vector,websearch_to_tsquery('english',%s)) AS lexical_score,
      count(*) OVER() AS matched_candidate_count
    FROM vectors c JOIN records r ON r.current_version=c.version_id AND r.record_id=c.record_id
      JOIN record_versions v ON v.version_id=c.version_id
    WHERE {where} AND c.search_vector @@ websearch_to_tsquery('english',%s)
    ORDER BY lexical_score DESC,c.chunk_id LIMIT 50"""
    rows = connection.execute(sql, [Jsonb(chunks), query, *params, query]).fetchall()
    matched = int(rows[0]["matched_candidate_count"]) if rows else 0
    return select_records(rows, chunks_per_record=chunks_per_record), matched


def interval_overlap(rows):
    """Redundant original characters / returned characters, never across sources."""
    total = sum(row["end"] - row["start"] for row in rows)
    intervals = {}
    for row in rows:
        key = (row["record_id"], row["version_id"], row["retained_range_index"])
        intervals.setdefault(key, []).append((row["start"], row["end"]))
    unique = 0
    for group in intervals.values():
        last_end = -1
        for start, end in sorted(group):
            unique += max(0, end - max(start, last_end))
            last_end = max(last_end, end)
    return {"returned_characters": total, "unique_source_characters": unique,
            "redundant_characters": total - unique,
            "ratio": (total - unique) / total if total else None}


def score_case(case, evidence, located_gold, snapshot, variant):
    valid = [valid_locator(row, snapshot, variant.max_tokens) for row in evidence]
    if not all(valid):
        raise EvaluationInvalid("Retrieved derived evidence failed original-source validation")
    record_ids = list(dict.fromkeys(row["record_id"] for row in evidence))[:5]
    support = [{**gold, "covered": any(
        row["record_id"] == gold["record_id"] and gold["quote"] in row["text"]
        for row in evidence
    )} for gold in located_gold]
    # A support quote must be complete in ONE returned chunk. Overlapping/split
    # chunks are not concatenated to improve this metric or bridge removed gaps.
    return {
        "record_ids_at_5": record_ids,
        "record_hit_at_5": set(case.required_record_ids).issubset(record_ids)
        if case.case_type == "retrieval" else None,
        "support_quotes": support,
        "complete_support_quote_coverage": ratio(sum(row["covered"] for row in support), len(support)),
        "locator_checks": ratio(sum(valid), len(valid)),
        "returned_chunks": len(evidence),
        "returned_token_count": sum(row["token_count"] for row in evidence),
        "overlap": interval_overlap(evidence),
        "evidence": evidence,
        "abstention_correct": None, "semantic_correct": None,
    }


def diagnostics(development):
    """Transparent, development-derived probes; never add to the original gold."""
    bases = {case.id: case for case in development}
    specifications = [
        ("dev-01", "zh-translation", "Total 和 GoodPlanet 的员工航空旅行碳抵消项目中，沼气池使用什么原料，项目位于哪里？"),
        ("dev-02", "zh-translation", "ExxonMobil 的 Baytown 氢能广告怎样把天然气与 CCS 联系起来？广告是否称这座新氢能设施已经建成？"),
        ("dev-03", "zh-translation", "Statoil 委托的 Forbes 技术文章承认了碳捕集部署方面的什么限制？"),
        ("dev-04", "zh-translation", "AFPM 在 Politico 发布的塑料广告为什么说机械回收存在局限？"),
        ("dev-05", "zh-translation", "Total 怎样把水处理中的细菌和微藻与 CO2 排放、沼气和生物燃料联系起来？"),
        ("dev-06", "zh-translation", "Total 的 2018 年碳强度广告中，OGCI 首席执行官会议宣布了什么甲烷目标？"),
        ("dev-07", "zh-translation", "ExxonMobil 的 Baytown 广告和 PETRONAS 的 Kasawari CCS 广告怎样表明产能属于未来计划而非已实现的结果？"),
        ("dev-08", "en-translation", "In Enbridge's 2022 Politico native advertisement, how is the low-carbon hydrogen and ammonia production and export hub connected to CCS? Which statements describe plans?"),
    ]
    cases, origins = [], {}
    for source_id, kind, question in specifications:
        if source_id not in bases:
            continue
        original = bases[source_id]
        cid = f"diagnostic-{source_id}-{kind}"
        case = original.model_copy(deep=True, update={
            "id": cid, "question": question,
            "selection_note": "A single manually authored translation of an existing development case; not independent or held out. Gold and filters are unchanged.",
        })
        cases.append(case)
        origins[cid] = {"source_case_id": source_id, "kind": kind, "independent_heldout": False,
                        "original_question": original.question}
    return cases, origins


def summarize(rows):
    ready = [row for row in rows if row["case_status"] == "ready"]
    retrieval = [row for row in ready if row["case_type"] == "retrieval"]
    counts = [row for row in ready if row["case_type"] == "count"]
    support = [q for row in retrieval for q in row["support_quotes"]]
    return {
        "case_status_counts": dict(Counter(row["case_status"] for row in rows)),
        "record_hit_at_5": ratio(sum(row["record_hit_at_5"] for row in retrieval), len(retrieval)),
        "complete_support_quote_coverage": ratio(sum(q["covered"] for q in support), len(support)),
        "count_exact": ratio(sum(row["count_exact"] for row in counts), len(counts)),
        "returned_tokens_retrieval_cases": sum(row["returned_token_count"] for row in retrieval),
        "overlap_retrieval_cases": {
            # Do not interpret the same passage returned for two different
            # questions as within-context duplication.
            "returned_characters": sum(row["overlap"]["returned_characters"] for row in retrieval),
            "unique_source_characters": sum(row["overlap"]["unique_source_characters"] for row in retrieval),
            "redundant_characters": sum(row["overlap"]["redundant_characters"] for row in retrieval),
            "ratio": ratio(sum(row["overlap"]["redundant_characters"] for row in retrieval),
                           sum(row["overlap"]["returned_characters"] for row in retrieval))["rate"],
        },
        "retrieval_ms_total": sum(row["retrieval_ms"] for row in retrieval),
        "abstention_correct": None, "human_semantic_acceptance": None, "overall_pass": None,
    }


def compare_legacy_baseline(derived, persisted, *, active_profile):
    def signature(row):
        return (row["chunk_id"], row["text_hash"], row["start"], row["end"], row["token_count"])

    expected = {signature(row) for row in derived}
    observed = {signature(row) for row in persisted}
    matches = expected == observed and len(persisted) == len(observed)
    is_current = active_profile == LEGACY_PROFILE
    return {
        "scope": "persisted_legacy_baseline", "profile_id": LEGACY_PROFILE,
        "baseline_matches_persisted_legacy_index": matches,
        "baseline_is_current_index": is_current,
        "baseline_matches_current_index": matches if is_current else None,
        "derived_chunks": len(derived), "persisted_legacy_chunks": len(persisted),
        "derived_missing_or_different_in_persisted_legacy": len(expected - observed),
        "persisted_legacy_missing_or_different_in_derived": len(observed - expected),
        "compared_fields": ["chunk_id", "text_hash", "start", "end", "token_count"],
    }


def compare(repository, groups, *, expected_data_version=None, chunks_per_record=1, embedding_model="text-embedding-3-small"):
    if chunks_per_record not in (1, 3):
        raise ValueError("Use a separately reported 1 or 3 chunks per record")
    health = repository.health()
    if expected_data_version and health["data_version"] != expected_data_version:
        raise EvaluationInvalid("Current data version does not match the requested snapshot")
    snapshot = load_snapshot(repository)
    corpus_rows = repository.public_rows(Filters(dataset="all"))
    selected_ids = {row["record_id"] for row in corpus_rows}
    legacy_chunks = repository.connection.execute(
        'SELECT c.chunk_id,c.text_hash,c.start_char AS "start",c.end_char AS "end",c.token_count '
        'FROM chunks c JOIN records r ON r.current_version=c.version_id AND r.record_id=c.record_id '
        'JOIN chunk_profile_membership m ON m.chunk_id=c.chunk_id '
        'WHERE r.active AND m.profile_id=%s', (LEGACY_PROFILE,),
    ).fetchall()
    prepared = {}
    for name, cases in groups.items():
        base_rows = {case.id: repository.public_rows(case.filters) for case in cases if case.status == "ready"}
        located = validate_gold(cases, snapshot, base_rows)
        prepared[name] = {}
        for case in cases:
            if case.status != "ready":
                continue
            scope = case.filters if case.case_type == "count" else Service._title_scope(
                SimpleNamespace(db=repository), case.question, case.filters,
            )
            prepared[name][case.id] = {
                "filters": scope, "rows": repository.public_rows(scope), "gold": located[case.id],
            }
    cache_hashes = {row["text_hash"] for row in repository.connection.execute(
        "SELECT text_hash FROM embeddings WHERE model=%s", (embedding_model,),
    ).fetchall()}
    query_hashes = {digest(case.question) for cases in groups.values() for case in cases
                    if case.status == "ready" and case.case_type != "count"}
    results = []
    baseline = None
    for variant in VARIANTS:
        started = time.perf_counter()
        chunks = build_chunks(snapshot, selected_ids, variant)
        if variant.name == "legacy600":
            baseline = compare_legacy_baseline(
                chunks, legacy_chunks, active_profile=health["active_profile"],
            )
        build_ms = (time.perf_counter() - started) * 1000
        hashes = {row["text_hash"] for row in chunks}
        result_groups = {}
        for name, cases in groups.items():
            rows = []
            for case in cases:
                result = {"case_id": case.id, "case_status": case.status, "case_type": case.case_type,
                          "question": case.question, "filters": case.filters.model_dump(mode="json")}
                if case.status != "ready":
                    rows.append(result)
                    continue
                context = prepared[name][case.id]
                filters = context["filters"]
                scope_ids = {row["record_id"] for row in context["rows"]}
                candidates = [row for row in chunks if row["record_id"] in scope_ids]
                result.update({"effective_filters": filters.model_dump(mode="json"),
                               "title_scope_changed": filters != case.filters,
                               "scoped_countable_records": len(scope_ids),
                               "candidate_chunks": len(candidates), "gold_locators": context["gold"]})
                if case.case_type == "count":
                    result.update({"count": len(scope_ids), "count_exact": len(scope_ids) == case.expected_count,
                                   "retrieval": "not_applicable_sql_count"})
                else:
                    started = time.perf_counter()
                    evidence, matched = retrieve(repository.connection, candidates, case.question, filters,
                                                 chunks_per_record=chunks_per_record)
                    result["retrieval_ms"] = (time.perf_counter() - started) * 1000
                    result["matched_chunks_before_top50"] = matched
                    result.update(score_case(case, evidence, context["gold"], snapshot, variant))
                rows.append(result)
            result_groups[name] = {"summary": summarize(rows), "cases": rows}
        results.append({
            "variant": asdict(variant), "build_ms": build_ms,
            "candidate_chunks": len(chunks), "unique_chunk_texts": len(hashes),
            "derived_chunk_fingerprint_sha256": digest(json.dumps(
                [(row["chunk_id"], row["text_hash"]) for row in chunks], separators=(",", ":"))),
            "existing_embedding_cache": {
                "model": embedding_model, "unique_text_coverage": ratio(len(hashes & cache_hashes), len(hashes)),
                "chunk_coverage": ratio(sum(row["text_hash"] in cache_hashes for row in chunks), len(chunks)),
                "query_coverage": ratio(len(query_hashes & cache_hashes), len(query_hashes)),
                "vectors_used": False, "hybrid_status": "not_run",
            },
            "groups": result_groups,
        })
    return {
        "schema_version": 1, "run_id": uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "lexical_only", "database_transaction": "repeatable read, read only",
        "data_version": health["data_version"], "source_snapshot": health,
        "source_data_version": health["source_data_version"],
        "current_active_profile": health["active_profile"],
        "legacy600_baseline": baseline,
        "countable_records": len(selected_ids),
        "retrievable_countable_records": sum(snapshot[rid]["payload"].get("retrievable", False) for rid in selected_ids),
        "retrieval_profile": {"name": RETRIEVAL_PROFILE, "language": "english", "lexical_limit": 50,
                              "distinct_record_limit": 5, "chunks_per_record": chunks_per_record,
                              "fusion": "RRF k=60 with lexical channel only", "title_scope": "Service._title_scope",
                              "metadata_scope": "Database.where", "index_writes": False},
        "metrics_notes": [
            "Record Hit@5 requires every gold record within the five distinct records.",
            "Complete support coverage requires the exact quote inside one returned original-source chunk.",
            "Returned tokens use cl100k_base; duplication is redundant original characters per question.",
            "Chunk construction and SELECT latency are recorded separately; CTE timings are not indexed-service benchmarks.",
            "Development questions were already used for tuning. PDF smoke is separately scoped. Diagnostics are derived, not held out.",
            "No generation, embedding, reranker, hybrid recall, answer correctness or human acceptance is evaluated.",
            "Derived chunk IDs preserve production geometry tie-breaks but are not asserted to exist in the database.",
            "The persisted legacy baseline includes only legacy600-v1 members of current source versions; it is the current index only when that profile is active.",
        ],
        "variants": results,
    }


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def implementation_manifest():
    return {
        "package_version": version("ciss-observatory"), "hash_method": "sha256 raw file bytes",
        "chunking_profile": chunking.CHUNKING_PROFILE_VERSION,
        "segmentation_profile": segmentation.SEGMENTATION_PROFILE,
        "files": {name: file_hash(ROOT / name) for name in (
            "scripts/compare_retrieval_chunks.py", "src/observatory/chunking.py", "src/observatory/segmentation.py",
            "src/observatory/db.py", "src/observatory/schema.sql", "src/observatory/service.py", "src/observatory/evaluate.py",
        )},
    }


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development", type=Path, default=ROOT / "eval/development.jsonl")
    parser.add_argument("--pdf-smoke", type=Path, default=ROOT / "eval/pdf265_recovery_smoke.jsonl")
    parser.add_argument("--output", type=Path, required=True, help="New JSON file; existing output is never overwritten")
    parser.add_argument("--expected-data-version")
    parser.add_argument("--chunks-per-record", type=int, choices=(1, 3), default=1)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Output already exists; select a new path")
    try:
        implementation = implementation_manifest()
        gold_sources = {
            name: {"filename": path.name, "sha256": file_hash(path), "status": "development_draft_not_heldout"}
            for name, path in (("development", args.development), ("pdf265_smoke", args.pdf_smoke))
        }
        development = load_cases(args.development)
        smoke = load_cases(args.pdf_smoke)
        if any(case.suite != "development" for case in development + smoke):
            raise EvaluationInvalid("This runner accepts development diagnostics, not acceptance drafts")
        probes, origins = diagnostics(development)
        groups = {"development": development, "pdf265_smoke": smoke, "derived_diagnostics_not_heldout": probes}
        settings = Settings.from_env()
        with readonly_repository(settings.database_url) as repository:
            report = compare(repository, groups, expected_data_version=args.expected_data_version,
                             chunks_per_record=args.chunks_per_record, embedding_model=settings.embedding_model)
        if implementation != implementation_manifest() or any(
            gold_sources[name]["sha256"] != file_hash(path)
            for name, path in (("development", args.development), ("pdf265_smoke", args.pdf_smoke))
        ):
            raise EvaluationInvalid("Source or gold changed during comparison; discard this run")
        report["diagnostic_origins"] = origins
        report["implementation"] = implementation
        report["gold_sources"] = gold_sources
        write_report(args.output, report)
    except (EvaluationInvalid, psycopg.Error, OSError, ValueError) as exc:
        # Connection errors can contain a DSN; show no raw exception text.
        parser.exit(1, f"Comparison failed ({type(exc).__name__}); no database writes or paid calls were attempted.\n")
    print(json.dumps({"output": str(args.output), "data_version": report["data_version"],
                      "mode": report["mode"], "variants": len(report["variants"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
