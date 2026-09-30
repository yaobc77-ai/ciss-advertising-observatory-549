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
from .structured_queries import QuestionPlan, plan_question

log = logging.getLogger(__name__)


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
    }


class Service:
    def __init__(self, settings, db=None, rag=None, research_agent=None, claims_store=None):
        self.settings = settings
        self.db = db or Database(settings.database_url)
        self.rag = rag or Rag(self.db, settings)
        self.research_agent = research_agent
        self.claims_store = claims_store

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

        filters = plan.filters
        datasets = ["native", "social"] if filters.dataset == "all" else [filters.dataset]
        collections, groups, records = [], [], []
        for dataset in datasets:
            snapshot = self.dashboard(filters.model_copy(update={"dataset": dataset}), limit=10)
            stats = snapshot["stats"]
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
            fields = ("record_id", "version_id", "dataset", "title", "date", "publisher",
                      "sponsor", "url", "archive_url", "retrievable")
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
        return Answer(
            status="answered", answer=text, answer_mode="statistics",
            structured_result={
                "kind": plan.kind, "method": "database", "group_by": plan.group_by,
                "filters": filters.model_dump(mode="json"), "collections": collections,
                "groups": groups, "records": records, "scope_notes": list(plan.scope_notes),
            },
        )

    def answer(self, question, filters, visitor):
        if getattr(self.settings, "research_agent_enabled", False):
            return self._answer_with_tools(question, filters, visitor)
        return self._answer_legacy(question, filters, visitor)

    def _answer_with_tools(self, question, filters, visitor):
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
            version = self.db.health()["data_version"]
            catalog = ToolCatalog(self, filters)
            agent = self.research_agent or ResearchAgent(self.rag, catalog)
            run = agent.run(question, filters, visitor)
            data = run.result
            if run.route == "statistics":
                result = self._tool_statistics_answer(data)
            elif run.route == "evidence":
                narrowed = Filters.model_validate(data["filters"])
                # Question language and wording remain intact for the cited answer.
                result = self._answer_evidence(question, narrowed, visitor,
                                               search_query=data.get("search_query") or data.get("query") or question, audit=False)
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
            else:
                result = Answer(
                    status="limited" if run.route == "limited" else "service_unavailable",
                    answer_mode="tools", failure_reason=run.failure_reason,
                    answer="Question understanding is temporarily unavailable. Browse the collection or use keyword search.",
                )
            downstream_cost = result.cost_usd
            if self.db.health()["data_version"] != version:
                result = Answer(status="service_unavailable", answer_mode="tools",
                                failure_reason="data_changed_during_tool_research",
                                answer="The collection changed while processing this question. Please submit it again.",
                                cost_usd=result.cost_usd)
            result.cost_usd += run.cost_usd
            result.research_trace = run.audit()
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
    def _tool_statistics_answer(data):
        """Publish program-computed counts; model-written totals are never used."""
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
        return Answer(status="answered", answer_mode="statistics", answer=message, structured_result=data)

    def _answer_legacy(self, question, filters, visitor):
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
                if not current_count and plan.status != "unsupported":
                    plan = plan_question(question, filters, self.facets(filters.dataset))
                if plan.status != "ready":
                    return Answer(status="insufficient_evidence", answer_mode="clarification", answer=plan.message)
                result = self._statistics_answer(plan)
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
                answer="Counts support named outlets, sponsors and explicit dates. For example: How many native ads are from the New York Times? Percentages and topic counts require a defined denominator or validated annotations. Retrieved passages cannot establish corpus totals.",
            )
        return self._answer_evidence(question, filters, visitor)

    def _answer_evidence(self, question, filters, visitor, search_query=None, audit=True):
        start = time.monotonic()
        evidence = []
        reservation = None
        dispatched = False
        version = "unavailable"
        embedding_cost = []
        search_query = search_query or question
        try:
            version = self.db.health()["data_version"]
            filters = self._title_scope(question, filters)
            # Keyword search always runs first so failures can return usable evidence.
            evidence = self.search(search_query, filters)
            price(self.settings.generation_model, 1, 1)
            # Admission covers the whole paid question, before query embedding.
            # This exceeds the maximum generation cost under the 100k-byte prompt cap.
            reservation = self.rag.budget.reserve(
                "0.04", visitor, "generation", self.settings.generation_model
            )
            vector = self.rag.embed(
                [search_query], visitor=visitor, cost_sink=embedding_cost
            )[0]
            evidence = self._public_evidence(
                self.db.search(
                    search_query,
                    filters,
                    vector=vector,
                    model=self.settings.embedding_model,
                    chunks_per_record=3,
                )
            )
            if self.db.health()["data_version"] != version:
                evidence = []
                version = self.db.health()["data_version"]
                raise ValueError(
                    "Data changed during retrieval; retry on a consistent version"
                )
            dispatched = True
            result = self.rag.generate(
                question, evidence, visitor, reservation=reservation
            )
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
        result.latency_ms = int((time.monotonic() - start) * 1000)
        if reservation:
            result.cost_usd = self.rag.budget.reservation_cost(reservation) + sum(
                embedding_cost
            )
        if audit:
            try:
                self.db.save_answer(question, filters, result, version)
            except Exception:
                log.warning("Answer audit log unavailable")
        return result
