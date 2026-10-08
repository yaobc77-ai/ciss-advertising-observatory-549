"""Draft evidence evaluation. Free lexical retrieval is the default; --paid is explicit."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
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


class ReviewedDatasetRelease(BaseModel):
    """Recorded human approval of a corpus version; not proof of that approval."""

    model_config = ConfigDict(extra="forbid")
    data_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: str = Field(min_length=1)
    approval_record: str = Field(min_length=1)

    @model_validator(mode="after")
    def reject_blank_review(self):
        if not self.reviewer.strip() or not self.approval_record.strip():
            raise ValueError("Reviewed releases require a reviewer and approval record")
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
    reviewed_release: ReviewedDatasetRelease | None = None

    @model_validator(mode="after")
    def check_case(self):
        if self.filters.dataset != {"native": "native", "social": "social", "cross": "all"}[self.dataset]:
            raise ValueError("Dataset scope and Filters.dataset disagree")
        if len(self.required_record_ids) != len(set(self.required_record_ids)):
            raise ValueError("Duplicate required record IDs")
        if self.status == "pending_social":
            if (self.dataset == "native" or self.required_record_ids or self.support_quote
                    or self.expected_count is not None or self.reviewed_release is not None):
                raise ValueError("Pending social/cross cases must not contain invented gold")
            return self
        if self.dataset != "native" and self.reviewed_release is None:
            raise ValueError("Social/cross readiness requires a separately reviewed dataset release")
        if self.case_type == "retrieval":
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


FROZEN_FORMAT_VERSION = 2
FROZEN_INPUT_FILES = {"review_plan.json", "questions.jsonl", "article_groups.csv", "reviewed_supports.json"}
SNAPSHOT_FIELDS = ("data_version", "source_data_version", "index_version", "active_profile")


def strict_json_loads(data):
    """Reject contradictory keys and nonfinite values in review authority files."""
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise EvaluationInvalid("Duplicate JSON key in reviewed or frozen input")
            result[key] = value
        return result

    def invalid_constant(value):
        raise EvaluationInvalid("Nonfinite JSON value in reviewed or frozen input")

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise EvaluationInvalid("Nonfinite JSON value in reviewed or frozen input")
        return parsed

    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data
    return json.loads(text, object_pairs_hook=unique_keys, parse_constant=invalid_constant, parse_float=finite_float)


def reviewed_group_rows(payload):
    """Read the documented CSV columns without DictReader's silent overwrites."""
    try:
        reader = csv.reader(io.StringIO(payload.decode("utf-8-sig"), newline=""), strict=True)
        header = next(reader, [])
        required = {"record_id", "article_group_id", "split"}
        if (len(header) != len(set(header)) or not required.issubset(header)
                or set(header) - required - {"review_note"}):
            raise EvaluationInvalid("Reviewed article-group CSV has missing, duplicate or unknown columns")
        rows = []
        for cells in reader:
            if not cells:
                continue
            if len(cells) != len(header):
                raise EvaluationInvalid("Reviewed article-group CSV has missing or extra cells")
            rows.append(dict(zip(header, cells)))
        return rows
    except (csv.Error, UnicodeError) as exc:
        raise EvaluationInvalid("Reviewed article-group CSV is malformed") from exc


def file_digest(data):
    """Hash original file bytes, without newline or encoding normalization."""
    return hashlib.sha256(data).hexdigest()


