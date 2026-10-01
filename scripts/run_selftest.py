"""Run the self-made development cases, with independent database gold.

Free: --rules and --lexical. Paid: --agent and --hybrid require --paid.
--ids S01,S19,R01 bounds a run to named cases. Existing application budgets
and quotas still apply; pacing between questions is not a per-model-call limit.
Fresh receipts are saved before access and after each case/retrieval variant.
These checks are not independent semantic review or customer acceptance.
"""

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg

from observatory.chunking import retrieval_spans
from observatory.config import Settings
from observatory.models import Filters
from observatory.service import Service
from observatory.structured_queries import plan_question

ROOT = Path(__file__).resolve().parents[1]
CASE_FILE = ROOT / "eval" / "selftest" / "cases.json"
CASE_BYTES = CASE_FILE.read_bytes()
CASES = json.loads(CASE_BYTES)
NATIVE = Filters(dataset="native")
VARIANTS = ("en", "zh", "paraphrase")
BASE = """FROM records r JOIN record_versions v ON v.version_id=r.current_version
 WHERE r.active AND r.dataset='native' AND (v.payload->>'countable')::boolean"""


def expected_filters(case, key="constraints"):
    constraints = case.get(key, {})
    filters = Filters(dataset="native")
    for field in ("publisher", "sponsor"):
        if field in constraints:
            setattr(filters, field + "s", [constraints[field]])
    if "years" in constraints:
        filters.date_from = date(constraints["years"][0], 1, 1)
        filters.date_to = date(constraints["years"][1], 12, 31)
        filters.include_unknown_dates = False
    return filters


def same_scope(actual, expected):
    """Compare every emitted filter, without silently filling missing fields."""
    wanted = expected.model_dump(mode="json")
    if not isinstance(actual, dict) or actual.keys() != wanted.keys():
        return False
    for key, value in wanted.items():
        candidate = actual[key]
        if isinstance(value, list):
            if not isinstance(candidate, list) or not all(isinstance(v, str) for v in candidate):
                return False
            if set(candidate) != set(value):
                return False
        elif type(candidate) is not type(value) or candidate != value:
            return False
    return True


def gold_where(constraints):
    where, params = BASE, []
    for field in ("publisher", "sponsor"):
        if field in constraints:
            where += f" AND v.payload->>'{field}'=%s"
            params.append(constraints[field])
    if "years" in constraints:
        where += " AND (v.payload->>'published_at')::date BETWEEN %s AND %s"
        params += [date(constraints["years"][0], 1, 1), date(constraints["years"][1], 12, 31)]
    return where, params


def gold_statistics(conn, case):
    where, params = gold_where(case.get("constraints", {}))
    if case["expect"] == "group":
        field = case["group_by"]
        if field not in ("publisher", "sponsor"):
            raise ValueError("unsupported gold dimension")
        return dict(conn.execute(
            f"SELECT COALESCE(NULLIF(v.payload->>'{field}',''),'(Unknown)'), count(*) {where} GROUP BY 1",
            params).fetchall())
    numerator = conn.execute(f"SELECT count(*) {where}", params).fetchone()[0]
    if case["expect"] != "share":
        return numerator
    where, params = gold_where(case["denominator_constraints"])
    denominator = conn.execute(f"SELECT count(*) {where}", params).fetchone()[0]
    return {"numerator": numerator, "denominator": denominator,
            "percentage": 100 * numerator / denominator if denominator else None}


def nonnegative_integer(value):
    return type(value) is int and value >= 0


def outcome(answer):
    result = answer.structured_result
    return {"mode": answer.answer_mode, "status": answer.status,
            "failure": answer.failure_reason, "result": result if isinstance(result, dict) else {}}


