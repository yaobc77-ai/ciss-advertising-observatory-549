"""Shared filtered-record service for the UI, export and evidence queries."""

import logging
import re
import time
from collections import Counter
from copy import deepcopy
from urllib.parse import urlsplit

from .budget import LimitReached, price
from .db import Database
from .models import Answer, Filters
from .rag import Rag
from .structured_queries import QuestionPlan, plan_question, validate_share_scope

log = logging.getLogger(__name__)


def _progress(callback, stage):
    # A disconnected browser must never interrupt accounting or source checks.
    if callback:
        try:
            callback(stage)
        except Exception:
            log.debug("Progress listener unavailable")


def safe_url(value):
    try:
        p = urlsplit(value or "")
        return (
            value
            if p.scheme in ("http", "https")
            and p.hostname
            and not p.username
            and not p.password
            else ""
        )
    except ValueError:
        return ""


def summarize(rows):
    total = len(rows)

    def group(field):
        counts = Counter(r.get(field) or "(Unknown)" for r in rows)
        return [
            {"name": k, "count": v, "percent": 100 * v / total if total else 0}
            for k, v in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
        ]

    timeline = Counter((r["date"][:7] if r.get("date") else "Unknown") for r in rows)
    inferred = Counter(r.get("inferred_tier") for r in rows
                       if str(r.get("date_basis") or "").startswith("inferred:"))
    relationships = Counter(
        (r.get("sponsor") or "(Unknown)", r.get("publisher") or "(Unknown)")
        for r in rows
    )
    return {
        "total": total,
        "retrievable": sum(r["retrievable"] for r in rows),
        "unknown_dates": sum(not r.get("date") for r in rows),
        **{
            f: group(f[:-1] if f != "keywords" else "keyword")
            for f in ["publishers", "sponsors", "platforms", "keywords"]
        },
        "timeline": [{"month": k, "count": v} for k, v in sorted(timeline.items())],
        "relationships": [
            {"sponsor": s, "publisher": p, "count": n}
            for (s, p), n in sorted(relationships.items(), key=lambda x: (-x[1], x[0]))
        ],
        "inferred_dates": dict(sorted(inferred.items())),
    }


WEB_SUPPLEMENT_LEAD = ("The advertising collection does not contain enough to answer this question, so the "
                      "answer below is supplemented from web sources. It is not from the advertising "
                      "database: each paragraph cites the web page it relies on.")


def _date_inference_note(used: bool, tiers: dict) -> str:
    """Server-written label; the model can neither omit nor reword it."""
    total = sum(tiers.values())
    if not total:
        return ""
    labels = {"A": "a date in the article URL", "B": "an archive capture date", "C": "a web search"}
    detail = "; ".join(f"{n} from {labels.get(tier, tier)} (tier {tier})" for tier, n in sorted(tiers.items()))
    if used:
        return (f" {total:,} of these records have no publication date in the source data; their dates "
                f"are supplemented ({detail}).")
    return (f" {total:,} of these records have no publication date in the source data but have a "
            f"supplemented date ({detail}) that was not used here.")


def _plain_scope(scope, base):
    """Describe how a comparison group narrows the trusted selection, in plain words."""
    from .analytics import sponsor_display

    parts = []
    if scope.sponsors and scope.sponsors != base.sponsors:
        parts.append("ads sponsored by " + ", ".join(sponsor_display(s) for s in scope.sponsors))
    if scope.publishers and scope.publishers != base.publishers:
        parts.append("ads in " + ", ".join(scope.publishers))
    if (scope.date_from, scope.date_to) != (base.date_from, base.date_to):
        parts.append(f"ads dated {scope.date_from or 'any time'} to {scope.date_to or 'today'}")
    if scope.date_presence != base.date_presence and scope.date_presence != "any":
        parts.append("ads " + ("with" if scope.date_presence == "known" else "without") + " a publication date")
    return " and ".join(parts) or "the current selection"