def canonical_digest(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def review_plan_blockers(plan):
    """Record manual review declarations; these are not semantic acceptance."""
    errors = []
    if not isinstance(plan.get("scope"), str) or plan["scope"] not in {"native", "social", "cross"}:
        errors.append("invalid_dataset_scope")
    for name in ("reviewer", "approval_record", "question_source_note", "acceptance_criteria"):
        if not isinstance(plan.get(name), str) or not plan[name].strip():
            errors.append(f"missing_review:{name}")
    for name in ("representative_tasks_approved", "not_used_for_tuning", "article_groups_reviewed"):
        if plan.get(name) is not True:
            errors.append(f"unconfirmed:{name}")
    if not re.fullmatch(r"[0-9a-f]{64}", str(plan.get("expected_data_version", ""))):
        errors.append("missing_or_invalid_expected_data_version")
    return errors


def scope_allows_case(scope, case):
    return (case.suite == "acceptance_draft" and isinstance(scope, str) and scope in {"native", "social", "cross"}
            and (scope == "cross" or case.dataset == scope))


def snapshot_identity(health):
    """Freeze collection and index identities rather than only a display count."""
    identity = {name: health.get(name) for name in SNAPSHOT_FIELDS}
    if any(not isinstance(value, str) or not value.strip() or value == "unavailable"
           for value in identity.values()):
        raise EvaluationInvalid("Frozen evaluation requires complete source/index/profile identity")
    if not re.fullmatch(r"[0-9a-f]{64}", identity["data_version"]):
        raise EvaluationInvalid("Frozen evaluation requires an exact data version")
    return identity


def frozen_supports(cases, snapshot, filtered_rows, located, identity):
    """Bind reviewed supports, full filtered count sets and original versions.

    No source text is invented and no reviewer verdict is inferred. Pending
    collection cases retain empty bindings and never become ready here.
    """
    return {
        "format_version": 1,
        "snapshot_identity": identity,
        "source_records": {
            rid: {"dataset": record["dataset"], "version_id": record["version_id"],
                  "body_sha256": digest(record["body"]),
                  "payload_sha256": canonical_digest(record["payload"])}
            for rid, record in sorted(snapshot.items())
        },
        "cases": [
            {"case_id": case.id, "dataset": case.dataset, "status": case.status,
             "case_type": case.case_type, "expected_count": case.expected_count,
             "required_record_ids": case.required_record_ids,
             "selected_record_ids": sorted({row["record_id"] for row in filtered_rows.get(case.id, [])}),
             "gold_spans": located.get(case.id, []),
             "reviewed_release": case.reviewed_release.model_dump(mode="json") if case.reviewed_release else None}
            for case in cases
        ],
    }


def _frozen_groups(payload):
    groups = {}
    for row in reviewed_group_rows(payload):
        rid, group, split = ((row.get(key) or "").strip() for key in ("record_id", "article_group_id", "split"))
        if not rid or not group or rid in groups or split not in {"development", "holdout"}:
            raise EvaluationInvalid("Frozen article groups have invalid or duplicate rows")
        groups[rid] = {"group": group, "split": split}
    if ({row["group"] for row in groups.values() if row["split"] == "development"}
            & {row["group"] for row in groups.values() if row["split"] == "holdout"}):
        raise EvaluationInvalid("Frozen article groups leak across development and holdout")
    return groups


def _validate_frozen_supports(supports, cases, identity):
    if (not isinstance(supports, dict) or set(supports) != {"format_version", "snapshot_identity", "source_records", "cases"}
            or supports["format_version"] != 1 or supports["snapshot_identity"] != identity
            or not isinstance(supports["source_records"], dict) or not isinstance(supports["cases"], list)
            or len(supports["cases"]) != len(cases)):
        raise EvaluationInvalid("Frozen support snapshot or case coverage is incomplete")
    sources = supports["source_records"]
    for rid, source in sources.items():
        if (not isinstance(rid, str) or not rid or not isinstance(source, dict)
                or set(source) != {"dataset", "version_id", "body_sha256", "payload_sha256"}
                or source["dataset"] not in {"native", "social"}
                or not isinstance(source["version_id"], str) or not source["version_id"]
                or any(not isinstance(source[name], str) or not re.fullmatch(r"[0-9a-f]{64}", source[name])
                       for name in ("body_sha256", "payload_sha256"))):
            raise EvaluationInvalid("Frozen support source binding is malformed")
    for case, binding in zip(cases, supports["cases"]):
        expected = {"case_id": case.id, "dataset": case.dataset, "status": case.status,
                    "case_type": case.case_type, "expected_count": case.expected_count,
                    "required_record_ids": case.required_record_ids,
                    "reviewed_release": case.reviewed_release.model_dump(mode="json") if case.reviewed_release else None}
        if (not isinstance(binding, dict) or set(binding) != set(expected) | {"selected_record_ids", "gold_spans"}
                or any(binding[name] != value for name, value in expected.items())):
            raise EvaluationInvalid("Frozen question/count/support/review binding differs from its case")
        if case.reviewed_release and case.reviewed_release.data_version != identity["data_version"]:
            raise EvaluationInvalid("Frozen reviewed dataset release belongs to a different version")
        selected, spans = binding["selected_record_ids"], binding["gold_spans"]
        datasets = {"native", "social"} if case.dataset == "cross" else {case.dataset}
        if (not isinstance(selected, list) or any(not isinstance(rid, str) for rid in selected)
                or selected != sorted(set(selected)) or not set(case.required_record_ids).issubset(selected)
                or any(rid not in sources or sources[rid]["dataset"] not in datasets for rid in selected)
                or not isinstance(spans, list) or len(spans) != len(case.support_quote)):
            raise EvaluationInvalid("Frozen support records or dataset scope differ from its case")
        if case.status == "pending_social" and (selected or spans):
            raise EvaluationInvalid("Pending collection case cannot contain frozen gold")
        if case.status == "ready" and case.case_type == "count" and len(selected) != case.expected_count:
            raise EvaluationInvalid("Frozen count gold does not cover its complete filtered set")
        for gold, span in zip(case.support_quote, spans):
            if (not isinstance(span, dict) or set(span) != {"record_id", "version_id", "start", "end", "quote"}
                    or span["record_id"] != gold.record_id or span["quote"] != gold.quote
                    or gold.record_id not in selected or span["version_id"] != sources[gold.record_id]["version_id"]
                    or type(span["start"]) is not int or span["start"] < 0 or type(span["end"]) is not int
                    or span["end"] != span["start"] + len(gold.quote)):
                raise EvaluationInvalid("Frozen support quote or original locator differs from its case")


def load_frozen_manifest(path, case_file, cases):
    """Verify every frozen local input before connecting to the evaluation service.

    A format-1 handoff cannot satisfy this contract because it did not bind a
    complete source/index identity or a separately hashed support artifact.
    """
    path, case_file = Path(path), Path(case_file)
    try:
        manifest_bytes = path.read_bytes()
        manifest = strict_json_loads(manifest_bytes)
        if not isinstance(manifest, dict) or manifest.get("format_version") != FROZEN_FORMAT_VERSION:
            raise EvaluationInvalid("Unsupported frozen manifest format; prepare a format-2 packet")
        if (manifest.get("status") != "inputs_frozen_awaiting_execution_and_human_review"
                or manifest.get("semantic_acceptance") != "pending_human_review"
                or manifest.get("overall_pass") is not None):
            raise EvaluationInvalid("Frozen manifest must not declare acceptance or a semantic pass")
        hashes, seen_files = manifest.get("input_sha256"), manifest.get("seen_case_files")
        if not isinstance(hashes, dict) or not isinstance(seen_files, dict):
            raise EvaluationInvalid("Frozen manifest is missing exact input hashes")
        if any(not re.fullmatch(r"previously_used_cases/[0-9]{4}\.jsonl", name) for name in seen_files):
            raise EvaluationInvalid("Frozen manifest contains an invalid input path")
        if set(hashes) != FROZEN_INPUT_FILES | set(seen_files):
            raise EvaluationInvalid("Frozen manifest input set is incomplete or unexpected")
        payloads = {}
        for name, expected in hashes.items():
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise EvaluationInvalid("Frozen manifest contains an invalid input hash")
            payloads[name] = (path.parent / name).read_bytes()
            if file_digest(payloads[name]) != expected:
                raise EvaluationInvalid(f"Frozen input changed: {name}")
        if case_file.read_bytes() != payloads["questions.jsonl"]:
            raise EvaluationInvalid("Question file differs from the frozen question file")
        bound_cases = [EvaluationCase.model_validate(strict_json_loads(line))
                       for line in payloads["questions.jsonl"].decode("utf-8-sig").splitlines() if line.strip()]
        serialized = [case.model_dump(mode="json") for case in cases]
        if (not cases or serialized != [case.model_dump(mode="json") for case in bound_cases]
                or manifest.get("case_ids") != [case.id for case in cases]
                or manifest.get("cases_sha256") != canonical_digest(serialized)):
            raise EvaluationInvalid("Frozen case identity, order or question/count/support content differs")
        if len({case.id for case in cases}) != len(cases):
            raise EvaluationInvalid("Frozen cases have duplicate IDs")
        plan = strict_json_loads(payloads["review_plan.json"])
        if not isinstance(plan, dict) or review_plan_blockers(plan):
            raise EvaluationInvalid("Frozen manual review prerequisites are not confirmed")
        if (manifest.get("scope") != plan["scope"] or manifest.get("reviewer") != plan["reviewer"]
                or manifest.get("approval_record") != plan["approval_record"]
                or any(not scope_allows_case(plan["scope"], case) for case in cases)):
            raise EvaluationInvalid("Frozen review declaration or dataset scope differs")
        if any(not case.question.strip() or not case.selection_note.strip() or any(not item.strip() for item in case.rubric)
               for case in cases):
            raise EvaluationInvalid("Frozen cases lack reviewed task context")
        identity = snapshot_identity(manifest.get("snapshot_identity", {}))
        if manifest.get("data_version") != identity["data_version"] or plan["expected_data_version"] != identity["data_version"]:
            raise EvaluationInvalid("Frozen review and source/index versions differ")
        groups = _frozen_groups(payloads["article_groups.csv"])
        if manifest.get("article_groups") != groups:
            raise EvaluationInvalid("Frozen article group manifest differs from its bound file")
        seen_cases = [EvaluationCase.model_validate(strict_json_loads(line)) for name in seen_files
                      for line in payloads[name].decode("utf-8-sig").splitlines() if line.strip()]
        previous_hashes = {original: hashes[name] for name, original in seen_files.items()}
        if len(previous_hashes) != len(seen_files) or manifest.get("previously_used_case_sha256") != previous_hashes:
            raise EvaluationInvalid("Frozen previously used case identities differ")
        def normalized(text):
            return " ".join(text.casefold().split())
        questions = [normalized(case.question) for case in cases]
        if len(set(questions)) != len(questions) or set(questions) & {normalized(case.question) for case in seen_cases}:
            raise EvaluationInvalid("Frozen questions duplicate a held-out or previously used question")
        seen_records = ({rid for case in seen_cases for rid in case.required_record_ids}
                        | {quote.record_id for case in seen_cases for quote in case.support_quote})
        candidate_records = ({rid for case in cases for rid in case.required_record_ids}
                             | {quote.record_id for case in cases for quote in case.support_quote})
        if (not (seen_records | candidate_records).issubset(groups)
                or any(groups[rid]["split"] != "development" for rid in seen_records)
                or any(groups[rid]["split"] != "holdout" for rid in candidate_records)):
            raise EvaluationInvalid("Frozen required records violate reviewed article splits")
        supports = strict_json_loads(payloads["reviewed_supports.json"])
        _validate_frozen_supports(supports, cases, identity)
        spans = {row["case_id"]: row["gold_spans"] for row in supports["cases"] if row["status"] == "ready"}
        if manifest.get("gold_spans") != spans:
            raise EvaluationInvalid("Frozen support locators differ from the manifest")
    except EvaluationInvalid:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise EvaluationInvalid(f"Invalid frozen inputs: {type(exc).__name__}") from exc
    return {"manifest": manifest, "supports": supports, "manifest_sha256": file_digest(manifest_bytes),
            "path": path, "case_file": case_file}


def load_cases(path: Path) -> list[EvaluationCase]:
    cases = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
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
            "FROM records r JOIN record_versions v ON r.current_version=v.version_id WHERE r.active"
        ).fetchall()
    return {row["record_id"]: row for row in rows}