def judgment(case, gold, answer):
    """Strict structured checks; no_count checks routing only, not answer meaning."""
    observed = outcome(answer)
    result = observed["result"]
    if case["expect"] == "no_count":
        checks = {
            "available": answer.status == "answered" or
            (answer.status == "insufficient_evidence" and answer.answer_mode == "clarification"),
            "no_failure": not answer.failure_reason,
            "route_only": answer.answer_mode != "statistics" and result.get("kind") not in
            {"count", "share", "list_publishers", "list_sponsors", "list_platforms"},
        }
        return checks
    checks = {"answered": answer.status == "answered", "no_failure": not answer.failure_reason,
              "route": answer.answer_mode == "statistics" and result.get("method") == "database",
              "scope": same_scope(result.get("filters"), expected_filters(case))}
    group_by = case.get("group_by")
    kind = {"publisher": "list_publishers", "sponsor": "list_sponsors"}.get(group_by, case["expect"])
    checks["kind_and_dimension"] = result.get("kind") == kind and result.get("group_by") == (
        group_by + "s" if group_by else None)
    collections = result.get("collections")
    valid_collection = (isinstance(collections, list) and len(collections) == 1 and
                        isinstance(collections[0], dict) and collections[0].get("dataset") == "native" and
                        nonnegative_integer(collections[0].get("total")))
    checks["collection"] = valid_collection
    collection = collections[0] if valid_collection else {}
    total = collection.get("total")
    groups = result.get("groups")
    if case["expect"] == "group":
        mapped = {}
        valid_groups = isinstance(groups, list)
        for item in groups if valid_groups else []:
            if (not isinstance(item, dict) or item.get("dataset") != "native" or
                    not isinstance(item.get("name"), str) or not item["name"] or
                    not nonnegative_integer(item.get("count")) or item["name"] in mapped):
                valid_groups = False
                break
            mapped[item["name"]] = item["count"]
        checks["groups"] = valid_groups and mapped == gold
        checks["total"] = nonnegative_integer(total) and total == sum(gold.values())
    elif case["expect"] == "count":
        checks["groups"] = groups == []
        checks["total"] = nonnegative_integer(total) and total == gold
    else:
        checks["groups"] = groups == []
        checks["denominator_scope"] = same_scope(
            result.get("denominator_filters"), expected_filters(case, "denominator_constraints"))
        checks["denominator_basis"] = result.get("denominator_basis") == "current_selection_before_question_targets"
        checks["numerator"] = nonnegative_integer(collection.get("numerator")) and (
            collection.get("numerator") == gold["numerator"] == total)
        checks["denominator"] = nonnegative_integer(collection.get("denominator")) and (
            collection.get("denominator") == gold["denominator"])
        percentage = collection.get("percentage")
        if gold["denominator"]:
            checks["percentage"] = (type(percentage) in (int, float) and math.isfinite(percentage) and
                                    math.isclose(percentage, gold["percentage"], rel_tol=1e-9, abs_tol=1e-9) and
                                    collection.get("percentage_status") == "defined")
        else:
            checks["percentage"] = percentage is None and collection.get("percentage_status") == "empty_selection"
    return checks


def judge(case, gold, answer):
    return all(judgment(case, gold, answer).values())


def public_scalar(value):
    if type(value) is float and not math.isfinite(value):
        return None
    return value if type(value) in (str, int, float, bool, type(None)) else None


def public_filters(value):
    if not isinstance(value, dict):
        return None
    return {key: [public_scalar(item) for item in field] if isinstance(field, list) else public_scalar(field)
            for key, field in value.items() if key in Filters.model_fields}


def record_answer(row, case, gold, answer):
    observed = outcome(answer)
    result = observed.pop("result")
    cost = answer.cost_usd
    if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
        raise ValueError("invalid reported cost")
    row.update(observed, gold=gold, checks=judgment(case, gold, answer), cost_usd=cost)
    for key in ("kind", "method", "group_by", "denominator_basis"):
        row[key] = public_scalar(result.get(key))
    for key in ("filters", "denominator_filters"):
        row[key] = public_filters(result.get(key))
        row[key + "_supplied_keys"] = sorted(result[key]) if isinstance(result.get(key), dict) else None
    # Persist only public result fields; omit record bodies, URLs and arbitrary metadata.
    row["collections"] = [{key: public_scalar(item.get(key)) for key in (
        "dataset", "total", "numerator", "denominator", "percentage", "percentage_status")}
        for item in result.get("collections", []) if isinstance(item, dict)] if isinstance(
            result.get("collections"), list) else None
    row["groups"] = [{key: public_scalar(item.get(key)) for key in ("dataset", "name", "count")}
                     for item in result.get("groups", []) if isinstance(item, dict)] if isinstance(
                         result.get("groups"), list) else None
    row["pass"] = judge(case, gold, answer)
    row["run_status"] = "completed"


