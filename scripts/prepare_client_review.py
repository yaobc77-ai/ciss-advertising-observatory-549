"""Check a client review packet and optionally freeze reviewed collection inputs.

No retrieval, model calls, database writes, or acceptance scores are produced.
The existing observatory.evaluate module validates cases and source evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from observatory.config import Settings
from observatory.db import Database
from observatory.evaluate import (
    FROZEN_FORMAT_VERSION,
    EvaluationCase,
    EvaluationInvalid,
    canonical_digest,
    frozen_supports,
    load_snapshot,
    review_plan_blockers,
    reviewed_group_rows,
    scope_allows_case,
    snapshot_identity,
    strict_json_loads,
    validate_gold,
)

ROOT = Path(__file__).resolve().parents[1]
PACKET_FILES = ("review_plan.json", "questions.jsonl", "article_groups.csv")
SEEN_FILES = (ROOT / "eval/development.jsonl", ROOT / "eval/acceptance.draft.jsonl")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def normalized(text):
    return " ".join(text.casefold().split())


def parse_cases(data):
    return [EvaluationCase.model_validate(strict_json_loads(line)) for line in data.decode("utf-8-sig").splitlines()
            if line.strip()]


def inspect_packet(folder, seen_files=SEEN_FILES):
    """Only inspect local inputs. Human declarations are recorded, not inferred."""
    errors = []
    payloads = {}
    for name in PACKET_FILES:
        path = folder / name
        if not path.is_file():
            errors.append(f"missing_file:{name}")
        else:
            payloads[name] = path.read_bytes()
    if errors:
        return {"blockers": errors, "payloads": payloads}
    try:
        plan = strict_json_loads(payloads["review_plan.json"])
        if not isinstance(plan, dict):
            raise ValueError("review_plan must be an object")
        cases = parse_cases(payloads["questions.jsonl"])
        group_rows = reviewed_group_rows(payloads["article_groups.csv"])
        seen_payloads = {str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path): path.read_bytes()
                         for path in seen_files}
        seen = [case for data in seen_payloads.values() for case in parse_cases(data)]
    except (ValueError, OSError, EvaluationInvalid) as exc:
        return {"blockers": [f"invalid_input:{type(exc).__name__}"], "payloads": payloads}

    errors.extend(review_plan_blockers(plan))
    if not cases:
        errors.append("no_client_questions_or_gold")
    if len({case.id for case in cases}) != len(cases):
        errors.append("duplicate_case_ids")
    if len({normalized(case.question) for case in cases}) != len(cases):
        errors.append("duplicate_questions")
    seen_questions = {normalized(case.question) for case in seen}
    for case in cases:
        if not scope_allows_case(plan.get("scope"), case):
            errors.append(f"unsupported_case_scope:{case.id}")
        if not case.question.strip() or not case.selection_note.strip() or any(not r.strip() for r in case.rubric):
            errors.append(f"missing_case_context:{case.id}")
        if normalized(case.question) in seen_questions:
            errors.append(f"question_previously_used:{case.id}")

    groups = {}
    for row in group_rows:
        rid, group, split = ((row.get(key) or "").strip() for key in ("record_id", "article_group_id", "split"))
        if not rid or not group or split not in {"development", "holdout"} or rid in groups:
            errors.append("invalid_or_duplicate_article_group_row")
            continue
        groups[rid] = {"group": group, "split": split}
    seen_records = ({rid for case in seen for rid in case.required_record_ids}
                    | {quote.record_id for case in seen for quote in case.support_quote})
    candidate_records = ({rid for case in cases for rid in case.required_record_ids}
                         | {quote.record_id for case in cases for quote in case.support_quote})
    missing_groups = sorted((seen_records | candidate_records) - groups.keys())
    if missing_groups:
        errors.append(f"missing_article_groups:{len(missing_groups)}_records")
    for rid in sorted(seen_records & groups.keys()):
        if groups[rid]["split"] != "development":
            errors.append(f"previously_used_record_not_development:{rid}")
    for rid in sorted(candidate_records & groups.keys()):
        if groups[rid]["split"] != "holdout":
            errors.append(f"candidate_record_not_holdout:{rid}")
    split_groups = {split: {v["group"] for v in groups.values() if v["split"] == split}
                    for split in ("development", "holdout")}
    if split_groups["development"] & split_groups["holdout"]:
        errors.append("article_group_leakage")
    return {"blockers": errors, "payloads": payloads, "plan": plan, "cases": cases,
            "groups": groups, "seen_records": seen_records, "candidate_records": candidate_records,
            "seen_payloads": seen_payloads}


def check_sources(packet, db):
    """Reuse evaluator gold checks; refuse stale versions and exact-body leakage."""
    if packet.get("blockers") or not packet.get("cases"):
        raise EvaluationInvalid("review_prerequisites_not_ready")
    expected = packet["plan"]["expected_data_version"]
    health = db.health()
    if health.get("data_version") != expected:
        raise EvaluationInvalid("data_version_changed")
    snapshot = load_snapshot(db)
    unknown = set(packet["groups"]) - set(snapshot)
    if unknown:
        raise EvaluationInvalid("article_groups_contain_unknown_records")
    filtered = {case.id: db.public_rows(case.filters) for case in packet["cases"] if case.status == "ready"}
    located = validate_gold(packet["cases"], snapshot, filtered, data_version=expected)
    seen_hashes = {sha256(normalized(snapshot[rid]["body"]).encode("utf-8"))
                   for rid in packet["seen_records"] if rid in snapshot and snapshot[rid]["body"].strip()}
    for rid in packet["candidate_records"]:
        body = snapshot[rid]["body"]
        if body.strip() and sha256(normalized(body).encode("utf-8")) in seen_hashes:
            raise EvaluationInvalid("exact_body_overlap_with_previously_used_records")
    final_health = db.health()
    if final_health.get("data_version") != expected:
        raise EvaluationInvalid("data_changed_during_source_validation")
    identity = snapshot_identity(health)
    if snapshot_identity(final_health) != identity:
        raise EvaluationInvalid("source_or_index_changed_during_source_validation")
    packet["validated_supports"] = frozen_supports(packet["cases"], snapshot, filtered, located, identity)
    packet["validated_cases_sha256"] = canonical_digest([case.model_dump(mode="json") for case in packet["cases"]])
    return located


def freeze_packet(packet, folder, output, located):
    """Write an immutable-input handoff, never a pass/fail acceptance verdict."""
    if packet["blockers"] or not packet.get("cases"):
        raise EvaluationInvalid("review_prerequisites_not_ready")
    if set(located) != {case.id for case in packet["cases"] if case.status == "ready"}:
        raise EvaluationInvalid("source_validation_missing")
    for name, original in packet["payloads"].items():
        if (folder / name).read_bytes() != original:
            raise EvaluationInvalid("packet_changed_during_validation")
    for name, original in packet["seen_payloads"].items():
        if (ROOT / name).read_bytes() != original:
            raise EvaluationInvalid("previously_used_cases_changed_during_validation")
    supports = packet.get("validated_supports")
    if not supports or located != {row["case_id"]: row["gold_spans"] for row in supports["cases"] if row["status"] == "ready"}:
        raise EvaluationInvalid("source_validation_missing")
    if (packet["plan"] != strict_json_loads(packet["payloads"]["review_plan.json"])
            or [case.model_dump(mode="json") for case in packet["cases"]]
            != [case.model_dump(mode="json") for case in parse_cases(packet["payloads"]["questions.jsonl"])]
            or packet.get("validated_cases_sha256") != canonical_digest([case.model_dump(mode="json") for case in packet["cases"]])):
        raise EvaluationInvalid("packet_changed_during_validation")
    payloads = dict(packet["payloads"])
    payloads["reviewed_supports.json"] = (json.dumps(supports, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    seen_files = {}
    for number, (name, data) in enumerate(sorted(packet["seen_payloads"].items()), 1):
        frozen_name = f"previously_used_cases/{number:04d}.jsonl"
        seen_files[frozen_name] = name
        payloads[frozen_name] = data
    manifest = {
        "format_version": FROZEN_FORMAT_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "inputs_frozen_awaiting_execution_and_human_review", "scope": packet["plan"]["scope"],
        "data_version": packet["plan"]["expected_data_version"],
        "snapshot_identity": supports["snapshot_identity"],
        "input_sha256": {name: sha256(data) for name, data in payloads.items()},
        "previously_used_case_sha256": {name: sha256(data) for name, data in packet["seen_payloads"].items()},
        "seen_case_files": seen_files,
        "case_ids": [case.id for case in packet["cases"]],
        "cases_sha256": canonical_digest([case.model_dump(mode="json") for case in packet["cases"]]),
        "article_groups": packet["groups"], "gold_spans": located,
        "reviewer": packet["plan"]["reviewer"], "approval_record": packet["plan"]["approval_record"],
        "semantic_acceptance": "pending_human_review", "overall_pass": None,
        "limits": ["Human declarations and near-duplicate grouping require audit.",
                   "Frozen inputs do not establish semantic correctness or customer acceptance.",
                   "Pending social/cross cases remain pending; no-evidence answers require human review."],
    }
    output.mkdir(parents=True, exist_ok=False)
    for name, data in payloads.items():
        (output / name).parent.mkdir(parents=True, exist_ok=True)
        (output / name).write_bytes(data)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, default=ROOT / "eval/client_review_20260925")
    parser.add_argument("--check-sources", action="store_true", help="Read current DB; no API calls")
    parser.add_argument("--freeze", action="store_true", help="Check sources and copy approved inputs to a new directory")
    parser.add_argument("--output", type=Path, help="Required with --freeze; must not exist")
    args = parser.parse_args(argv)
    if args.freeze and args.output is None:
        parser.error("--freeze requires --output")
    packet = inspect_packet(args.packet)
    blockers = list(packet["blockers"])
    status = "pending" if blockers else "ready_for_source_validation"
    if not blockers and (args.check_sources or args.freeze):
        try:
            located = check_sources(packet, Database(Settings.from_env().database_url))
            status = "sources_validated_awaiting_freeze"
            if args.freeze:
                freeze_packet(packet, args.packet, args.output, located)
                status = "inputs_frozen_awaiting_execution_and_human_review"
        except EvaluationInvalid as exc:
            blockers.append(str(exc))
            status = "pending"
        except Exception as exc:
            # Never print database URLs, credentials, or driver exception details.
            blockers.append(f"source_check_or_freeze_failed:{type(exc).__name__}")
            status = "pending"
    print(json.dumps({"status": status, "blockers": blockers, "overall_pass": None,
                      "model_calls": 0, "scope": packet.get("plan", {}).get("scope"),
                      "pending_collection_cases": sum(case.status == "pending_social" for case in packet.get("cases", [])),
                      "semantic_acceptance": "pending_human_review",
                      "formal_social_status": "pending_customer_data_and_release_review"},
                     ensure_ascii=False, indent=2))
    return 2 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
