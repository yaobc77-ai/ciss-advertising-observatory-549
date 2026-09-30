"""Draft evidence evaluation. Free lexical retrieval is the default; --paid is explicit."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .chunking import retrieval_spans
from .config import Settings
from .db import digest
from .models import Filters
from .rag import MAX_QUOTE_WORDS, SYSTEM
from .service import Service


class GoldQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: str
    quote: str = Field(min_length=1)

    @model_validator(mode="after")
    def reject_blank_quote(self):
        if not self.quote.strip():
            raise ValueError("A support quote cannot be whitespace only")
        return self


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    suite: Literal["development", "acceptance_draft"]
    dataset: Literal["native", "social", "cross"]
    status: Literal["ready", "pending_social"]
    case_type: Literal["retrieval", "count", "no_evidence"]
    question: str = Field(min_length=1, max_length=2000)
    filters: Filters
    required_record_ids: list[str] = Field(default_factory=list)
    support_quote: list[GoldQuote] = Field(default_factory=list)
    expected_count: int | None = Field(default=None, ge=0)
    rubric: list[str] = Field(min_length=1)
    selection_note: str

    @model_validator(mode="after")
    def check_case(self):
        if self.filters.dataset != {"native": "native", "social": "social", "cross": "all"}[self.dataset]:
            raise ValueError("Dataset scope and Filters.dataset disagree")
        if len(self.required_record_ids) != len(set(self.required_record_ids)):
            raise ValueError("Duplicate required record IDs")
        if self.status == "pending_social":
            if self.dataset == "native" or self.required_record_ids or self.support_quote or self.expected_count is not None:
                raise ValueError("Pending social/cross cases must not contain invented gold")
        elif self.dataset != "native":
            raise ValueError("Social/cross readiness requires a separately reviewed dataset release")
        elif self.case_type == "retrieval":
            if not self.required_record_ids:
                raise ValueError("Ready retrieval cases require source records")
            if not set(self.required_record_ids).issubset({q.record_id for q in self.support_quote}):
                raise ValueError("Every required record needs an exact support quote")
        elif self.case_type == "count":
            if self.expected_count is None or self.expected_count != len(self.required_record_ids):
                raise ValueError("Count gold must enumerate its complete filtered record set")
        elif self.required_record_ids:
            raise ValueError("No-evidence cases cannot require a positive retrieval hit")
        return self


class EvaluationInvalid(RuntimeError):
    pass


def load_cases(path: Path) -> list[EvaluationCase]:
    cases = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                cases.append(EvaluationCase.model_validate_json(line))
            except ValueError as exc:
                raise EvaluationInvalid(f"Invalid evaluation case at line {number}: {exc}") from exc
    if not cases or len({case.id for case in cases}) != len(cases):
        raise EvaluationInvalid("Evaluation file is empty or has duplicate case IDs")
    if len({case.suite for case in cases}) != 1:
        raise EvaluationInvalid("Do not mix development and acceptance draft in one run")
    return cases


def load_snapshot(db):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT r.record_id,r.dataset,v.version_id,v.body,v.payload "
            "FROM records r JOIN record_versions v ON r.current_version=v.version_id"
        ).fetchall()
    return {row["record_id"]: row for row in rows}


def validate_gold(cases, snapshot, filtered_rows):
    """Validate source ownership before any retrieval or paid request is made."""
    located = {}
    for case in cases:
        if case.status != "ready":
            continue
        selected = {row["record_id"] for row in filtered_rows[case.id]}
        if not set(case.required_record_ids).issubset(selected):
            raise EvaluationInvalid(f"{case.id}: required records are missing or excluded by filters")
        if case.case_type == "count" and selected != set(case.required_record_ids):
            raise EvaluationInvalid(f"{case.id}: count gold is stale; re-enumerate the whole filtered corpus")
        spans = []
        for gold in case.support_quote:
            record = snapshot.get(gold.record_id)
            if not record or gold.record_id not in selected or record["dataset"] != "native":
                raise EvaluationInvalid(f"{case.id}: support quote belongs to a missing or out-of-scope record")
            if gold.quote not in record["body"]:
                raise EvaluationInvalid(f"{case.id}: exact support quote is absent from the current original body")
            try:
                ranges = retrieval_spans(
                    record["body"],
                    retrieval_end=record["payload"].get("retrieval_end"),
                    retrieval_ranges=record["payload"].get("retrieval_ranges"),
                )
            except (ValueError, TypeError) as exc:
                raise EvaluationInvalid(f"{case.id}: current source has invalid retrieval ranges") from exc
            start = next(
                (found for lower, upper in ranges
                 if (found := record["body"].find(gold.quote, lower, upper)) >= 0),
                -1,
            )
            if start < 0:
                raise EvaluationInvalid(f"{case.id}: support quote is outside the accepted retrieval boundary; it must fit within one retained interval")
            spans.append({"record_id": gold.record_id, "version_id": record["version_id"],
                          "start": start, "end": start + len(gold.quote), "quote": gold.quote})
        if case.case_type == "retrieval" and any(
            not snapshot[rid]["payload"].get("retrievable") for rid in case.required_record_ids
        ):
            raise EvaluationInvalid(f"{case.id}: a required record is currently marked non-retrievable")
        located[case.id] = spans
    return located


def ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None}


def ledger_usage(db, visitor):
    """Include query embedding charges, which Answer.cost_usd alone omits."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT state,actual_usd,reserved_usd FROM usage_ledger WHERE visitor=%s", (visitor,)
        ).fetchall()
    return {
        "status": "ledger_observed",
        "settled_usd": float(sum(row["actual_usd"] or 0 for row in rows if row["state"] == "settled")),
        "unresolved_reserved_usd": float(sum(row["reserved_usd"] for row in rows if row["state"] in {"pending", "uncertain"})),
        "ledger_entries": len(rows),
    }