def pending_rows(cases, retrieval=False):
    rows = [{"id": case["id"], "run_status": "not_run", "pass": False} for case in cases]
    if retrieval:
        for row in rows:
            row.update({variant: False for variant in VARIANTS})
            row["variants"] = {variant: {"status": "not_run", "cost_usd": 0.0} for variant in VARIANTS}
    return rows


def run_rules(service, conn, cases, rows, checkpoint):
    facets = service.facets("native")
    for case, row in zip(cases, rows, strict=True):
        row["run_status"] = "running"
        checkpoint()
        gold = gold_statistics(conn, case) if case["expect"] != "no_count" else None
        plan = plan_question(case["question"], NATIVE, facets)
        if plan is not None and plan.status == "ready":
            record_answer(row, case, gold, service._statistics_answer(plan))
        else:
            row.update(run_status="completed", status="not_dispatched", mode=plan.status if plan else "rag",
                       gold=gold, checks={"route_only": case["expect"] == "no_count"})
            row["pass"] = case["expect"] == "no_count"
        checkpoint()


def run_agent(service, conn, settings, cases, rows, checkpoint):
    delay = 60 / max(1, settings.requests_per_minute) + 1
    visitor = f"selftest-{uuid4().hex}"
    for index, (case, row) in enumerate(zip(cases, rows, strict=True)):
        if index:
            remaining = delay
            while remaining > 0:
                pause = min(remaining, 30)
                time.sleep(pause)
                remaining -= pause
        row["run_status"] = "running"
        checkpoint()
        gold = gold_statistics(conn, case) if case["expect"] != "no_count" else None
        answer = service.answer(case["question"], NATIVE, visitor)
        record_answer(row, case, gold, answer)
        checkpoint()
        if answer.status in {"limited", "service_unavailable"} or answer.failure_reason:
            raise RuntimeError("paid answer unavailable; remaining cases not dispatched")


def gold_records(conn, phrase):
    if not isinstance(phrase, str) or not phrase.strip():
        raise ValueError("empty retrieval gold phrase")
    rows = conn.execute(f"SELECT r.record_id,v.body,v.payload {BASE} "
                        "AND (v.payload->>'retrievable')::boolean")
    phrase = phrase.casefold()
    gold = set()
    for record_id, body, payload in rows:
        spans = retrieval_spans(body, retrieval_ranges=payload.get("retrieval_ranges"),
                                retrieval_end=payload.get("retrieval_end"))
        if any(phrase in body[start:end].casefold() for start, end in spans):
            gold.add(record_id)
    return gold


def run_retrieval(service, conn, settings, hybrid, cases, rows, checkpoint):
    visitor = f"selftest-{uuid4().hex}"
    for case, row in zip(cases, rows, strict=True):
        row["run_status"] = "running"
        checkpoint()
        gold = gold_records(conn, case["gold_phrase"])
        row["gold_records"] = len(gold)
        if not gold:
            row.update(run_status="invalid_gold", failure="empty_gold")
            checkpoint()
            continue
        for variant in VARIANTS:
            state = row["variants"][variant]
            state["status"] = "running"
            checkpoint()
            costs = []
            try:
                question = case[variant]
                if hybrid:
                    vector = service.rag.embed([question], visitor=visitor, cost_sink=costs)[0]
                    evidence = service.db.search(question, NATIVE, limit=5, vector=vector,
                                                 model=settings.embedding_model, chunks_per_record=3)
                else:
                    evidence = service.search_report(question, NATIVE)["evidence"]
                top = list(dict.fromkeys(e.record_id for e in evidence))[:5]
                row[variant] = bool(gold & set(top))
                state.update(status="completed", retrieved_records=top)
            except Exception as exc:
                state.update(status="failed", error_type=type(exc).__name__)
                row["run_status"] = "failed"
                raise
            finally:
                state["cost_usd"] = sum(costs)
                checkpoint()
        row.update(run_status="completed")
        row["pass"] = all(row[v] for v in VARIANTS)
        checkpoint()


