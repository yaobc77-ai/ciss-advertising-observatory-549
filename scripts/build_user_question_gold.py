"""Compute gold answers for eval/user_questions and a blank reviewer sheet.

Read-only: independent SQL over current, active, countable native records. No
Observatory query code, model call or database write is used, so the gold does
not inherit the application's interpretation. Outputs are bound to the data
version and written to new files under outputs/.
"""

import csv
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg

from observatory.config import Settings

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "eval" / "user_questions" / "cases.json").read_text(encoding="utf-8"))
BASE = """FROM records r JOIN record_versions v ON v.version_id=r.current_version
 WHERE r.active AND r.dataset='native' AND (v.payload->>'countable')::boolean"""
REVIEW_COLUMNS = [
    "correct", "scope_clear", "gaps_disclosed", "plain_language", "no_overclaim", "usable_next_step",
    "verdict", "reviewer", "notes",
]


def where(spec):
    sql, params = BASE, []
    for field in ("publisher", "sponsor"):
        if spec.get(field):
            sql += f" AND v.payload->>'{field}' = ANY(%s)"
            params.append(spec[field])
    if spec.get("unknown_date"):
        sql += " AND v.payload->>'published_at' IS NULL"
    if spec.get("date_from"):
        sql += " AND (v.payload->>'published_at')::date >= %s"
        params.append(date.fromisoformat(spec["date_from"]))
    if spec.get("date_to"):
        sql += " AND (v.payload->>'published_at')::date <= %s"
        params.append(date.fromisoformat(spec["date_to"]))
    return sql, params


def count(conn, spec):
    sql, params = where(spec)
    return conn.execute("SELECT count(*) " + sql, params).fetchone()[0]


def group(conn, spec):
    sql, params = where(spec)
    key = {
        "publisher": "COALESCE(NULLIF(v.payload->>'publisher',''),'(Unknown)')",
        "sponsor": "COALESCE(NULLIF(v.payload->>'sponsor',''),'(Unknown)')",
        "year": "COALESCE(extract(year FROM (v.payload->>'published_at')::date)::int::text,'(Unknown date)')",
    }[spec["group_by"]]
    rows = conn.execute(f"SELECT {key}, count(*) {sql} GROUP BY 1 ORDER BY 2 DESC, 1", params).fetchall()
    return {name: n for name, n in rows}


def records(conn, spec):
    sql, params = where(spec)
    sql += " AND (v.payload->>'retrievable')::boolean"
    for phrase in ([spec["phrase"]] if spec.get("phrase") else []) + spec.get("all_phrases", []):
        sql += " AND v.body ILIKE %s"
        params.append(f"%{phrase}%")
    if spec.get("any_phrases"):
        sql += " AND (" + " OR ".join("v.body ILIKE %s" for _ in spec["any_phrases"]) + ")"
        params += [f"%{p}%" for p in spec["any_phrases"]]
    if spec.get("pattern"):
        sql += " AND v.body ~* %s"
        params.append(spec["pattern"])
    rows = conn.execute(
        "SELECT r.record_id, v.payload->>'sponsor', v.payload->>'publisher', v.payload->>'title' "
        + sql + " ORDER BY 1", params).fetchall()
    return [{"record_id": a, "sponsor": b, "publisher": c, "title": d} for a, b, c, d in rows]


def gold_for(conn, case):
    spec = case["gold"]
    kind = spec["type"]
    if kind == "count":
        return {"count": count(conn, spec)}
    if kind == "group":
        groups = group(conn, spec)
        return {"groups": groups, "total": sum(groups.values()),
                "top": next(iter(groups), None)}
    if kind == "share":
        numerator, denominator = count(conn, spec["numerator"]), count(conn, spec["denominator"])
        return {"numerator": numerator, "denominator": denominator,
                "percentage": round(100 * numerator / denominator, 1) if denominator else None}
    if kind == "compare":
        return {"counts": [count(conn, item) for item in spec["sets"]]}
    if kind == "records":
        found = records(conn, spec)
        return {"records": found, "record_count": len(found)}
    return {}


def summary(case, gold):
    if "count" in gold:
        return str(gold["count"])
    if "groups" in gold:
        return "; ".join(f"{k}: {v}" for k, v in gold["groups"].items())
    if "percentage" in gold:
        return f"{gold['numerator']} of {gold['denominator']} ({gold['percentage']}%)"
    if "counts" in gold:
        return " | ".join(str(n) for n in gold["counts"])
    if "records" in gold:
        return f"{gold['record_count']} relevant articles, e.g. " + "; ".join(
            f"{r['title'][:50]} ({r['sponsor']})" for r in gold["records"][:3])
    return "Behaviour only - see what the answer must say."


def main():
    settings = Settings.from_env()
    with psycopg.connect(settings.database_url, connect_timeout=10) as conn:
        version = conn.execute(
            "SELECT md5(COALESCE(string_agg(current_version, ',' ORDER BY record_id), '')) FROM records WHERE active"
        ).fetchone()[0]
        problems, results = [], []
        for case in CASES["cases"]:
            gold = gold_for(conn, case)
            if case["gold"]["type"] == "records" and not gold["records"]:
                problems.append(f"{case['id']}: empty records gold")
            if case["gold"]["type"] == "group" and not gold["groups"]:
                problems.append(f"{case['id']}: empty group gold")
            results.append({"id": case["id"], "gold": gold, "summary": summary(case, gold)})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "outputs" / f"user-questions-gold-{stamp}.json"
    sheet = ROOT / "outputs" / f"user-questions-review-{stamp}.csv"
    out.parent.mkdir(exist_ok=True)
    with out.open("x", encoding="utf-8") as stream:
        json.dump({"cases_version": CASES["version"], "data_version": version, "built_at": stamp,
                   "problems": problems, "results": results}, stream, ensure_ascii=False, indent=1)
    by_id = {r["id"]: r for r in results}
    with sheet.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["id", "research_question", "persona", "seen_before", "question", "correct_answer",
                         "answer_must", "system_answer", *REVIEW_COLUMNS])
        for case in CASES["cases"]:
            writer.writerow([case["id"], case["pd"], case["persona"], case["source"] == "client_seen",
                             case["question"], by_id[case["id"]]["summary"], " / ".join(case["must_say"]),
                             "", *[""] * len(REVIEW_COLUMNS)])
    print(json.dumps({"data_version": version, "cases": len(results), "problems": problems,
                      "gold": str(out.relative_to(ROOT)), "review_sheet": str(sheet.relative_to(ROOT))},
                     ensure_ascii=False, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