def validate_gold(cases, snapshot, filtered_rows, *, data_version=None):
    """Validate source ownership before any retrieval or paid request is made."""
    located = {}
    connected_datasets = {record["dataset"] for record in snapshot.values()}
    for case in cases:
        if case.status != "ready":
            continue
        if case.reviewed_release and case.reviewed_release.data_version != data_version:
            raise EvaluationInvalid(f"{case.id}: reviewed dataset release differs from the current data version")
        datasets = {"native", "social"} if case.dataset == "cross" else {case.dataset}
        if case.dataset != "native" and not datasets.issubset(connected_datasets):
            raise EvaluationInvalid(f"{case.id}: a required collection has no active records in the reviewed snapshot")
        selected = {row["record_id"] for row in filtered_rows[case.id]}
        if any(rid not in snapshot or snapshot[rid]["dataset"] not in datasets for rid in selected):
            raise EvaluationInvalid(f"{case.id}: filtered records belong to a missing or out-of-scope dataset")
        if not set(case.required_record_ids).issubset(selected):
            raise EvaluationInvalid(f"{case.id}: required records are missing or excluded by filters")
        if case.case_type == "count" and selected != set(case.required_record_ids):
            raise EvaluationInvalid(f"{case.id}: count gold is stale; re-enumerate the whole filtered corpus")
        if (case.dataset == "cross" and case.case_type == "retrieval"
                and {snapshot[rid]["dataset"] for rid in case.required_record_ids} != datasets):
            raise EvaluationInvalid(f"{case.id}: cross-collection retrieval gold must include both datasets")
        spans = []
        for gold in case.support_quote:
            record = snapshot.get(gold.record_id)
            if not record or gold.record_id not in selected or record["dataset"] not in datasets:
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


