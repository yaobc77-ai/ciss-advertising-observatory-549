"""Likely publication dates for records whose source date is missing.

Source dates are never edited. Each inference is stored beside the immutable
record version with an evidence tier, never as a probability:

  A  url_path              full date in the article URL; the publisher's
                           URL-vs-source agreement on dated records is recorded
  B  archive_first_capture earliest archive capture; only an upper bound
  C  web_search            a dated mention found by hosted web search

Supplemented dates are used without a manual review step (a row someone
explicitly rejects is ignored). Questions use them by default; counts, records
and citations always say which dates were supplemented.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal

from psycopg.types.json import Jsonb

from .budget import LimitReached

URL_DATE = re.compile(r"/((?:19|20)\d{2})/(\d{2})/(\d{2})/")
METHOD_TIER = {"url_path": "A", "archive_first_capture": "B", "web_search": "C"}
BASIS_NOTE = ("Some dates are supplemented, not taken from the source data: tier A from a date in "
              "the article URL, tier C from a web search.")
UNCHECKED_NOTE = "The date basis of the cited records could not be checked; treat their dates with caution."
CURRENT = """FROM records r JOIN record_versions v ON v.version_id=r.current_version
 WHERE r.active AND (v.payload->>'countable')::boolean"""


def same_site(left: str | None, right: str | None) -> bool:
    """True when two URLs share a host (ignoring a leading www.)."""
    from urllib.parse import urlsplit

    def host(url):
        name = (urlsplit(url or "").hostname or "").lower()
        return name[4:] if name.startswith("www.") else name
    return bool(host(left)) and host(left) == host(right)


def url_date(url: str | None) -> date | None:
    match = URL_DATE.search(url or "")
    if not match:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def inference_id(version_id: str, method: str) -> str:
    return hashlib.sha256(f"{version_id}:{method}".encode()).hexdigest()[:32]


def publisher_agreement(conn) -> dict[str, dict]:
    """How often the URL date equals the source date, per publisher."""
    stats = defaultdict(lambda: {"compared": 0, "exact": 0, "within_3_days": 0})
    for row in conn.execute(
            "SELECT v.payload->>'publisher' AS publisher, v.payload->>'url' AS url, "
            "v.payload->>'published_at' AS published " + CURRENT +
            " AND NULLIF(v.payload->>'published_at','') IS NOT NULL").fetchall():
        found = url_date(row["url"])
        if not found:
            continue
        gap = abs((found - date.fromisoformat(row["published"][:10])).days)
        item = stats[row["publisher"] or "(Unknown)"]
        item["compared"] += 1
        item["exact"] += gap == 0
        item["within_3_days"] += gap <= 3
    return dict(stats)


def undated_records(conn) -> list[dict]:
    return conn.execute(
        "SELECT r.record_id, v.version_id, v.payload->>'publisher' AS publisher, "
        "v.payload->>'sponsor' AS sponsor, v.payload->>'title' AS title, v.payload->>'url' AS url "
        + CURRENT + " AND NULLIF(v.payload->>'published_at','') IS NULL ORDER BY r.record_id").fetchall()


def propose_url_inferences(conn) -> list[dict]:
    agreement = publisher_agreement(conn)
    proposals = []
    for row in undated_records(conn):
        found = url_date(row["url"])
        if not found:
            continue
        match = URL_DATE.search(row["url"])
        proposals.append({
            "inference_id": inference_id(row["version_id"], "url_path"),
            "record_id": row["record_id"], "version_id": row["version_id"],
            "method": "url_path", "tier": "A", "inferred_date": found,
            "earliest": None, "latest": None, "precision": "day",
            "evidence": {"url": row["url"], "matched": match.group(0),
                         "publisher": row["publisher"],
                         "publisher_url_agreement": agreement.get(row["publisher"] or "(Unknown)"),
                         "derived_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        })
    return proposals


def apply_inferences(conn, proposals: list[dict]) -> int:
    """Insert new inferences; an existing row for the same version and method is kept."""
    inserted = 0
    for item in proposals:
        cursor = conn.execute(
            "INSERT INTO date_inferences(inference_id,record_id,version_id,method,tier,inferred_date,"
            "earliest,latest,precision,evidence) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (item["inference_id"], item["record_id"], item["version_id"], item["method"], item["tier"],
             item["inferred_date"], item["earliest"], item["latest"], item["precision"],
             Jsonb(item["evidence"])))
        inserted += cursor.rowcount
    return inserted


# ---------------------------------------------------------------- web search (tier C)

SEARCH_MODEL = "gpt-5.6-luna"
SEARCH_RESERVATION_USD = Decimal("0.05")
SEARCH_PROMPT = """Find when this sponsored article was first published online.
Use web search. Report only what a search result states; do not guess.
Return JSON only: {"date": "YYYY-MM-DD" or null, "earliest": "YYYY-MM-DD" or null,
"latest": "YYYY-MM-DD" or null, "precision": "day"|"month"|"year"|"range",
"evidence_url": "<a URL from your search results>", "quote": "<short exact text stating the date>"}.
Use "range" with earliest/latest when only bounds are known. If nothing states a date,
return {"date": null, "earliest": null, "latest": null, "precision": "range", "evidence_url": null, "quote": null}."""


def _sources(response) -> set[str]:
    urls = set()
    for item in getattr(response, "output", None) or []:
        action = getattr(item, "action", None)
        for source in getattr(action, "sources", None) or []:
            url = getattr(source, "url", None) or (source.get("url") if isinstance(source, dict) else None)
            if url:
                urls.add(url)
        for part in getattr(item, "content", None) or []:
            for note in getattr(part, "annotations", None) or []:
                url = getattr(note, "url", None)
                if url:
                    urls.add(url)
    return urls


def web_search_inference(rag, record: dict, visitor: str = "date-inference-maintenance") -> dict:
    """One bounded, budgeted web search for one undated record (tier C, unreviewed).

    The evidence URL must be one of the provider's own search sources, so the
    model cannot invent a citation.
    """
    from .claims_source_search import source_search_price

    try:
        reservation = rag.budget.reserve(SEARCH_RESERVATION_USD, visitor, "generation", SEARCH_MODEL)
    except LimitReached:
        return {"status": "limited"}
    query = (f"Title: {record['title']}\nPublisher: {record['publisher']}\n"
             f"Sponsor: {record['sponsor']}\nURL: {record['url']}")
    try:
        response = rag.client.responses.create(
            model=SEARCH_MODEL, input=[{"role": "system", "content": SEARCH_PROMPT},
                                       {"role": "user", "content": query}],
            tools=[{"type": "web_search", "search_context_size": "low"}], max_tool_calls=2,
            include=["web_search_call.action.sources"], max_output_tokens=800, store=False)
        usage = response.usage
        cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
        searches = sum(getattr(item, "type", None) == "web_search_call" for item in response.output or [])
        cost = source_search_price(usage.input_tokens, usage.output_tokens, cached, 0, searches)
        rag.budget.settle(reservation, cost, {**usage.model_dump(), "web_search_calls": searches})
    except Exception:
        rag.budget.uncertain(reservation, "web_search_failed")
        raise
    text = getattr(response, "output_text", "") or ""
    match = re.search(r"\{.*\}", text, re.S)
    sources = _sources(response)
    result = {"status": "no_date", "cost_usd": float(cost), "sources": sorted(sources)}
    if not match:
        return result
    try:
        found = json.loads(match.group(0))
    except json.JSONDecodeError:
        return result
    evidence_url = found.get("evidence_url")
    if not evidence_url or evidence_url not in sources or not (found.get("date") or found.get("earliest") or found.get("latest")):
        return {**result, "rejected": "date without a provider-sourced evidence URL" if found.get("date") else None}

    def as_date(value):
        try:
            return date.fromisoformat(value) if value else None
        except ValueError:
            return None

    point, earliest, latest = as_date(found.get("date")), as_date(found.get("earliest")), as_date(found.get("latest"))
    precision = found.get("precision") if found.get("precision") in {"day", "month", "year", "range"} else "range"
    if precision == "day" and not point:
        precision = "range"
    return {**result, "status": "found", "proposal": {
        "inference_id": inference_id(record["version_id"], "web_search"),
        "record_id": record["record_id"], "version_id": record["version_id"],
        "method": "web_search", "tier": "C", "inferred_date": point,
        "earliest": earliest, "latest": latest, "precision": precision,
        "evidence": {"evidence_url": evidence_url, "quote": (found.get("quote") or "")[:300],
                     "host_match": same_site(evidence_url, record["url"]),
                     "query": query, "provider_sources": sorted(sources)[:10], "model": SEARCH_MODEL,
                     "searched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "cost_usd": float(cost)}}}