def check_version(service, expected):
    current = service.db.health().get("data_version")
    if not current or current == "unavailable" or current != expected:
        raise EvaluationInvalid("Data changed during evaluation; this run is invalid and must not be scored")


def summarize_results(rows, paid):
    summaries = {}
    for dataset in ("native", "social", "cross"):
        group = [row for row in rows if row["dataset"] == dataset]
        ready = [row for row in group if row["case_status"] == "ready"]
        retrieval = [row for row in ready if row["case_type"] == "retrieval"]
        counts = [row for row in ready if row["case_type"] == "count"]
        abstention = [row for row in ready if row["case_type"] == "no_evidence"] if paid else []
        citations = [row["citation_locator_valid"] for row in ready]
        locators = [row["evidence_locator_valid"] for row in ready]
        passages = [row["support_passage_coverage"] for row in retrieval]
        language = Counter(
            row.get("language_check", {}).get("status", "not_checked")
            for row in retrieval
        ) if paid else {}
        summaries[dataset] = {
            "total_cases": len(group), "ready_cases": len(ready),
            "pending_cases": sum(row["case_status"] == "pending_social" for row in group),
            "hit_at_5": ratio(sum(row["hit_at_5"] is True for row in retrieval), len(retrieval)),
            "support_passage_coverage": ratio(sum(m["numerator"] for m in passages), sum(m["denominator"] for m in passages)),
            "count_exact": ratio(sum(row["count_exact"] is True for row in counts), len(counts)),
            "evidence_locator_valid": ratio(sum(m["numerator"] for m in locators), sum(m["denominator"] for m in locators)),
            "citation_locator_valid": ratio(sum(m["numerator"] for m in citations), sum(m["denominator"] for m in citations)),
            "abstention_on_no_evidence": ratio(sum(row["abstained"] is True for row in abstention), len(abstention)),
            "abstention_status": "measured" if paid else "not_run_lexical",
            "citation_status": "measured" if paid else "not_run_lexical",
            "semantic_support": {"status": "pending_human_review", "rate": None},
            "language_check_statuses": dict(language),
            "language_check_scope": "local heuristic on generated claims; not semantic acceptance",
            "latency_ms_total": round(sum(row["latency_ms"] for row in ready), 3),
            "latency_ms_mean": round(sum(row["latency_ms"] for row in ready) / len(ready), 3) if ready else None,
            "settled_cost_usd": sum(row["cost"]["settled_usd"] for row in ready),
            "unresolved_reserved_usd": sum(row["cost"]["unresolved_reserved_usd"] for row in ready),
            "cost_unknown_cases": sum(row["cost"]["status"] == "unavailable" for row in ready),
            "execution_statuses": dict(Counter(row["execution_status"] for row in group)),
            "failure_reasons": dict(Counter(row["failure_reason"] for row in ready if row.get("failure_reason"))),
            "overall_pass": None,
        }
    return summaries