def check_version(service, expected, frozen_identity=None):
    health = service.db.health()
    current = health.get("data_version")
    if not current or current == "unavailable" or current != expected:
        raise EvaluationInvalid("Data changed during evaluation; this run is invalid and must not be scored")
    if frozen_identity is not None and snapshot_identity(health) != frozen_identity:
        raise EvaluationInvalid("Source/index/profile changed during frozen evaluation; this run is invalid")


def summarize_results(rows, paid):
    summaries = {}
    for dataset in ("native", "social", "cross"):
        group = [row for row in rows if row["dataset"] == dataset]
        ready = [row for row in group if row["case_status"] == "ready"]
        retrieval = [row for row in ready if row["case_type"] == "retrieval"]
        counts = [row for row in ready if row["case_type"] == "count"]
        count_answers = [row for row in counts if row.get("count_answer_evaluated")]
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
            "answer_count_exact": ratio(sum(row.get("answer_count_exact") is True for row in count_answers), len(count_answers)),
            "answer_count_scope_valid": ratio(sum(row.get("answer_count_scope_valid") is True for row in count_answers), len(count_answers)),
            "count_answer_status": "measured" if count_answers else "not_run_database_only",
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


def serialized_filters_match(actual, expected):
    """Require complete JSON scope evidence; never fill omitted answer fields."""
    wanted = expected.model_dump(mode="json")
    if not isinstance(actual, dict) or actual.keys() != wanted.keys():
        return False
    for key, value in wanted.items():
        candidate = actual[key]
        if isinstance(value, list):
            if (not isinstance(candidate, list) or any(type(item) is not str for item in candidate)
                    or set(candidate) != set(value)):
                return False
        elif type(candidate) is not type(value) or candidate != value:
            return False
    return True