def select_cases(ids):
    cases = CASES["statistics"] + CASES["retrieval"]
    if ids is None:
        return CASES["statistics"], CASES["retrieval"]
    selected = ids.split(",")
    known = {case["id"] for case in cases}
    if any(not item or item not in known for item in selected) or len(selected) != len(set(selected)):
        raise ValueError("--ids requires distinct known IDs separated by commas")
    return ([case for case in CASES["statistics"] if case["id"] in selected],
            [case for case in CASES["retrieval"] if case["id"] in selected])


def atomic_receipt(path, report):
    """Replace only this run's reserved path; never reuse an earlier receipt."""
    data = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False, default=str) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def summarize(report):
    summary = {}
    for mode in report["modes"]:
        rows = report[mode]
        if mode in {"rules", "agent"}:
            summary[mode] = f"{sum(r['pass'] for r in rows)}/{len(rows)}"
        else:
            summary[mode] = {v: f"{sum(r[v] for r in rows)}/{len(rows)}" for v in VARIANTS}
        summary[mode + "_completed"] = sum(r["run_status"] == "completed" for r in rows)
    summary["agent_cost_usd"] = sum(r.get("cost_usd", 0) or 0 for r in report.get("agent", []))
    summary["hybrid_cost_usd"] = sum(state["cost_usd"] for row in report.get("hybrid", [])
                                      for state in row["variants"].values())
    return summary


def implementation_identity():
    """Bind the tested implementation as well as the runner and case rubric."""
    sources = {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted((ROOT / "src").rglob("*.py"))}
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                          text=True, timeout=10)
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "src", "tests", "scripts", "eval"],
                           cwd=ROOT, capture_output=True, text=True, timeout=10)
    return {"code_commit": head.stdout.strip() if head.returncode == 0 else None,
            "worktree_changes": dirty.stdout.splitlines() if dirty.returncode == 0 else None,
            "source_sha256": sources}