def run_evaluation(cases, service, *, paid=False, expected_data_version=None):
    run_id = uuid4().hex
    # Capture the implementation before requests run; do not label a historical
    # result with whatever source happens to exist when the report is read later.
    provenance = {
        "base_prompt_sha256": digest(SYSTEM) if paid else None,
        "module_sha256": {
            path.name: digest(path.read_text(encoding="utf-8"))
            for path in sorted(Path(__file__).parent.glob("*.py"))
        },
    }
    version = service.db.health().get("data_version")
    if not version or version == "unavailable":
        raise EvaluationInvalid("A current database version is required")
    if expected_data_version and expected_data_version != version:
        raise EvaluationInvalid("Database version differs from --expected-data-version")
    snapshot = load_snapshot(service.db)
    filtered = {case.id: service.browse(case.filters) for case in cases if case.status == "ready"}
    located = validate_gold(cases, snapshot, filtered)
    check_version(service, version)
    rows = []
    for case in cases:
        row = {
            "id": case.id, "dataset": case.dataset, "case_type": case.case_type,
            "case_status": case.status, "execution_status": "pending_social",
            "question": case.question, "filters": case.filters.model_dump(mode="json"),
            "required_record_ids": case.required_record_ids, "gold_spans": located.get(case.id, []),
            "hit_at_5": False if case.status == "ready" and case.case_type == "retrieval" else None,
            "support_passage_coverage": ratio(0, len(case.support_quote) if case.status == "ready" and case.case_type == "retrieval" else 0),
            "count_exact": None, "abstained": None,
            "citation_locator_valid": ratio(0, 0), "evidence_locator_valid": ratio(0, 0),
            "semantic_support": "pending_human_review", "latency_ms": 0,
            "cost": {"status": "not_dispatched", "settled_usd": 0.0, "unresolved_reserved_usd": 0.0},
        }
        if case.status == "pending_social":
            rows.append(row)
            continue
        check_version(service, version)
        visitor = f"evaluation:{run_id}:{case.id}"
        start = time.perf_counter()
        result = None
        evidence = []
        try:
            if case.case_type == "count":
                actual = service.statistics(case.filters)["total"]
                row.update(actual_count=actual, expected_count=case.expected_count,
                           count_exact=actual == case.expected_count, execution_status="count_only")
            else:
                if paid:
                    result = service.answer(case.question, case.filters, visitor)
                    evidence = result.evidence
                    row.update(answer_status=result.status, answer=result.answer,
                               failure_reason=result.failure_reason,
                               language_check=result.language_check,
                               citations=[c.model_dump() for c in result.citations],
                               reported_answer_cost_usd=result.cost_usd,
                               abstained=result.status == "insufficient_evidence", execution_status=result.status)
                else:
                    evidence = service.search(case.question, case.filters, limit=5)
                    row["execution_status"] = "retrieval_only"
                top_five_record_ids = list(dict.fromkeys(e.record_id for e in evidence))[:5]
                row["top_five_distinct_record_ids"] = top_five_record_ids
                if case.case_type == "retrieval":
                    row["hit_at_5"] = set(case.required_record_ids).issubset(top_five_record_ids)
                valid = {e.evidence_id: service.db.validate_evidence(e) and
                         e.record_id in snapshot and snapshot[e.record_id]["version_id"] == e.version_id
                         for e in evidence}
                row["evidence_locator_valid"] = ratio(sum(valid.values()), len(evidence))
                row["retrieved_record_ids"] = [e.record_id for e in evidence]
                row["evidence"] = [e.model_dump() for e in evidence]
                if case.case_type == "retrieval":
                    matches = [
                        {"record_id": gold.record_id, "quote": gold.quote,
                         "evidence_ids": [e.evidence_id for e in evidence
                                          if e.record_id == gold.record_id and valid[e.evidence_id]
                                          and gold.quote in e.text]}
                        for gold in case.support_quote
                    ]
                    row["support_passage_matches"] = matches
                    row["support_passage_coverage"] = ratio(sum(bool(m["evidence_ids"]) for m in matches), len(matches))
                if result is not None:
                    by_id = {e.evidence_id: e for e in evidence}
                    passed = sum(bool(c.quote.strip()) and c.evidence_id in by_id and
                                 valid.get(c.evidence_id, False) and c.quote in by_id[c.evidence_id].text and
                                 len(c.quote.split()) <= MAX_QUOTE_WORDS for c in result.citations)
                    row["citation_locator_valid"] = ratio(passed, len(result.citations))
                    row["answered_without_citations"] = result.status == "answered" and not result.citations
        except Exception as exc:
            row.update(execution_status="error", error_type=type(exc).__name__)
        finally:
            row["latency_ms"] = round((time.perf_counter() - start) * 1000, 3)
        if paid and case.case_type != "count":
            try:
                row["cost"] = ledger_usage(service.db, visitor)
            except Exception:
                row["cost"] = {"status": "unavailable", "settled_usd": 0.0, "unresolved_reserved_usd": 0.0}
        check_version(service, version)
        rows.append(row)
    check_version(service, version)
    return {
        "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(),
        "suite": cases[0].suite, "gold_status": "draft_not_frozen", "mode": "paid" if paid else "lexical",
        "data_version": version, "data_version_status": "stable_during_run",
        "generation_model": service.settings.generation_model if paid else None,
        "embedding_model": service.settings.embedding_model if paid else None,
        "max_quote_words": MAX_QUOTE_WORDS,
        "implementation": provenance,
        "summary": summarize_results(rows, paid), "cases": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path("eval/development.jsonl"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--paid", action="store_true", help="Explicitly allow paid Service.answer requests for ready non-count cases")
    parser.add_argument("--expected-data-version", help="Optionally require a previously recorded exact dataset version")
    args = parser.parse_args()
    cases = load_cases(args.cases)
    result = run_evaluation(cases, Service(Settings.from_env()), paid=args.paid,
                            expected_data_version=args.expected_data_version)
    result["case_file"] = args.cases.as_posix()
    import hashlib
    result["case_file_sha256"] = hashlib.sha256(args.cases.read_bytes()).hexdigest()
    output = args.output or Path("outputs") / f"evaluation-{result['suite']}-{result['run_id']}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"output": str(output.resolve()), "mode": result["mode"],
                      "gold_status": result["gold_status"], "summary": result["summary"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