def count_answer_checks(result, case, expected_collections):
    """Score structured database totals and scope, never model-written numbers."""
    data = result.structured_result or {}
    returned = {}
    scope_valid = serialized_filters_match(data.get("filters"), case.filters)
    try:
        for collection in data["collections"]:
            dataset, total = collection["dataset"], collection["total"]
            if dataset in returned or type(total) is not int or total < 0:
                raise ValueError("Malformed collection totals")
            returned[dataset] = total
    except (KeyError, TypeError, ValueError):
        returned = {}
    route_valid = (result.status == "answered" and not result.failure_reason and result.answer_mode == "statistics"
                   and data.get("method") == "database")
    return {"answer_count_exact": route_valid and scope_valid and returned == expected_collections,
            "answer_count_scope_valid": scope_valid, "answer_count_route_valid": route_valid,
            "answer_collection_counts": returned}


def run_evaluation(cases, service, *, paid=False, answer_counts=False, expected_data_version=None,
                   frozen_manifest=None, case_file=None):
    if answer_counts and not paid:
        raise EvaluationInvalid("Service.answer count evaluation requires explicit --paid authorization")
    frozen = None
    if frozen_manifest is not None:
        case_file = Path(case_file) if case_file is not None else Path(frozen_manifest).parent / "questions.jsonl"
        frozen = load_frozen_manifest(frozen_manifest, case_file, cases)
    identity = frozen["manifest"]["snapshot_identity"] if frozen else None
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
    health = service.db.health()
    version = health.get("data_version")
    if not version or version == "unavailable":
        raise EvaluationInvalid("A current database version is required")
    if expected_data_version and expected_data_version != version:
        raise EvaluationInvalid("Database version differs from --expected-data-version")
    if identity is not None and snapshot_identity(health) != identity:
        raise EvaluationInvalid("Current collection or retrieval index differs from the frozen snapshot")
    snapshot = load_snapshot(service.db)
    filtered = {case.id: service.browse(case.filters) for case in cases if case.status == "ready"}
    located = validate_gold(cases, snapshot, filtered, data_version=version)
    if frozen and frozen_supports(cases, snapshot, filtered, located, identity) != frozen["supports"]:
        raise EvaluationInvalid("Frozen source versions, filtered counts or support locators differ from current records")
    check_version(service, version, identity)
    rows = []
    for case in cases:
        row = {
            "id": case.id, "dataset": case.dataset, "case_type": case.case_type,
            "case_status": case.status, "execution_status": "pending_social",
            "question": case.question, "filters": case.filters.model_dump(mode="json"),
            "required_record_ids": case.required_record_ids, "gold_spans": located.get(case.id, []),
            "reviewed_release": case.reviewed_release.model_dump() if case.reviewed_release else None,
            "hit_at_5": False if case.status == "ready" and case.case_type == "retrieval" else None,
            "support_passage_coverage": ratio(0, len(case.support_quote) if case.status == "ready" and case.case_type == "retrieval" else 0),
            "count_exact": None, "abstained": None,
            "count_answer_evaluated": answer_counts and case.status == "ready" and case.case_type == "count",
            "answer_count_exact": None, "answer_count_scope_valid": None,
            "citation_locator_valid": ratio(0, 0), "evidence_locator_valid": ratio(0, 0),
            "semantic_support": "pending_human_review", "latency_ms": 0,
            "cost": {"status": "not_dispatched", "settled_usd": 0.0, "unresolved_reserved_usd": 0.0},
        }
        if case.status == "pending_social":
            rows.append(row)
            continue
        check_version(service, version, identity)
        if frozen:
            current = load_frozen_manifest(frozen_manifest, case_file, cases)
            if current["manifest_sha256"] != frozen["manifest_sha256"]:
                raise EvaluationInvalid("Frozen manifest changed during evaluation")
        visitor = f"evaluation:{run_id}:{case.id}"
        start = time.perf_counter()
        result = None
        evidence = []
        try:
            if case.case_type == "count":
                datasets = ["native", "social"] if case.dataset == "cross" else [case.dataset]
                expected_collections = {dataset: sum(snapshot[rid]["dataset"] == dataset
                                                   for rid in case.required_record_ids)
                                        for dataset in datasets}
                actual_collections = {dataset: service.statistics(case.filters.model_copy(update={"dataset": dataset}))["total"]
                                      for dataset in datasets}
                actual = sum(actual_collections.values())
                row.update(actual_count=actual, expected_count=case.expected_count,
                           actual_collection_counts=actual_collections, expected_collection_counts=expected_collections,
                           count_exact=actual_collections == expected_collections, execution_status="count_only")
                if answer_counts:
                    result = service.answer(case.question, case.filters, visitor)
                    row.update(answer_status=result.status, answer=result.answer,
                               answer_mode=result.answer_mode, failure_reason=result.failure_reason,
                               structured_result=result.structured_result, research_trace=result.research_trace,
                               reported_answer_cost_usd=result.cost_usd, execution_status=result.status)
                    row.update(count_answer_checks(result, case, expected_collections))
            else:
                if paid:
                    result = service.answer(case.question, case.filters, visitor)
                    evidence = result.evidence
                    row.update(answer_status=result.status, answer=result.answer,
                               failure_reason=result.failure_reason,
                               language_check=result.language_check,
                               answer_mode=result.answer_mode, structured_result=result.structured_result,
                               research_trace=result.research_trace,
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
                selected_ids = {item["record_id"] for item in filtered[case.id]}
                valid = {e.evidence_id: service.db.validate_evidence(e) and
                         e.record_id in selected_ids and e.record_id in snapshot
                         and snapshot[e.record_id]["version_id"] == e.version_id
                         and snapshot[e.record_id]["dataset"] == e.dataset
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
        if paid and (case.case_type != "count" or answer_counts):
            try:
                row["cost"] = ledger_usage(service.db, visitor)
            except Exception:
                row["cost"] = {"status": "unavailable", "settled_usd": 0.0, "unresolved_reserved_usd": 0.0}
        check_version(service, version, identity)
        rows.append(row)
    check_version(service, version, identity)
    if frozen:
        current = load_frozen_manifest(frozen_manifest, case_file, cases)
        if current["manifest_sha256"] != frozen["manifest_sha256"]:
            raise EvaluationInvalid("Frozen manifest changed during evaluation")
    return {
        "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(),
        "suite": cases[0].suite, "gold_status": "frozen_inputs_verified" if frozen else "draft_not_frozen", "mode": "paid" if paid else "lexical",
        "count_evaluation_mode": "service_answer" if answer_counts else "database_only",
        "research_agent_enabled": bool(getattr(service.settings, "research_agent_enabled", False)),
        "data_version": version, "data_version_status": "stable_during_run",
        "frozen_inputs": {"manifest_sha256": frozen["manifest_sha256"], "scope": frozen["manifest"]["scope"],
                          "snapshot_identity": identity, "input_sha256": frozen["manifest"]["input_sha256"],
                          "reviewer": frozen["manifest"]["reviewer"], "approval_record": frozen["manifest"]["approval_record"],
                          "semantic_acceptance": "pending_human_review", "overall_pass": None} if frozen else None,
        "generation_model": service.settings.generation_model if paid else None,
        "embedding_model": service.settings.embedding_model if paid else None,
        "max_quote_words": MAX_QUOTE_WORDS,
        "implementation": provenance,
        "summary": summarize_results(rows, paid), "cases": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, help="Question file; defaults to frozen questions when --frozen-manifest is used, otherwise eval/development.jsonl")
    parser.add_argument("--frozen-manifest", type=Path, help="Verify a format-2 frozen review manifest and all bound files before evaluation")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--paid", action="store_true", help="Explicitly allow paid Service.answer requests; count cases require --answer-counts too")
    parser.add_argument("--answer-counts", action="store_true", help="Also evaluate count questions through Service.answer; requires --paid")
    parser.add_argument("--expected-data-version", help="Optionally require a previously recorded exact dataset version")
    args = parser.parse_args(argv)
    if args.answer_counts and not args.paid:
        parser.error("--answer-counts requires --paid")
    case_file = args.cases or (args.frozen_manifest.parent / "questions.jsonl" if args.frozen_manifest else Path("eval/development.jsonl"))
    cases = load_cases(case_file)
    if args.frozen_manifest:
        # Refuse modified/malformed inputs before even constructing a service.
        load_frozen_manifest(args.frozen_manifest, case_file, cases)
    result = run_evaluation(cases, Service(Settings.from_env()), paid=args.paid, answer_counts=args.answer_counts,
                            expected_data_version=args.expected_data_version,
                            frozen_manifest=args.frozen_manifest, case_file=case_file)
    result["case_file"] = case_file.as_posix()
    result["case_file_sha256"] = file_digest(case_file.read_bytes())
    output = args.output or Path("outputs") / f"evaluation-{result['suite']}-{result['run_id']}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"output": str(output.resolve()), "mode": result["mode"],
                      "gold_status": result["gold_status"], "summary": result["summary"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