def health_identity(service):
    health = service.health()
    return {key: health.get(key) for key in ("status", "data_version", "index_version", "active_profile")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for flag in ("rules", "lexical", "agent", "hybrid", "paid"):
        parser.add_argument(f"--{flag}", action="store_true")
    parser.add_argument("--ids", help="Distinct case IDs, e.g. S01,S19,R01")
    args = parser.parse_args(argv)
    if (args.agent or args.hybrid) and not args.paid:
        parser.error("--agent and --hybrid call the OpenAI API; add --paid to confirm")
    if not any((args.rules, args.lexical, args.agent, args.hybrid)):
        args.rules = args.lexical = True
    try:
        statistics, retrieval = select_cases(args.ids)
    except ValueError as exc:
        parser.error(str(exc))
    modes = [mode for mode in ("rules", "lexical", "agent", "hybrid") if getattr(args, mode)]
    if any(not (statistics if mode in {"rules", "agent"} else retrieval) for mode in modes):
        parser.error("selected IDs must include cases for each requested mode")
    runner_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    case_hash = hashlib.sha256(CASE_BYTES).hexdigest()
    report = {"run_at": datetime.now(timezone.utc).isoformat(), "run_status": "running",
              "evaluation_scope": "self_made_development_checks", "cases_version": CASES["version"],
              "runner_sha256": runner_hash, "cases_sha256": case_hash, "modes": modes,
              "selected_ids": [c["id"] for c in statistics + retrieval],
              "data_scope": "current active countable native records; accepted retrieval intervals",
              "cost_coverage": "Known returned costs and embedding exposure only; usage ledger may contain additional exposure after exceptions.",
              "snapshot_claim": "No shared database snapshot; version equality is only a change guard."}
    for mode in modes:
        report[mode] = pending_rows(statistics if mode in {"rules", "agent"} else retrieval,
                                    retrieval=mode in {"lexical", "hybrid"})
    out = ROOT / "outputs" / f"selftest-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}-{uuid4().hex[:8]}.json"
    out.parent.mkdir(exist_ok=True)
    with out.open("x", encoding="utf-8"):
        pass

    def checkpoint():
        report["summary"] = summarize(report)
        atomic_receipt(out, report)

    checkpoint()
    try:
        if hashlib.sha256(CASE_FILE.read_bytes()).hexdigest() != case_hash:
            raise ValueError("case file changed since loading")
        settings = Settings.from_env()
        if args.agent and not settings.research_agent_enabled:
            raise ValueError("agent mode disabled")
        report.update(generation_model=settings.generation_model, embedding_model=settings.embedding_model,
                      paid_calls_authorized=args.paid,
                      quota_note="Per-question pacing does not bound calls inside the research agent.")
        service = Service(settings)
        report["implementation"] = implementation_identity()
        report["health_start"] = health_identity(service)
        report["data_version_start"] = report["health_start"]["data_version"]
        if report["health_start"]["status"] != "ok" or not all(report["health_start"].values()):
            raise ValueError("missing source/index identity")
        checkpoint()
        with psycopg.connect(settings.database_url) as conn:
            for mode in modes:
                if mode == "rules":
                    run_rules(service, conn, statistics, report[mode], checkpoint)
                elif mode == "agent":
                    run_agent(service, conn, settings, statistics, report[mode], checkpoint)
                else:
                    run_retrieval(service, conn, settings, mode == "hybrid", retrieval, report[mode], checkpoint)
        report["health_end"] = health_identity(service)
        report["end_service_available"] = report["health_end"]["status"] == "ok" and all(
            report["health_end"].values())
        report["data_version_end"] = report["health_end"]["data_version"]
        report["data_version_unchanged"] = report["data_version_end"] == report["data_version_start"]
        report["index_unchanged"] = all(report["health_end"][key] == report["health_start"][key]
                                       for key in ("index_version", "active_profile"))
        report["implementation_unchanged"] = implementation_identity() == report["implementation"]
        report["artifacts_unchanged"] = (hashlib.sha256(CASE_FILE.read_bytes()).hexdigest() == case_hash and
                                         hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == runner_hash)
        passed = all(row["pass"] and row["run_status"] == "completed" for mode in modes for row in report[mode])
        report["run_status"] = "passed" if (passed and report["data_version_unchanged"] and
                                             report["index_unchanged"] and report["artifacts_unchanged"] and
                                             report["implementation_unchanged"] and
                                             report["end_service_available"]) else "failed"
        if not report["end_service_available"]:
            report["failure_code"] = "end_service_unavailable"
        elif not report["data_version_unchanged"]:
            report["failure_code"] = "data_changed"
        elif not report["artifacts_unchanged"]:
            report["failure_code"] = "evaluation_artifacts_changed"
        elif not report["index_unchanged"]:
            report["failure_code"] = "index_changed"
        elif not report["implementation_unchanged"]:
            report["failure_code"] = "implementation_changed"
    except (Exception, KeyboardInterrupt) as exc:
        report.update(run_status="failed", failure_code="run_interrupted" if isinstance(exc, KeyboardInterrupt)
                      else "run_exception", error_type=type(exc).__name__)
        for mode in modes:
            for row in report[mode]:
                if row["run_status"] == "running":
                    row["run_status"] = "failed"
                for state in row.get("variants", {}).values():
                    if state["status"] == "running":
                        state.update(status="failed", error_type=type(exc).__name__)
    finally:
        checkpoint()
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"Saved {out.relative_to(ROOT)}")
    if report["run_status"] != "passed":
        print("Selftest failed or incomplete; inspect the saved receipt.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