class Service:
    def __init__(self, settings, db=None, rag=None, research_agent=None, claims_store=None, web_research=None):
        self.settings = settings
        self.db = db or Database(settings.database_url)
        self.rag = rag or Rag(self.db, settings)
        self.research_agent = research_agent
        self.claims_store = claims_store
        self.web_research = web_research

    def claims_matches(self, filters: Filters, *, nc_ids=None, sc_ids=None, taxonomy=None,
                       review_state=None, offset=0, limit=20):
        """Read published NC/SC assignments, independently of historical labels.

        The store rechecks current source versions and exact quote positions in
        one read-only snapshot. This adapter applies the public link policy and
        preserves its positive-only coverage; no classification is run here.
        """
        from .claims_store import ClaimsStore

        store = self.claims_store if self.claims_store is not None else ClaimsStore(self.db)
        try:
            result = deepcopy(store.matches(filters, nc_ids=nc_ids, sc_ids=sc_ids,
                                            taxonomy=taxonomy, review_state=review_state,
                                            offset=offset, limit=limit))
        except ValueError:
            raise
        except Exception:
            # Driver diagnostics may include credentials or source paths.
            result = {"available": False, "claims_version": None, "total_records": 0,
                      "total_matches": 0, "records": [], "offset": offset, "limit": limit}
        result.setdefault("meaning", "Published taxonomy assignments; not independent fact checking.")
        result.setdefault("coverage", "Records with published matches only; unmatched records are not classified negatives.")
        result["filters"] = filters.model_dump(mode="json")
        result["status"] = "ok" if result.get("available") else "unavailable"
        result["model_calls"] = 0
        result["claims_status"] = "published_assignments_only"
        if not result.get("available"):
            result["message"] = "Published CLAIMS2 results are unavailable. Unmatched records are not classified negatives."
        refs = []
        for record in result.get("records", []):
            claims = record.get("claims", [])
            for claim in claims:
                for field in ("url", "archive_url"):
                    claim[field] = safe_url(claim.get(field)) if self.settings.show_source_links else ""
                refs.append({key: claim.get(key) for key in (
                    "candidate_key", "record_id", "version_id", "body_hash", "dataset", "start", "end",
                    "taxonomy_version", "review_version", "run_id",
                )})
            if claims:
                record.update({key: claims[0].get(key) for key in (
                    "title", "publisher", "sponsor", "dataset", "date", "url", "archive_url",
                )})
        result["source_refs"] = refs
        return result

    def browse(self, filters):
        return self._public_rows(self.db.public_rows(filters))

    def _public_rows(self, rows):
        for row in rows:
            for key in ["url", "archive_url"]:
                row[key] = (
                    safe_url(row.get(key)) if self.settings.show_source_links else ""
                )
        return rows

    def page(self, filters, offset=0, limit=20, sort_by="date", descending=True):
        result = self.db.public_page(filters, offset, limit, sort_by, descending)
        result["rows"] = self._public_rows(result["rows"])
        return result

    def dashboard(self, filters, offset=0, limit=20, sort_by="date", descending=True):
        result = self.db.dashboard(filters, offset, limit, sort_by, descending)
        result["page"]["rows"] = self._public_rows(result["page"]["rows"])
        return result

    def network(self, filters, limit=60):
        return self.db.network(filters, limit)

    def knowledge_graph(self, filters, offset=0, limit=5, record_details=None):
        """A typed, provenance-bearing graph derived from current stored versions.

        No model inference or writes occur here. Entity IDs represent exact
        source identities; aliases need a separately reviewed resolution policy.
        """
        from .knowledge_graph import build_graph

        page = self.db.knowledge_page(filters, offset=offset, limit=limit)
        attachments = {}
        if record_details is not None and self.settings.show_source_links:
            for row in page["rows"]:
                detail = record_details.get(row["record_id"])
                # Attachments are optional and independently read. Do not join
                # a newly published version to the graph's earlier snapshot.
                if detail and detail.get("version_id") == row["version_id"]:
                    attachments[row["record_id"]] = detail.get("attachments", [])
        graph = build_graph(page["rows"], links_enabled=self.settings.show_source_links,
                            attachments_by_record=attachments, claims2=page.get("claims2"))
        graph["coverage"] = {
            "total_records": page["total"], "shown_records": len(page["rows"]),
            "offset": page["offset"], "limit": page["limit"],
        }
        graph["filters"] = filters.model_dump(mode="json")
        public_fields = ("record_id", "version_id", "dataset", "title", "date",
                         "sponsor", "publisher", "retrievable", "url", "archive_url")
        graph["records"] = self._public_rows([
            {key: row.get(key) for key in public_fields} for row in page["rows"]
        ])
        return graph

    def statistics(self, filters):
        return self.db.dashboard(filters)["stats"]

    def facets(self, dataset):
        return self.db.facets(dataset)

    def knowledge_map(self, filters):
        """Full selection overview with record-backed derived associations."""
        from .knowledge_map import build_collection_map

        if filters.dataset != "native":
            raise ValueError("The collection knowledge graph supports native records")
        rows = self.db.knowledge_map_rows(filters)
        graph = build_collection_map(rows, links_enabled=self.settings.show_source_links)
        graph["filters"] = filters.model_dump(mode="json")
        return graph

    def health(self):
        try:
            return self.db.health()
        except Exception:
            return {
                "status": "unavailable",
                "record_counts": {},
                "data_version": "unavailable",
            }

    def _public_evidence(self, evidence):
        return [
            e.model_copy(
                update={
                    "url": safe_url(e.url) if self.settings.show_source_links else "",
                    "archive_url": safe_url(e.archive_url)
                    if self.settings.show_source_links
                    else "",
                }
            )
            for e in evidence
        ]

    def search(self, question, filters, limit=5):
        if not 1 <= len(question.strip()) <= 2000:
            return []
        filters = self._title_scope(question, filters)
        return self._public_evidence(
            self.db.search(question, filters, limit=min(limit, 10))
        )

    def search_report(self, question, filters, limit=5):
        """Free search with keyword coverage of the same filtered index snapshot."""
        if not 1 <= len(question.strip()) <= 2000:
            return {
                "evidence": [],
                "diagnostics": {
                    "status": "unavailable", "operator": "OR", "terms": [],
                    "reason": "Enter a question of 1–2,000 characters.",
                },
            }
        filters = self._title_scope(question, filters)
        report = self.db.search_report(question, filters, limit=min(limit, 10))
        report["evidence"] = self._public_evidence(report["evidence"])
        return report

    def _title_scope(self, question, filters):
        """A full explicitly named title narrows retrieval within existing filters."""

        def normal(text):
            return " ".join(re.findall(r"\w+", text.lower()))

        query = normal(question)
        rows = self.db.public_rows(filters)
        matches = [
            r["record_id"]
            for r in rows
            if len(normal(r["title"])) >= 20 and normal(r["title"]) in query
        ]
        return (
            filters.model_copy(update={"record_ids": matches}) if matches else filters
        )

    def _statistics_answer(self, plan):
        """SQL totals and examples share a read-only snapshot per collection.

        Every eligible record contributes, including records without searchable
        text. Native articles and social posts retain separate counting units.
        Only public fields are returned; source categories are never merged.
        """
        from .analytics import sponsor_display

        if plan.kind == "share":
            return self._share_statistics_answer(plan)
        filters = plan.filters
        datasets = ["native", "social"] if filters.dataset == "all" else [filters.dataset]
        collections, groups, records, tiers = [], [], [], {}
        for dataset in datasets:
            snapshot = self.dashboard(filters.model_copy(update={"dataset": dataset}), limit=10)
            stats = snapshot["stats"]
            for tier, n in (stats.get("inferred_dates") or {}).items():
                tiers[tier] = tiers.get(tier, 0) + n
            collections.append({
                "dataset": dataset, "total": stats["total"],
                "retrievable": stats["retrievable"], "unknown_dates": stats["unknown_dates"],
            })
            if plan.group_by:
                for group in stats[plan.group_by]:
                    groups.append({
                        "dataset": dataset, "name": group["name"], "count": group["count"],
                        "display_name": sponsor_display(group["name"])
                        if plan.group_by == "sponsors" else group["name"],
                    })
            fields = ("record_id", "version_id", "dataset", "title", "date", "date_basis",
                      "inferred_date", "inferred_tier", "publisher", "sponsor", "url", "archive_url", "retrievable")
            records.extend({field: row.get(field) for field in fields} for row in snapshot["page"]["rows"])
        totals = "; ".join(
            f"{item['total']:,} eligible {'native ad records' if item['dataset'] == 'native' else 'social ad records'}"
            for item in collections
        )
        if plan.kind == "count":
            text = f"{totals} match this question and the active filters."
        else:
            dimension = "publishers" if plan.group_by == "publishers" else "source-listed sponsors / organizations"
            names = {group["name"] for group in groups if group["name"] != "(Unknown)"}
            text = f"{len(names):,} {dimension} appear in {totals} within this selection. Every category and its count is listed below."
        if not any(item["total"] for item in collections):
            text += " No eligible records match; this does not establish that no such advertisements exist elsewhere."
        note = _date_inference_note(filters.include_inferred_dates, tiers)
        if filters.date_presence == "missing" and filters.include_inferred_dates:
            # "Without a date" after supplementing differs from the source gap; say both.
            source_missing = sum(
                self.dashboard(filters.model_copy(update={"dataset": dataset, "include_inferred_dates": False}),
                               limit=1)["stats"]["total"] for dataset in datasets)
            remaining = sum(item["total"] for item in collections)
            if source_missing > remaining:
                note += (f" {source_missing:,} records have no publication date in the source data; "
                         f"{source_missing - remaining:,} of them have a supplemented date, so {remaining:,} "
                         "remain without any date.")
        return Answer(
            status="answered", answer=text + note, answer_mode="statistics",
            structured_result={
                "kind": plan.kind, "method": "database", "group_by": plan.group_by,
                "filters": filters.model_dump(mode="json"), "collections": collections,
                "groups": groups, "records": records, "scope_notes": list(plan.scope_notes),
                "date_inference": {"used": filters.include_inferred_dates, "tiers": tiers, "note": note.strip()},
            },
        )

    def _cites_inferred_dates(self, result, filters):
        ids = sorted({item.record_id for item in result.evidence or []})
        if not ids:
            return False
        try:
            rows = self.db.public_rows(filters.model_copy(update={"record_ids": ids}))
        except Exception:
            return None  # Unknown: keep the answer and add a conservative notice.
        return any(str(row.get("date_basis") or "").startswith("inferred:") for row in rows)

    def _share_statistics_answer(self, plan):
        """Compute numerator and trusted denominator in one snapshot per dataset.

        Reuse the database's complete eligibility/filter query rather than
        reproducing its annotation or unknown-date rules in Python. The caller's
        scope is bound before any target filters, and no model supplies a count.
        """
        numerator, denominator = plan.filters, plan.denominator_filters
        if not isinstance(numerator, Filters) or not isinstance(denominator, Filters) or plan.group_by:
            raise ValueError("A percentage needs a trusted denominator and no grouping")
        validate_share_scope(numerator, denominator)
        datasets = ("native", "social") if numerator.dataset == "all" else (numerator.dataset,)
        collections, records, share_tiers, denominator_inferred = [], [], {}, 0
        health = self.health()
        if health.get("status") != "ok":
            raise ValueError("Percentage statistics require a loaded collection")
        missing = [dataset for dataset in datasets if not health.get("record_counts", {}).get(dataset)]
        fields = ("record_id", "version_id", "dataset", "title", "date", "publisher",
                  "sponsor", "url", "archive_url", "retrievable")
        for dataset in datasets:
            if dataset in missing:
                continue
            base_query, base_params = self.db._public_query(denominator.model_copy(update={"dataset": dataset}))
            target_query, target_params = self.db._public_query(numerator.model_copy(update={"dataset": dataset}))
            # Identical record/version membership also enforces the subset at
            # the SQL boundary; both queries use the same MVCC snapshot.
            prefix = f"WITH denominator AS ({base_query}), target AS ({target_query}), numerator AS (SELECT t.* FROM target t JOIN denominator d USING(record_id,version_id)) "
            params = [*base_params, *target_params]
            with self.db.connect() as conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                counts = conn.execute(prefix + """SELECT count(*) AS numerator,
                    count(*) FILTER (WHERE date_basis LIKE 'inferred:%%' AND inferred_tier='A') AS inferred_a,
                    count(*) FILTER (WHERE date_basis LIKE 'inferred:%%' AND inferred_tier='B') AS inferred_b,
                    count(*) FILTER (WHERE date_basis LIKE 'inferred:%%' AND inferred_tier='C') AS inferred_c,
                    count(*) FILTER (WHERE retrievable) AS retrievable,
                    (SELECT count(*) FROM denominator WHERE date_basis LIKE 'inferred:%%') AS denominator_inferred,
                    count(*) FILTER (WHERE date IS NULL OR date='') AS unknown_dates,
                    (SELECT count(*) FROM denominator) AS denominator FROM numerator""", params).fetchone()
                rows = conn.execute(prefix + "SELECT * FROM numerator ORDER BY date DESC NULLS LAST,record_id LIMIT 10", params).fetchall()
            n, d = counts["numerator"], counts["denominator"]
            denominator_inferred += counts.get("denominator_inferred") or 0
            for tier in "ABC":
                if counts.get(f"inferred_{tier.lower()}"):
                    share_tiers[tier] = share_tiers.get(tier, 0) + counts[f"inferred_{tier.lower()}"]
            collections.append({
                "dataset": dataset, "total": n, "retrievable": counts["retrievable"],
                "unknown_dates": counts["unknown_dates"], "numerator": n, "denominator": d,
                "percentage": 100 * n / d if d else None,
                "percentage_status": "defined" if d else "empty_selection",
            })
            records.extend({field: row.get(field) for field in fields} for row in self._public_rows(rows))
        basis = getattr(plan, "denominator_basis", None) or "current_selection_before_question_targets"
        note = ("Each percentage uses the comparison group named in the question, within the current selection."
                if basis == "question_comparison_group" else
                "Each percentage uses its collection's eligible records in the current selection before question targets.")
        data = {
            "kind": "share", "method": "database", "group_by": None,
            "filters": numerator.model_dump(mode="json"),
            "denominator_filters": denominator.model_dump(mode="json"),
            "denominator_basis": basis,
            "collections": collections, "groups": [], "records": records,
            "scope_notes": [*plan.scope_notes,
                note + " Unsearchable records still count; a zero denominator is undefined."],
        }
        if missing:
            data["scope_notes"].append("Unloaded collections are omitted, not reported as zero advertisements or zero percent.")
        used = numerator.include_inferred_dates or denominator.include_inferred_dates
        note = _date_inference_note(used, share_tiers).strip()
        if used and denominator_inferred:
            note = (note + " " if note else "") + (f"The comparison group includes {denominator_inferred:,} "
                                                   "records dated by an unreviewed estimate.")
        data["date_inference"] = {"used": used, "tiers": share_tiers,
                                  "denominator_inferred": denominator_inferred, "note": note}
        trusted = getattr(plan, "trusted_filters", None) or denominator
        return self._tool_statistics_answer(data, base_filters=trusted)

    def answer(self, question, filters, visitor, progress=None):
        start = time.monotonic()
        # A default must never override a caller's explicit source-only choice.
        if "include_inferred_dates" not in filters.model_fields_set:
            filters = filters.model_copy(update={"include_inferred_dates": True})
        if getattr(self.settings, "research_agent_enabled", False):
            result = self._answer_with_tools(question, filters, visitor, progress=progress)
        else:
            result = self._answer_legacy(question, filters, visitor, progress=progress)
        result = self._ensure_answer(question, filters, visitor, result, progress)
        self._record_web_trace(result, question, filters)
        result.latency_ms = int((time.monotonic() - start) * 1000)
        if result.external_research or result.research_trace.get("external_web"):
            self._save_supplement(question, filters, result)
        return result

    def _ensure_answer(self, question, filters, visitor, result, progress=None):
        """Always give an answer: when the collection cannot, supplement from the web.

        Supplemented content is labelled as not coming from the advertising
        database and keeps the provider's source links. Budget and rate limits
        still apply, and a failed web search never invents an answer.
        """
        protected = {"citation_mismatch", "quote_too_long", "invalid_claim_count",
                     "evidence_version_mismatch", "answer_structure_mismatch"}
        if (result.status in ("answered", "limited") or not 1 <= len(question.strip()) <= 2000
                or result.failure_reason.startswith("data_changed") or result.failure_reason in protected):
            return result
        research = result.external_research or {}
        # Any prior outcome is a completed attempt, including failures and limits.
        # The evidence route may already have searched: do not charge it again.
        if not research:
            before = self.health()
            attempt = self._search_external(question, filters, visitor, progress=progress)
            if attempt.get("status") == "disabled" or attempt.get("reason") == "scope_not_supported":
                return result  # No lookup happened; the collection answer stands unchanged.
            research = attempt
            result.cost_usd += research.get("cost_usd", 0.0)
            result.external_research = research
            after = self.health()
            version = before.get("data_version")
            if before.get("status") == "ok" and version and version != "unavailable":
                result.research_trace["data_version"] = version
                if after.get("status") != "ok" or after.get("data_version") != version:
                    return self._withhold_changed_web_result(result)
        if research.get("status") != "ok":
            return result
        sources = [s for s in research.get("sources") or []
                   if isinstance(s, dict) and s.get("url") and s.get("supports_generated_paragraph", True)]
        if not sources or not str(research.get("summary") or "").strip():
            result.external_research = {**research, "status": "unresolved", "reason": "no_cited_answer"}
            return result
        reason = (result.answer or "").strip()
        result.status = "answered"
        result.answer_mode = "web_supplement"
        # API, MCP and exports receive the findings and links, not only UI metadata.
        result.answer = WEB_SUPPLEMENT_LEAD + "\n\n" + (research.get("summary") or "").strip()
        if sources:
            result.answer += "\n\nWeb sources:\n" + "\n".join(
                f"[{s.get('source_id')}] {s.get('title') or s['url']} - {s['url']}" for s in sources)
        if reason:
            result.answer += "\n\nWhy the collection could not answer: " + reason
        return result

    @staticmethod
    def _withhold_changed_web_result(result):
        external = result.external_research or {}
        trace = deepcopy(result.research_trace)
        trace["external_web"] = {key: external.get(key) for key in (
            "source_kind", "cost_usd", "model_calls", "searched_at", "audit",
        ) if key in external}
        trace["external_web"].update(status="withheld", reason="data_changed_during_web_research")
        return Answer(status="service_unavailable",
                      answer="The collection changed during research. Please submit the question again.",
                      failure_reason="data_changed_during_web_research", cost_usd=result.cost_usd,
                      research_trace=trace)

    @staticmethod
    def _record_web_trace(result, question, filters):
        external = result.external_research or result.research_trace.get("external_web")
        if not external or external.get("status") == "disabled" or external.get("reason") == "scope_not_supported":
            return
        trace = deepcopy(result.research_trace)
        trace.setdefault("original_question", question)
        trace.setdefault("base_filters", filters.model_dump(mode="json"))
        trace.setdefault("model_calls", [])
        trace["cost_usd"] = result.cost_usd
        trace["external_web"] = {key: external.get(key) for key in (
            "status", "reason", "source_kind", "cost_usd", "model_calls", "searched_at", "audit",
        ) if key in external}
        # The lower evidence path may already have recorded the same lookup.
        trace["tools"] = [step for step in trace.get("tools", [])
                          if (step.get("tool") or step.get("name")) != "search_external_sources"]
        trace["tools"].append({
            "tool": "search_external_sources", "status": external.get("status"),
            "source_kind": "external_web", "cost_usd": external.get("cost_usd", 0),
            "data_refs": [{"source_id": s.get("source_id"), "url": s.get("url")}
                          for s in external.get("sources", [])],
        })
        result.research_trace = trace

    def _save_supplement(self, question, filters, result):
        try:
            version = result.research_trace.get("data_version") or self.health().get("data_version", "unavailable")
            self.db.save_answer(question, filters, result, version)
        except Exception:
            log.warning("Supplemented answer audit log unavailable")

    def _answer_with_tools(self, question, filters, visitor, progress=None):
        """Understand the question before accessing constrained collection tools."""
        from .research_agent import ResearchAgent
        from .research_tools import ToolCatalog

        start = time.monotonic()
        if not 1 <= len(question.strip()) <= 2000:
            return Answer(status="insufficient_evidence", answer_mode="clarification",
                          answer="Please enter a question of 1–2,000 characters.")
        run = None
        downstream_cost = 0.0
        version = "unavailable"
        try:
            _progress(progress, "database")
            before = self.health()
            candidate_version = before.get("data_version")
            if before.get("status") != "ok" or not isinstance(candidate_version, str) or not candidate_version or candidate_version == "unavailable":
                raise ValueError("A healthy source version is required for question tools")
            version = candidate_version
            catalog = ToolCatalog(self, filters)
            agent = self.research_agent or ResearchAgent(self.rag, catalog)
            _progress(progress, "interpreting")
            run = agent.run(question, filters, visitor,
                            **({"progress": lambda stage: _progress(progress, stage)} if progress else {}))
            data = run.result
            if run.route == "statistics":
                result = self._tool_statistics_answer(data, base_filters=filters)
            elif run.route == "evidence":
                narrowed = Filters.model_validate(data["filters"])
                validate_share_scope(narrowed, filters)
                # Question language and wording remain intact for the cited answer.
                result = self._answer_evidence(question, narrowed, visitor,
                                               search_query=data.get("search_query") or data.get("query") or question, audit=False,
                                               **({"retrieval_groups": data["retrieval_groups"]} if data.get("retrieval_groups") else {}),
                                               **({"progress": progress} if progress else {}))
            elif run.route in {"graph", "sources", "record"}:
                result = Answer(
                    status="answered", answer_mode="tools",
                    answer="Source records and relationships from the current collection are shown below.",
                    structured_result={"kind": run.route, **data},
                )
            elif run.route == "claims":
                result = self._tool_claims_answer(data)
            elif run.route == "clarify":
                result = Answer(status="insufficient_evidence", answer_mode="clarification",
                                answer=data.get("message") or "Please clarify the collection, entity or date range.")
            elif run.route == "limited":
                # A rate or budget limit is expected behaviour, not an outage.
                result = Answer(
                    status="limited", answer_mode="tools", failure_reason=run.failure_reason,
                    answer="Your question limit or the API budget has been reached. Browsing and keyword search remain available; please try again later.",
                )
            else:
                result = Answer(
                    status="service_unavailable", answer_mode="tools",
                    failure_reason=run.failure_reason,
                    answer="Question understanding is temporarily unavailable. Browse the collection or use keyword search.",
                )
            downstream_cost = result.cost_usd
            after = self.health()
            if after.get("status") != "ok" or after.get("data_version") != version:
                result = Answer(status="service_unavailable", answer_mode="tools",
                                failure_reason="data_changed_during_tool_research",
                                answer="The collection changed while processing this question. Please submit it again.",
                                cost_usd=result.cost_usd, research_trace=result.research_trace)
            elif result.answer_mode == "statistics" and result.structured_result is not None:
                # Bind later record pagination to the same source snapshot that
                # passed the query's before/after guard, not a model-supplied ID.
                result.structured_result = {**result.structured_result, "data_version": version}
            result.cost_usd += run.cost_usd
            withheld_web = result.research_trace.get("external_web")
            result.research_trace = run.audit()
            result.research_trace["data_version"] = version
            if withheld_web:
                result.research_trace["external_web"] = withheld_web
            if result.external_research:
                external = result.external_research
                result.research_trace["external_web"] = {key: external.get(key) for key in (
                    "status", "source_kind", "cost_usd", "model_calls", "searched_at", "audit",
                ) if key in external}
                result.research_trace["tools"].append({
                    "tool": "search_external_sources", "status": external.get("status"),
                    "source_kind": "external_web", "cost_usd": external.get("cost_usd", 0),
                    "data_refs": [{"source_id": s.get("source_id"), "url": s.get("url")}
                                  for s in external.get("sources", [])],
                })
        except Exception as exc:
            log.warning("Question understanding failed: %s", type(exc).__name__)
            result = Answer(status="service_unavailable", answer_mode="tools",
                            answer="Question understanding is temporarily unavailable. Browse the collection or use keyword search.",
                            failure_reason="research_agent_unavailable",
                            cost_usd=downstream_cost + (run.cost_usd if run else 0))
            if run:
                result.research_trace = run.audit()
        result.latency_ms = int((time.monotonic() - start) * 1000)
        try:
            self.db.save_answer(question, filters, result, version)
        except Exception:
            log.warning("Tool research audit log unavailable")
        return result

    @staticmethod
    def _tool_claims_answer(data):
        """Present stored assignments and exact quotes without prose generation."""
        if data.get("status") != "ok" or not data.get("available"):
            return Answer(status="service_unavailable", answer_mode="tools",
                          answer="Published CLAIMS2 results are unavailable; this does not establish that advertisements contain no claims.",
                          failure_reason="claims_results_unavailable")
        total = data["total_records"]
        matches = data["total_matches"]
        message = (f"{total:,} stored records have {matches:,} published CLAIMS2 assignments within this selection. "
                   "Definitions, review states and original evidence are shown below. "
                   "These assignments are not independent fact checking or verified greenwashing findings. "
                   "Unmatched records are not classified negatives.")
        return Answer(status="answered", answer_mode="tools", answer=message,
                      structured_result={"kind": "claims", **data})

    @staticmethod
    def _tool_statistics_answer(data, *, base_filters=None):
        """Publish program-computed counts; model-written totals are never used."""
        if data.get("kind") in {"list_years", "top_years", "compare_periods"}:
            return Service._time_statistics_answer(data, base_filters=base_filters)
        if data.get("kind") == "share":
            numerator = Filters.model_validate(data["filters"])
            denominator = Filters.model_validate(data["denominator_filters"])
            basis = data.get("denominator_basis")
            if (not isinstance(base_filters, Filters)
                    or basis not in {"current_selection_before_question_targets", "question_comparison_group"}
                    or data.get("method") != "database" or data.get("group_by") is not None
                    or data.get("groups") != []):
                raise ValueError("The percentage denominator must match the trusted current selection")
            if basis == "current_selection_before_question_targets":
                if denominator != base_filters:
                    raise ValueError("The percentage denominator must match the trusted current selection")
            else:
                # A question's comparison group may only narrow the trusted selection.
                validate_share_scope(denominator, base_filters)
            validate_share_scope(numerator, denominator)
            seen, messages = set(), []
            for item in data["collections"]:
                dataset, n, d = item["dataset"], item["numerator"], item["denominator"]
                if (dataset not in {"native", "social"} or dataset in seen
                        or numerator.dataset not in {"all", dataset}
                        or type(n) is not int or type(d) is not int or not 0 <= n <= d
                        or type(item.get("total")) is not int or item["total"] != n):
                    raise ValueError("Invalid separate-collection percentage counts")
                seen.add(dataset)
                expected = 100 * n / d if d else None
                if (item.get("percentage") != expected or isinstance(item.get("percentage"), bool)
                        or item.get("percentage_status") != ("defined" if d else "empty_selection")):
                    raise ValueError("The percentage must be computed from its numerator and denominator")
                unit = "native ad records" if dataset == "native" else "social ad records"
                messages.append(f"{expected:.2f}% ({n:,} of {d:,} eligible {unit})" if d
                                else f"Percentage undefined (0 eligible {unit} in the denominator)")
            if not messages:
                raise ValueError("A percentage result needs a loaded collection")
            if basis == "question_comparison_group":
                group = _plain_scope(denominator, base_filters)
                suffix = f". The comparison group is {group} in this collection."
            else:
                suffix = ". Denominators use the current selection before question targets."
            note = (data.get("date_inference") or {}).get("note") or ""
            return Answer(status="answered", answer_mode="statistics",
                          answer="; ".join(messages) + suffix + (" " + note if note else ""), structured_result=data)
        totals = "; ".join(
            f"{item['total']:,} eligible {'native ad records' if item['dataset'] == 'native' else 'social ad records'}"
            for item in data["collections"]
        )
        group_by = data.get("group_by")
        if group_by and group_by != "none":
            names = {item["name"] for item in data["groups"] if item["name"] != "(Unknown)"}
            dimension = {"publishers": "publishers", "sponsors": "source-listed sponsors / organizations",
                         "platforms": "platforms"}[group_by]
            message = f"{len(names):,} {dimension} appear in {totals} within this selection. Every category and its count is listed below."
        else:
            message = f"{totals} match this question and the active filters."
        note = (data.get("date_inference") or {}).get("note") or ""
        return Answer(status="answered", answer_mode="statistics",
                      answer=message + (" " + note if note else ""), structured_result=data)

    @staticmethod
    def _time_statistics_answer(data, *, base_filters):
        """Describe bounded time aggregates from the tool, preserving every part."""
        filters = Filters.model_validate(data["filters"])
        if not isinstance(base_filters, Filters) or data.get("method") != "database":
            raise ValueError("Time statistics require the trusted database scope")
        validate_share_scope(filters, base_filters)
        units = {"native": "native ad records", "social": "social ad records"}
        collections = data.get("collections") or []
        for item in collections:
            if (item.get("dataset") not in units or type(item.get("total")) is not int
                    or item["total"] < 0 or type(item.get("unknown_dates")) is not int
                    or not 0 <= item["unknown_dates"] <= item["total"]):
                raise ValueError("Invalid time-statistics totals")
        parts = []
        if data["kind"] == "compare_periods":
            periods = data.get("periods") or []
            if not 2 <= len(periods) <= 3:
                raise ValueError("A time comparison requires two or three periods")
            for period in periods:
                validate_share_scope(Filters.model_validate(period["filters"]), filters)
            for item in collections:
                counts = []
                for period in periods:
                    matches = [row for row in period["collections"] if row["dataset"] == item["dataset"]]
                    if len(matches) != 1 or type(matches[0].get("total")) is not int or matches[0]["total"] < 0:
                        raise ValueError("Every period must include each selected collection")
                    counts.append(matches[0]["total"])
                parts.append(units[item["dataset"]].capitalize() + ": " + "; ".join(
                    f"{count:,} in {period['label']}" for period, count in zip(periods, counts)))
                if len(counts) == 2:
                    if counts[0] == counts[1]:
                        parts.append("The two periods have equal counts")
                    else:
                        larger = 0 if counts[0] > counts[1] else 1
                        parts.append(f"{periods[larger]['label']} has {abs(counts[0] - counts[1]):,} more records")
        else:
            groups = data.get("groups") or []
            for item in collections:
                years = [group for group in groups if group["dataset"] == item["dataset"]]
                if any(type(group.get("count")) is not int or group["count"] < 0 for group in years):
                    raise ValueError("Invalid yearly counts")
                if data["kind"] == "top_years":
                    if years:
                        highest = max(group["count"] for group in years)
                        if any(group["count"] != highest for group in years):
                            raise ValueError("A highest-year result must preserve ties")
                        verb = "tie for the highest count" if len(years) > 1 else "has the highest count"
                        parts.append(f"{units[item['dataset']].capitalize()}: "
                                     + ", ".join(group["name"] for group in years)
                                     + f" {verb}, with {highest:,} records per year")
                    else:
                        parts.append(f"No dated {units[item['dataset']]} are available for year ranking")
                else:
                    if sum(group["count"] for group in years) + item["unknown_dates"] != item["total"]:
                        raise ValueError("Year counts and unknown dates must reconcile with the collection total")
                    parts.append(f"{item['total']:,} {units[item['dataset']]} are grouped by year below")
        parts.extend(f"{item['unknown_dates']:,} {units[item['dataset']]} have no date under this date basis; "
                     "they are listed separately" for item in collections)
        note = (data.get("date_inference") or {}).get("note")
        if note:
            parts.append(note)
        return Answer(status="answered", answer_mode="statistics", answer=". ".join(part.rstrip(". ") for part in parts) + ".",
                      structured_result=data)

    def _answer_legacy(self, question, filters, visitor, progress=None):
        start = time.monotonic()
        if not 1 <= len(question.strip()) <= 2000:
            return Answer(
                status="insufficient_evidence",
                answer="Please enter a question of 1–2,000 characters.",
            )
        normalized = question.strip().lower().rstrip("?.!")
        current_count = normalized in {
            "how many records",
            "how many records are there",
            "how many records match the current filters",
            "count records",
            "当前筛选有多少条记录",
            "当前有多少条记录",
        }
        # Preflight has no database or model dependency. Resolve only structured
        # questions against actual source facets; semantic questions keep RAG.
        plan = (QuestionPlan("ready", kind="count", filters=filters.model_copy(deep=True),
                             scope_notes=("Counts use current eligible records within the active filters.",))
                if current_count else plan_question(question, filters, {}))
        if plan is not None:
            try:
                _progress(progress, "database")
                if plan.status == "unsupported":
                    return Answer(status="insufficient_evidence", answer_mode="clarification", answer=plan.message)
                before = self.health()
                version = before.get("data_version")
                if before.get("status") != "ok" or not isinstance(version, str) or not version or version == "unavailable":
                    raise ValueError("A healthy source version is required for statistics")
                if not current_count:
                    plan = plan_question(question, filters, self.facets(filters.dataset))
                if plan.status != "ready":
                    return Answer(status="insufficient_evidence", answer_mode="clarification", answer=plan.message)
                result = self._statistics_answer(plan)
                after = self.health()
                if after.get("status") != "ok" or after.get("data_version") != version:
                    result = Answer(
                        status="service_unavailable", answer_mode="statistics",
                        failure_reason="data_changed_during_statistics",
                        answer="The collection changed while processing this question. Please submit it again.",
                    )
                else:
                    result.structured_result = {**result.structured_result, "data_version": version}
            except Exception:
                log.warning("Statistics answer unavailable")
                result = Answer(
                    status="service_unavailable", answer_mode="statistics",
                    answer="Collection statistics are temporarily unavailable. Please retry; no model call was made.",
                    failure_reason="statistics_unavailable",
                )
            result.latency_ms = int((time.monotonic() - start) * 1000)
            return result
        if re.search(
            r"\b(how many|count|percentage|percent|total number)\b|多少|占比|百分比",
            normalized,
        ):
            return Answer(
                status="insufficient_evidence",
                answer_mode="clarification",
                answer="Counts support named outlets, sponsors and explicit dates. Percentages compare target records with the current selected collection; select the denominator scope first. Topic counts require validated annotations. Retrieved passages cannot establish corpus totals.",
            )
        return self._answer_evidence(question, filters, visitor, progress=progress)

    def _answer_evidence(self, question, filters, visitor, search_query=None, audit=True,
                         retrieval_groups=None, progress=None):
        start = time.monotonic()
        evidence = []
        reservation = None
        dispatched = False
        version = "unavailable"
        embedding_cost = []
        coverage = []
        search_query = search_query or question
        try:
            _progress(progress, "database")
            version = self.db.health()["data_version"]
            trusted_filters = filters.model_copy(deep=True)
            filters = self._title_scope(question, filters)
            searches = retrieval_groups or [{"query": search_query, "filters": filters.model_dump(mode="json"), "label": "Current selection"}]
            if not 1 <= len(searches) <= 3:
                raise ValueError("Invalid comparison scope count")
            # Narrowing is revalidated here even for a custom research adapter.
            scoped = []
            for item in searches:
                scope = Filters.model_validate(item["filters"])
                validate_share_scope(scope, trusted_filters)
                if filters.record_ids:
                    selected_ids = [rid for rid in filters.record_ids if not scope.record_ids or rid in scope.record_ids]
                    if not selected_ids:
                        raise ValueError("Comparison titles do not intersect the requested records")
                    scope.record_ids = selected_ids
                validate_share_scope(scope, filters)
                scoped.append((item, scope))
            # Keyword search always runs first so failures can return usable evidence.
            for item, scope in scoped:
                evidence.extend(self.search(item["query"], scope, limit=3 if retrieval_groups else 5))
            price(self.settings.generation_model, 1, 1)
            # Admission covers the whole paid question, before query embedding.
            # This exceeds the maximum generation cost under the 100k-byte prompt cap.
            reservation = self.rag.budget.reserve(
                "0.04", visitor, "generation", self.settings.generation_model
            )
            vectors = self.rag.embed([item["query"] for item, _ in scoped], visitor=visitor, cost_sink=embedding_cost)
            if len(vectors) != len(scoped):
                raise ValueError("Embedding count mismatch")
            evidence = []
            for (item, scope), vector in zip(scoped, vectors):
                found = self._public_evidence(self.db.search(
                    item["query"], scope, vector=vector, model=self.settings.embedding_model,
                    chunks_per_record=3, **({"limit": 3} if retrieval_groups else {}),
                ))
                coverage.append({"label": item.get("label") or "Selected target", "passages": len(found),
                                 "filters": scope.model_dump(mode="json"), "query": item["query"]})
                evidence.extend(found)
            evidence = list({e.evidence_id: e for e in evidence}.values())
            if self.db.health()["data_version"] != version:
                evidence = []
                version = self.db.health()["data_version"]
                raise ValueError(
                    "Data changed during retrieval; retry on a consistent version"
                )
            dispatched = True
            _progress(progress, "organizing")
            result = self.rag.generate(
                question, evidence, visitor, reservation=reservation,
                **({"progress": progress} if progress else {}),
            )
            _progress(progress, "citations")
            final_version = self.db.health()["data_version"]
            if final_version != version:
                # A dispatched call remains charged and audited, but an answer
                # from an index superseded during generation is not published.
                evidence = []
                version = final_version
                raise ValueError(
                    "Data changed during generation; retry on a consistent version"
                )
        except LimitReached as exc:
            result = Answer(status="limited", answer=str(exc), evidence=evidence)
        except Exception as exc:
            log.warning("Answer failed: %s", type(exc).__name__)
            known = {
                "Unverifiable citation": "citation_mismatch",
                "Citation exceeds short-quote limit": "quote_too_long",
                "Missing or excessive claims": "invalid_claim_count",
                "Evidence failed original-version validation": "evidence_version_mismatch",
                "Unverifiable answer-structure citation": "answer_structure_mismatch",
                "Data changed during retrieval; retry on a consistent version": "data_changed_during_retrieval",
                "Data changed during generation; retry on a consistent version": "data_changed_during_generation",
            }
            reason = known.get(str(exc), type(exc).__name__)
            result = Answer(
                status="service_unavailable",
                answer="The answer service is unavailable. Browse the evidence below or use keyword search.",
                evidence=evidence,
                failure_reason=reason,
            )
        finally:
            if reservation and not dispatched:
                self.rag.budget.cancel_unsent(reservation)
        if reservation:
            result.cost_usd = self.rag.budget.reservation_cost(reservation) + sum(
                embedding_cost
            )
        if coverage:
            result.structured_result = {"kind": "evidence_coverage", "groups": coverage,
                                        "meaning": "Retrieved passages are a sample, not complete coverage or verified semantic support."}
        gaps = [item["label"] for item in coverage if not item["passages"]]
        if retrieval_groups and gaps and result.status == "answered":
            # A supported statement about one target is a partial answer,
            # never a completed comparison with a target that has no evidence.
            result.status = "insufficient_evidence"
            result.answer += "\n\nThe collection comparison is incomplete: no passages were retrieved for " + ", ".join(gaps) + "."
        # Only an evidence shortfall qualifies. Outages, bad source versions,
        # invalid quotes, limits and unsupported statistics never trigger search.
        if result.status == "insufficient_evidence":
            topics = gaps if retrieval_groups else []
            result.external_research = self._search_external(question, filters, visitor, missing_topics=topics, progress=progress)
            result.cost_usd += result.external_research.get("cost_usd", 0.0)
        if result.status == "answered":
            # Written by the server: a cited record's date is an estimate, or
            # could not be checked. An unknown is never reported as "none".
            from .date_inference import BASIS_NOTE, UNCHECKED_NOTE

            cited = self._cites_inferred_dates(result, filters)
            if cited is None:
                result.answer = result.answer + "\n\n" + UNCHECKED_NOTE
            elif cited:
                result.answer = result.answer + "\n\n" + BASIS_NOTE
        if result.external_research:
            after_web = self.health()
            if after_web.get("status") != "ok" or after_web.get("data_version") != version:
                result = self._withhold_changed_web_result(result)
        result.latency_ms = int((time.monotonic() - start) * 1000)
        if audit:
            try:
                self.db.save_answer(question, filters, result, version)
            except Exception:
                log.warning("Answer audit log unavailable")
        return result

    def _web_scope_supported(self, filters):
        # Every scope may be supplemented; the filters travel as context only.
        # Links must be showable, because external content is only usable with its sources.
        return bool(self.settings.show_source_links)

    def _search_external(self, question, filters, visitor, missing_topics=None, progress=None):
        if not getattr(self.settings, "web_search_enabled", False):
            return {"status": "disabled", "reason": "web_search_not_enabled", "cost_usd": 0.0}
        if not self._web_scope_supported(filters):
            return {"status": "unavailable", "reason": "scope_not_supported", "cost_usd": 0.0,
                    "message": "Web research is unavailable for this record, annotation or unloaded social scope. Adjust the selection to search for external native-ad sources."}
        from .web_research import WebResearch

        adapter = self.web_research or WebResearch(self.rag, enabled=True, base_filters=filters)
        _progress(progress, "web")
        try:
            result = adapter.call({"question": question, "missing_topics": missing_topics or []}, visitor=visitor)
        except Exception:
            # The production adapter contains accounting failures; a custom
            # adapter must likewise not expose provider diagnostics.
            result = {"status": "unavailable", "reason": "web_research_unavailable", "cost_usd": 0.0}
        _progress(progress, "citations")
        return result
