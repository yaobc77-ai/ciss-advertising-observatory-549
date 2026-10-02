"""Read-only research tools shared by model calls and the MCP server.

The language model chooses an operation, never SQL, a URL, or a source path.
Every tool intersects its request with the trusted collection selection. Source
names identify candidates; display aliases never merge organization identities.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    model_validator,
)

from .analytics import LABEL_NOTE, sponsor_display
from .date_inference import BASIS_NOTE
from .knowledge_graph import (
    IDENTITY_POLICY,
    NODE_TYPES,
    PREDICATES,
    SCHEMA_VERSION,
    build_graph,
)
from .models import Filters
from .structured_queries import canonical_source_values

Name = Annotated[str, Field(min_length=1, max_length=200)]
Names = Annotated[list[Name], Field(max_length=20)]
RecordId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")]
Question = Annotated[str, Field(min_length=1, max_length=2000)]
NcId = Annotated[str, Field(pattern=r"^NC_[1-9][0-9]*$", max_length=100)]
ScId = Annotated[str, Field(pattern=r"^SC_[1-9][0-9]*$", max_length=100)]
_DIMENSIONS = ("publishers", "sponsors", "platforms", "keywords", "labels", "record_ids")
_RECORD_FIELDS = ("record_id", "version_id", "dataset", "title", "date", "source_date", "effective_date", "date_basis",
                  "inferred_date", "inferred_tier", "publisher", "sponsor", "retrievable", "url", "archive_url")
_NOTES = (
    "Counts describe stored records in the selected collection, not every advertisement published elsewhere.",
    "Sponsor and publisher fields are exact source candidates, not independently verified business relationships.",
    "Native articles and social posts retain separate counting units.",
    LABEL_NOTE,
)


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FiltersRequest(Request):
    dataset: Literal["native", "social", "all"] | None = None
    publishers: Names | None = None
    sponsors: Names | None = None
    platforms: Names | None = None
    keywords: Names | None = None
    labels: Names | None = None
    record_ids: Annotated[list[RecordId], Field(max_length=20)] | None = None
    date_from: date | None = None
    date_to: date | None = None
    include_unknown_dates: StrictBool | None = None
    date_presence: Literal["any", "known", "missing"] | None = Field(default=None, description="Publication dates: any, known, or missing (null/empty). Use missing to count or list ads without a publication date. Intersects trusted date filters; cannot widen a selection that excludes missing dates.")
    include_inferred_dates: StrictBool | None = Field(default=None, description="Supplemented dates are used by default where the source date is missing; set false only when the user asks for source dates only. Results label every supplemented date.")


class ScopedRequest(Request):
    filters: FiltersRequest | None = None


class ResolveEntityRequest(ScopedRequest):
    query: Annotated[str, Field(min_length=1, max_length=200)]
    entity_type: Literal["sponsor", "publisher"]
    limit: Annotated[StrictInt, Field(ge=1, le=10)] | None = None


class StatisticsPeriod(Request):
    label: Annotated[str, Field(min_length=1, max_length=100)]
    date_from: date | None = None
    date_to: date | None = None

    @model_validator(mode="after")
    def explicit_range(self):
        if self.date_from is None and self.date_to is None:
            raise ValueError("A comparison period needs an explicit date endpoint")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("Period endpoints are reversed")
        return self


class StatisticsRequest(ScopedRequest):
    group_by: Literal["none", "publishers", "sponsors", "platforms", "years"] | None = None
    ranking: Literal["all", "highest"] | None = Field(default=None, description="For group_by='years': all returns the full year distribution; highest returns EVERY year tied for the largest dated-record count. Unknown dates are always reported separately.")
    periods: Annotated[list[StatisticsPeriod], Field(min_length=2, max_length=3)] | None = Field(default=None, description="Compare two or three named explicit date ranges in one database snapshot. Endpoints are inclusive, null is open-ended. Shared filters apply to every period. Missing dates are reported separately and never assigned to a period.")
    measure: Literal["count", "share"] | None = None
    denominator_filters: FiltersRequest | None = Field(default=None, description="Only for measure='share' when the question names the group to compare against, e.g. 'share of ExxonMobil's ads that ran in the NYT': denominator_filters={sponsors:[exxonmobil]}, filters={publishers:[The New York Times]}. The target in filters is counted within this denominator.")

    @model_validator(mode="after")
    def compatible_statistics(self):
        if self.ranking == "highest" and self.group_by != "years":
            raise ValueError("Highest ranking currently requires years")
        if self.periods:
            if self.group_by not in (None, "none") or self.measure == "share" or self.denominator_filters:
                raise ValueError("Periods compare counts without grouping or shares")
            if len({item.label.casefold().strip() for item in self.periods}) != len(self.periods):
                raise ValueError("Period labels must be distinct")
        if self.measure == "share" and self.group_by == "years":
            raise ValueError("Shares do not support grouping")
        return self


class ComparisonSearch(ScopedRequest):
    query: Question


class SearchRequest(ScopedRequest):
    query: Question
    limit: Annotated[StrictInt, Field(ge=1, le=10)] | None = None
    comparison_scopes: Annotated[list[ComparisonSearch], Field(min_length=2, max_length=3)] | None = Field(
        default=None, description="For a comparison, search each company/outlet separately. Each scope must narrow one sponsor or publisher from the active filters. Use the same topic qualifiers in every query. Never let one company's results substitute for another's."
    )


class WebGapRequest(Request):
    ticket: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class RecordRequest(ScopedRequest):
    record_id: RecordId


class RecordTextRequest(RecordRequest):
    body_start: Annotated[StrictInt, Field(ge=0, le=10000000)] | None = None
    body_limit: Annotated[StrictInt, Field(ge=1, le=12000)] | None = None


class GraphRequest(ScopedRequest):
    record_id: RecordId | None = None
    offset: Annotated[StrictInt, Field(ge=0, le=100000)] | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=5)] | None = None


class ClaimsRequest(ScopedRequest):
    nc_ids: Annotated[list[NcId], Field(max_length=20)] | None = None
    sc_ids: Annotated[list[ScId], Field(max_length=20)] | None = None
    taxonomy: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    review_state: Literal["automatic_unverified", "human_supported"] | None = None
    offset: Annotated[StrictInt, Field(ge=0, le=100000)] | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=20)] | None = None


TOOLS = {
    "resolve_entity": (ResolveEntityRequest,
        "Resolve a sponsor or publisher against actual source names. Returns candidates, not corporate identity merges; ask for clarification when ambiguous."),
    "record_statistics": (StatisticsRequest,
        "Count selected stored records or list every publisher/sponsor/platform and its count. For counts by year use group_by='years'; for the highest year use ranking='highest', which includes all ties and reports unknown dates separately. To compare before/after or two periods use periods with named inclusive date ranges and shared filters; never replace that task with a single total. measure='share' calculates a target's percentage. By default the denominator is the trusted current selection and filters narrow only the numerator. When the question names its own comparison group (for example the share of ExxonMobil ads that ran in the NYT), put that group in denominator_filters and the target in filters; the target is counted inside the group. Clarify an ambiguous comparison group instead of guessing. For ads without a publication date use filters.date_presence='missing'. Share requires group_by='none'. Separate native/social denominators; zero denominator means undefined. Exact SQL including records without searchable body; never infer totals from retrieved passages. Records carry date_basis; preserve the selected source-only or supplemented-date basis and keep the returned label in the answer."),
    "search_records": (SearchRequest,
        "Free keyword retrieval of bounded source passages within the collection selection. Use for article content, never corpus totals or factual verification. Each passage carries date_basis (source, inferred:<method> or missing); when citing a passage whose date is supplemented, say so."),
    "get_record": (RecordTextRequest,
        "Read a bounded unchanged article text interval by exact record ID with version/body-hash and character positions. Current detail adapter supports native records."),
    "get_record_sources": (RecordRequest,
        "Read version-bound public source references and historical annotation limitations. No file access or arbitrary URL fetching; current detail adapter supports native records."),
    "get_graph_schema": (Request,
        "Read graph node/predicate definitions, source identity policy and adapter limitations. The graph expresses recorded provenance, not verified greenwashing."),
    "get_graph_neighborhood": (GraphRequest,
        "Read a paged typed source graph for at most five selected native articles. This bounded neighborhood is not the complete graph or a basis for corpus totals."),
    "get_claims_matches": (ClaimsRequest,
        "Read published CLAIMS2 NC_/SC_ taxonomy assignments with definitions, review states and exact version-bound source quotes. Optional category IDs, taxonomy fingerprint and review state narrow current collection filters. Counts cover published positive matches only; unmatched records are not classified negatives and assignments do not establish verified greenwashing or factual truth. No classification or writes occur."),
}


class ScopeConflict(ValueError):
    """An attempted narrowing has no valid intersection with trusted scope."""


def strict_schema(schema):
    """Responses strict schemas require every optional property to be nullable."""
    schema = deepcopy(schema)

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object" or "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in list(node.values()):
                walk(value)

    walk(schema)
    return schema


def _normal(value):
    return " ".join(re.findall(r"\w+", value.casefold()))


def _related_name(query, names):
    # Whole words avoid treating short names such as BP as a substring in an
    # unrelated spelling. Longer source names remain separate candidates.
    return bool(query) and any(f" {query} " in f" {item} " for item in names)


def _candidate_id(dataset, field, value):
    encoded = json.dumps((dataset, field, value), ensure_ascii=False, separators=(",", ":"))
    kind = "sponsorcandidate" if field == "sponsor" else "outlet"
    return f"{kind}:{hashlib.sha256(encoded.encode()).hexdigest()[:32]}"


class ToolCatalog:
    """A per-request executor whose caller-selected scope cannot be expanded."""

    def __init__(self, service, base_filters: Filters, *, record_details=None):
        self.service = service
        self._base = base_filters.model_copy(deep=True)
        self.record_details = record_details
        # Audit of aliases mapped to exact source values during this request.
        self.alias_resolutions = []
        self._web_ticket = None
        self._web_lock = threading.Lock()

    @property
    def base_filters(self):
        return self._base.model_copy(deep=True)

    def definitions(self):
        return [{"type": "function", "name": name, "description": description,
                 "parameters": strict_schema(model.model_json_schema()), "strict": True}
                for name, (model, description) in TOOLS.items()]

    def mcp_definitions(self):
        return [{"name": name, "description": description,
                 "inputSchema": model.model_json_schema()}
                for name, (model, description) in TOOLS.items()]

    def entity_context(self, limit=200):
        """Small public namespace context; unknown names still fail at execution."""
        limit = max(1, min(int(limit), 200))
        facets = self.service.facets(self._base.dataset)
        result = {"identity_policy": IDENTITY_POLICY, "truncated": False,
                  "ambiguity_hints": []}
        for dimension in ("publishers", "sponsors"):
            values = list(facets.get(dimension, []))
            if getattr(self._base, dimension):
                values = [value for value in values if value in getattr(self._base, dimension)]
            values = [value for value in values if value and value != "(Unknown)"]
            result[dimension] = [{"value": value, "display": sponsor_display(value)
                                  if dimension == "sponsors" else value}
                                 for value in values[:limit]]
            result["truncated"] |= len(values) > limit
            for value in values[:limit]:
                query = _normal(value).removeprefix("the ")
                related = [candidate for candidate in values if _related_name(query, {
                    _normal(candidate), _normal(candidate).removeprefix("the ")})]
                if len(related) > 1 and len(result["ambiguity_hints"]) < 30:
                    result["ambiguity_hints"].append({"field": dimension, "query": value,
                        "source_candidates": related[:10], "candidate_count": len(related),
                        "instruction": "A short name can refer to multiple source candidates. Resolve or clarify; do not silently select or merge them. Exact active filters still identify a source value."})
        return result

    def narrow(self, requested: FiltersRequest | None, *, base: Filters | None = None):
        # ``base`` lets a share numerator narrow its own trusted denominator.
        filters = base.model_copy(deep=True) if base is not None else self.base_filters
        if requested is None:
            return filters
        updates = requested.model_dump(exclude_none=True)
        dataset = updates.pop("dataset", None)
        if dataset and dataset != "all":
            if filters.dataset not in ("all", dataset):
                raise ScopeConflict("The requested collection is outside the current selection.")
            filters.dataset = dataset
        # Asking for all never broadens a native/social selection.
        for dimension in _DIMENSIONS:
            values = updates.pop(dimension, None)
            if not values:
                continue
            if dimension in ("publishers", "sponsors"):
                values = self._canonical(dimension, values, filters.dataset,
                                         selected=getattr(filters, dimension))
            existing = getattr(filters, dimension)
            selected = [value for value in values if not existing or value in existing]
            if not selected or len(set(selected)) != len(set(values)):
                raise ScopeConflict("Some requested source names or records are outside the active filters. Clarify the selection instead of silently dropping them.")
            setattr(filters, dimension, list(dict.fromkeys(selected)))
        presence = updates.pop("date_presence", None)
        if presence and presence != "any":
            if filters.date_presence not in ("any", presence):
                raise ScopeConflict("Known-date and missing-date selections do not intersect the active filters.")
            filters.date_presence = presence
        lower = updates.pop("date_from", None)
        upper = updates.pop("date_to", None)
        if lower:
            filters.date_from = max(filter(None, (filters.date_from, lower)))
        if upper:
            filters.date_to = min(filter(None, (filters.date_to, upper)))
        if filters.date_from and filters.date_to and filters.date_from > filters.date_to:
            raise ScopeConflict("The requested dates do not intersect the active date range.")
        inferred = updates.pop("include_inferred_dates", None)
        if inferred is not None:
            filters.include_inferred_dates = inferred
        include_unknown = updates.pop("include_unknown_dates", None)
        if lower or upper:
            filters.include_unknown_dates = False
        elif include_unknown is not None:
            filters.include_unknown_dates = filters.include_unknown_dates and include_unknown
        if filters.date_presence == "missing" and not filters.include_unknown_dates:
            raise ScopeConflict("The active selection excludes missing publication dates; their count cannot be read by widening the date filter.")
        return filters

    def _canonical(self, dimension, values, dataset, *, selected=()):
        """Replace a known alias by its exact source value only when it is unique.

        Models pass names such as "NYT" or "ExxonMobil" straight into filters.
        An alias from the shared table, or a display name, that matches exactly
        one source value is mapped and recorded; ambiguous or unknown names stay
        unchanged so the strict facet check still asks the user.
        """
        known = set()
        for name in ("native", "social") if dataset == "all" else (dataset,):
            known.update(v for v in self.service.facets(name).get(dimension, []) if v and v != "(Unknown)")
        result = []
        for value in values:
            query = _normal(value).removeprefix("the ")
            related = sorted(candidate for candidate in known if _related_name(query, {
                _normal(candidate), _normal(candidate).removeprefix("the ")})
                and _normal(candidate).removeprefix("the ") != query)
            if (dimension == "sponsors" and value in known and value not in selected
                    and related and not set(related).issubset(values)):
                names = "; ".join([value, *related[:9]])
                raise ScopeConflict("This short sponsor name matches separate source candidates: "
                    + names + ". Choose an exact source value in the collection filters, or explicitly select the source names to compare; they are not silently merged.")
            matches = [] if value in known else canonical_source_values(value, dimension, sorted(known))
            if len(matches) == 1:
                item = {"field": dimension, "requested": value, "source_value": matches[0]}
                if item not in self.alias_resolutions:
                    self.alias_resolutions.append(item)
                value = matches[0]
            result.append(value)
        return result

    def _known_filters(self, filters):
        facets = self.service.facets(filters.dataset)
        for dimension in _DIMENSIONS[:-1]:
            unknown = set(getattr(filters, dimension)) - set(facets.get(dimension, []))
            if unknown:
                raise ScopeConflict("A requested news outlet, sponsor or label is not in this collection. Check the name or adjust the selection.")

    def _context(self, name, filters=None):
        context = {"tool": name, "filters": (filters or self._base).model_dump(mode="json"),
                   "scope_notes": list(_NOTES)}
        if self.alias_resolutions:
            context["alias_resolutions"] = [dict(item) for item in self.alias_resolutions]
        return context

    def _availability(self, filters):
        health = self.service.health()
        if health.get("status") != "ok":
            return {"status": "unavailable", "message": "The collection database is unavailable.",
                    "data_version": "unavailable"}
        datasets = ("native", "social") if filters.dataset == "all" else (filters.dataset,)
        missing = [dataset for dataset in datasets if not health.get("record_counts", {}).get(dataset)]
        return {"status": "unavailable" if len(missing) == len(datasets) else "ok",
                "message": "No selected collection is loaded." if len(missing) == len(datasets) else "",
                "data_version": health.get("data_version", "unavailable"),
                "missing_datasets": missing}

    def call(self, name, arguments):
        if name not in TOOLS or not isinstance(arguments, dict):
            return {"status": "invalid_request", "tool": name if name in TOOLS else "unknown",
                    "message": "Unknown tool or invalid argument object."}
        model = TOOLS[name][0]
        try:
            request = model.model_validate(arguments)
        except ValidationError:
            return {**self._context(name), "status": "invalid_request",
                    "message": "Arguments must match the bounded tool schema; extra fields are not accepted."}
        try:
            filters = self.narrow(getattr(request, "filters", None))
            context = self._context(name, filters)
            if name == "get_graph_schema":
                return {**context, "status": "ok", **self._graph_schema()}
            availability = self._availability(filters)
            if availability["status"] != "ok":
                return {**context, **availability}
            if filters.dataset in availability["missing_datasets"]:
                return {**context, **availability, "status": "unavailable",
                        "message": "This collection has not been loaded; zero is not an established advertising count."}
            self._known_filters(filters)
            result = getattr(self, "_" + name)(request, filters)
            final_health = self.service.health()
            if (final_health.get("status") != "ok" or final_health.get("data_version") != availability["data_version"]):
                return {**context, "status": "unavailable", "message": "Data changed during the read; retry against a current version."}
            # Health is an audit marker, not a claim that distinct tool calls
            # share one database snapshot. Row versions remain authoritative.
            result = {**context, **availability, **result}
            if self.alias_resolutions:
                # Include aliases resolved while reading, e.g. in a comparison group.
                result["alias_resolutions"] = [dict(item) for item in self.alias_resolutions]
            json.dumps(result, ensure_ascii=False, allow_nan=False)
            return result
        except ScopeConflict as exc:
            return {**self._context(name), "status": "clarify", "message": str(exc)}
        except Exception:
            # Driver errors can contain credentials, SQL, or source paths.
            return {**self._context(name), "status": "unavailable",
                    "message": "The requested read is unavailable. No model-generated result is substituted."}

    def _resolve_entity(self, request, filters):
        datasets = ("native", "social") if filters.dataset == "all" else (filters.dataset,)
        dimension = request.entity_type + "s"
        query = _normal(request.query)
        candidates = []
        for dataset in datasets:
            for value in self.service.facets(dataset).get(dimension, []):
                if not value or value == "(Unknown)":
                    continue
                if getattr(filters, dimension) and value not in getattr(filters, dimension):
                    continue
                display = sponsor_display(value) if request.entity_type == "sponsor" else value
                names = {_normal(value), _normal(display)}
                # Dropping a leading article is a display lookup only; each
                # matched exact source spelling remains a separate candidate.
                names |= {item.removeprefix("the ") for item in names}
                alias = bool(canonical_source_values(request.query, dimension, [value]))
                if alias:
                    names.add(query)
                if query in names or _related_name(query, names):
                    candidates.append({"entity_id": _candidate_id(dataset, request.entity_type, value),
                                       "dataset": dataset, "source_field": request.entity_type,
                                       "source_value": value, "display_name": display,
                                       "match": "exact_or_display" if query in names else "partial",
                                       "identity_status": "source_candidate_not_resolved"})
        candidates.sort(key=lambda item: (item["match"] != "exact_or_display",
                                           item["dataset"], item["source_value"]))
        limit = request.limit or 10
        return {"status": "ok" if len(candidates) == 1 else "clarify",
                "candidates": candidates[:limit], "candidate_count": len(candidates),
                "truncated": len(candidates) > limit,
                "message": "Use the exact source value in filters." if len(candidates) == 1
                else "Choose a source candidate; ambiguous names are never merged." if candidates
                else "No matching source candidate was found. Clarify the name or collection."}

    def _record_statistics(self, request, filters):
        group_by = request.group_by or "none"
        if group_by == "years" or request.periods:
            return self._complete_statistics(request, filters)
        if request.measure == "share" and group_by != "none":
            raise ScopeConflict("A percentage compares target records with the current selection. Use no grouping, or ask for a count distribution separately.")
        kind = "share" if request.measure == "share" else {
            "none": "count", "publishers": "list_publishers", "sponsors": "list_sponsors",
            "platforms": "list_platforms"}[group_by]
        # Reuse the exact same read-snapshot and public field projection as UI.
        denominator, basis = (self.base_filters, "current_selection_before_question_targets") if kind == "share" else (None, None)
        if request.denominator_filters is not None:
            if kind != "share":
                raise ScopeConflict("A comparison group applies only to a percentage.")
            # The question's own comparison group, narrowed from the trusted
            # selection; the target is then counted inside that group.
            denominator = self.narrow(request.denominator_filters)
            self._known_filters(denominator)
            filters = self.narrow(request.filters, base=denominator)
            self._known_filters(filters)
            if filters == denominator:
                raise ScopeConflict("Name the target to count inside the comparison group; a group compared with itself is always 100%.")
            basis = "question_comparison_group"
        plan = SimpleNamespace(kind=kind, filters=filters, scope_notes=_NOTES,
                               group_by=None if group_by == "none" else group_by,
                               denominator_filters=denominator, denominator_basis=basis,
                               trusted_filters=self.base_filters)
        answer = self.service._statistics_answer(plan)
        result = deepcopy(answer.structured_result)
        result["records"] = [{key: row.get(key) for key in _RECORD_FIELDS}
                             for row in result.get("records", [])]
        result["group_by"] = None if group_by == "none" else group_by
        result["kind"] = kind
        missing = self._availability(filters).get("missing_datasets", [])
        if missing:
            result["collections"] = [item for item in result["collections"] if item["dataset"] not in missing]
            result["groups"] = [item for item in result["groups"] if item.get("dataset") not in missing]
            result["records"] = [item for item in result["records"] if item.get("dataset") not in missing]
            result["scope_notes"].append("Unloaded collections are omitted, not reported as zero advertisements.")
        return {"status": "ok", **result}

    def _complete_statistics(self, request, filters):
        """A requested distribution/comparison cannot be replaced by its total."""
        if request.denominator_filters or request.measure == "share":
            raise ScopeConflict("Year distributions and named periods currently compare counts, not shares.")
        periods = []
        for period in request.periods or []:
            selected = self.narrow(FiltersRequest(date_from=period.date_from,
                date_to=period.date_to, include_unknown_dates=False), base=filters)
            periods.append({"label": period.label, "filters": selected})
        ranking = request.ranking or "all"
        result = self.service.db.research_statistics(filters,
            group_by="years" if not periods else "none", ranking=ranking, periods=periods)
        result = deepcopy(result)
        missing = self._availability(filters).get("missing_datasets", [])
        result["collections"] = [item for item in result["collections"] if item["dataset"] not in missing]
        result["groups"] = [item for item in result.get("groups", []) if item["dataset"] not in missing]
        for period in result.get("periods", []):
            period["collections"] = [item for item in period["collections"] if item["dataset"] not in missing]
        result["unknown_dates"] = [{"dataset": item["dataset"], "count": item["unknown_dates"]}
                                   for item in result["collections"]]
        result["records"] = self.service._public_rows([
            {key: row.get(key) for key in _RECORD_FIELDS}
            for row in result.get("records", []) if row.get("dataset") not in missing])
        result["kind"] = "compare_periods" if periods else "top_years" if ranking == "highest" else "list_years"
        result["method"] = "database"
        result["group_by"] = None if periods else "years"
        result["ranking"] = ranking
        result["scope_notes"] = list(_NOTES)
        if missing:
            result["scope_notes"].append("Unloaded collections are omitted, not reported as zero advertisements.")
        used = sum(item.get("inferred_dates", 0) for item in result["collections"])
        result["date_inference"] = {"enabled": filters.include_inferred_dates,
            "used": used, "tiers": result.pop("inferred_tiers", {}),
            "note": BASIS_NOTE if used else "Counts use source publication dates only." if not filters.include_inferred_dates else "No supplemented dates were used in this selection."}
        return {"status": "ok", **result}

    def _row(self, record_id, filters):
        if filters.dataset == "social":
            raise ScopeConflict("The current versioned detail adapter supports native records only; social source detail is not connected.")
        filters = filters.model_copy(deep=True)
        if filters.record_ids and record_id not in filters.record_ids:
            raise ScopeConflict("The record is outside the selected record IDs.")
        filters.record_ids = [record_id]
        filters.dataset = "native"
        page = self.service.db.knowledge_page(filters, limit=1)
        rows = page.get("rows", [])
        if len(rows) != 1 or rows[0].get("record_id") != record_id or rows[0].get("dataset") != "native":
            raise ScopeConflict("The record does not match the current collection filters.")
        return rows[0]

    def _record_projection(self, row):
        result = {key: row.get(key) for key in _RECORD_FIELDS}
        return self.service._public_rows([result])[0]

    def _get_record(self, request, filters):
        row = self._row(request.record_id, filters)
        body = row.get("body") or ""
        if not isinstance(body, str):
            body = ""
        start = request.body_start or 0
        if start > len(body):
            raise ScopeConflict("The requested text position is beyond the stored article.")
        end = min(len(body), start + (request.body_limit or 12000))
        digest = hashlib.sha256(body.encode()).hexdigest()
        matched = digest == row.get("body_hash")
        if not matched:
            return {"status": "unavailable", "message": "The stored text does not match its body hash; no exact source excerpt is published."}
        return {"status": "ok", "record": self._record_projection(row),
                "body": {"text": body[start:end], "start": start, "end": end,
                         "total_characters": len(body), "truncated": start > 0 or end < len(body),
                         "body_hash": row.get("body_hash") if matched else "",
                         "hash_status": "matched" if matched else "missing_or_mismatched",
                         "completeness": "not_established", "semantic_support": "not_verified"},
                "source_refs": [{"record_id": row["record_id"], "version_id": row["version_id"],
                                 "body_hash": row.get("body_hash") if matched else "",
                                 "start": start, "end": end}]}

    def _get_record_sources(self, request, filters):
        row = self._row(request.record_id, filters)
        graph = build_graph([row], links_enabled=self.service.settings.show_source_links)
        artifacts = [node for node in graph["nodes"] if node["type"] == "SourceArtifact"]
        annotations = [node for node in graph["nodes"] if node["type"] in ("Annotation", "Label")]
        body_hash = row.get("body_hash") or ""
        hash_matched = body_hash == hashlib.sha256((row.get("body") or "").encode()).hexdigest()
        return {"status": "ok", "record": self._record_projection(row),
                "source_refs": [{"record_id": row["record_id"], "version_id": row["version_id"],
                                 "body_hash": body_hash if hash_matched else "",
                                 "verification_status": "body_hash_matched" if hash_matched else "missing_or_mismatched"}],
                "source_artifacts": artifacts, "source_relations": [edge for edge in graph["edges"]
                    if edge["predicate"] in ("has_source_reference", "has_archive_reference", "derived_from")],
                "annotations": annotations, "warnings": graph["warnings"],
                "attachments_status": "adapter_not_connected", "claims_status": "historical_unverified"}

    def _attach_date_basis(self, evidence, filters):
        """Label each cited passage's date as source, inferred or missing.

        A citation of a record dated by inference must say so; the notice is
        written here, not left to the model.
        """
        ids = sorted({item["record_id"] for item in evidence})
        browse = getattr(self.service, "browse", None)
        if not ids or browse is None:
            return ""
        rows = {row["record_id"]: row for row in browse(filters.model_copy(update={"record_ids": ids}))}
        inferred = False
        for item in evidence:
            row = rows.get(item["record_id"], {})
            item["date_basis"] = row.get("date_basis") or ("source" if row.get("date") else "missing")
            if str(item["date_basis"]).startswith("inferred:"):
                item["inferred_date"], item["inferred_tier"] = row.get("inferred_date"), row.get("inferred_tier")
                inferred = True
        return BASIS_NOTE if inferred else ""

    def _search_records(self, request, filters):
        with self._web_lock:
            self._web_ticket = None
        if request.comparison_scopes:
            groups, evidence, refs, rejected, seen = [], [], [], 0, set()
            # Validate every target before performing any read. A bad second
            # target must not yield a seemingly complete first-company answer.
            scopes = []
            for group in request.comparison_scopes:
                scoped = self.narrow(group.filters, base=filters)
                self._known_filters(scoped)
                if not ((len(scoped.sponsors) == 1) or (len(scoped.publishers) == 1)):
                    raise ScopeConflict("Each comparison target needs one source-listed sponsor or publisher.")
                identity = (tuple(scoped.sponsors), tuple(scoped.publishers))
                if identity in seen:
                    raise ScopeConflict("Comparison targets must be distinct source selections.")
                seen.add(identity)
                scopes.append((group, scoped))
            for group, scoped in scopes:
                item = self._search_one(group.query, scoped, min(request.limit or 3, 3))
                label = " / ".join([*(sponsor_display(s) for s in scoped.sponsors), *scoped.publishers])
                groups.append({"label": label, "query": group.query, "filters": scoped.model_dump(mode="json"),
                               "keyword_passages": len(item["evidence"]), "rejected_evidence": item["rejected_evidence"]})
                evidence.extend(item["evidence"])
                refs.extend(item["source_refs"])
                rejected += item["rejected_evidence"]
            return {"status": "ok", "query": request.query, "evidence": evidence,
                    "source_refs": refs, "rejected_evidence": rejected, "retrieval_groups": groups,
                    "coverage": "separate_target_retrieval_not_complete_corpus", "semantic_support": "not_verified"}
        result = self._search_one(request.query, filters, request.limit or 5)
        if (not result["evidence"] and not result["rejected_evidence"]
                and result["diagnostics"].get("status") == "ok"
                and getattr(self.service.settings, "web_search_enabled", False)
                and self.service._web_scope_supported(filters)):
            ticket = uuid4().hex
            with self._web_lock:
                self._web_ticket = {"ticket": ticket, "query": request.query, "filters": filters.model_copy(deep=True),
                                    "expires": time.monotonic() + 300, "data_version": self.service.health().get("data_version")}
            result["web_fallback_ticket"] = ticket
            result["web_fallback_basis"] = "no_keyword_passages_not_exhaustive_semantic_absence"
        return result

    def search_external_sources(self, arguments, *, visitor="mcp-web"):
        """Optional paid MCP read; a single-use healthy local miss is required."""
        try:
            request = WebGapRequest.model_validate(arguments)
        except ValidationError:
            return {"status": "invalid_request", "message": "Use the ticket from a prior empty search_records result."}
        with self._web_lock:
            gap = self._web_ticket
            if not gap or request.ticket != gap["ticket"] or time.monotonic() > gap["expires"]:
                return {"status": "invalid_request", "message": "An unexpired database-miss ticket is required."}
            # Atomic consumption precedes dispatch, so concurrent retries
            # cannot both charge this ticket.
            self._web_ticket = None
        health = self.service.health()
        if health.get("status") != "ok" or health.get("data_version") != gap["data_version"]:
            return {"status": "unavailable", "message": "The collection changed; search the database again."}
        return self.service._search_external(gap["query"], gap["filters"], visitor)

    def _search_one(self, query, filters, limit):
        report = self.service.search_report(query, filters, limit=limit)
        evidence, refs, rejected = [], [], 0
        for item in report.get("evidence", []):
            public = item.model_dump(mode="json")
            ref = {"evidence_id": item.evidence_id, "record_id": item.record_id,
                   "version_id": item.version_id, "start": item.start, "end": item.end,
                   "body_hash": "", "verification_status": "adapter_unavailable"}
            if item.dataset == "native":
                row = self._row(item.record_id, filters)
                body = row.get("body") or ""
                matched = (row.get("version_id") == item.version_id and 0 <= item.start < item.end <= len(body)
                           and body[item.start:item.end] == item.text
                           and row.get("body_hash") == hashlib.sha256(body.encode()).hexdigest())
                if not matched:
                    rejected += 1
                    continue
                ref.update(body_hash=row["body_hash"], verification_status="exact_character_match")
            # Retrieval chunks are already small; reject an unexpectedly huge
            # adapter response instead of falsifying its original offsets.
            if len(item.text) > 12000:
                rejected += 1
                continue
            evidence.append(public)
            refs.append(ref)
        notice = self._attach_date_basis(evidence, filters)
        diagnostics = report.get("diagnostics", {})
        allowed = {key: diagnostics[key] for key in ("status", "operator", "terms", "missing_terms", "reason")
                   if key in diagnostics}
        return {"status": "ok", "query": query, "evidence": evidence,
                **({"date_notice": notice} if notice else {}),
                "source_refs": refs, "diagnostics": allowed, "rejected_evidence": rejected,
                "retrieval_limit": limit,
                "coverage": "retrieved_passages_only_not_complete_corpus",
                "semantic_support": "not_verified"}

    def _get_claims_matches(self, request, filters):
        result = self.service.claims_matches(
            filters, nc_ids=request.nc_ids, sc_ids=request.sc_ids, taxonomy=request.taxonomy,
            review_state=request.review_state, offset=request.offset or 0, limit=request.limit or 5,
        )
        # Full-selection category aggregates are useful in the dashboard but
        # can dwarf bounded source evidence sent to a model or MCP client.
        result.pop("category_counts", None)
        return result

    @staticmethod
    def _graph_schema():
        return {"schema_version": SCHEMA_VERSION, "node_types": deepcopy(NODE_TYPES),
                "predicates": deepcopy(PREDICATES), "identity_policy": deepcopy(IDENTITY_POLICY),
                "adapters": {"statistics": ["native", "social"], "keyword_evidence": ["native", "social"],
                             "versioned_record_detail": ["native"], "knowledge_graph": ["native"],
                             "claims2_published_matches": ["native", "social"],
                             "external_fact_check": "not_connected", "reviewed_source_attachments": "not_connected"},
                "claims_status": "historical_annotations_are_not_verified_greenwashing"}

    def _get_graph_neighborhood(self, request, filters):
        if filters.dataset == "social":
            return {"status": "unavailable", "message": "The social knowledge graph adapter is not connected."}
        filters = filters.model_copy(deep=True)
        filters.dataset = "native"
        if self._availability(filters)["status"] != "ok":
            return {"status": "unavailable", "message": "The native graph collection is not loaded."}
        if request.record_id:
            if filters.record_ids and request.record_id not in filters.record_ids:
                raise ScopeConflict("The record is outside the selected record IDs.")
            filters.record_ids = [request.record_id]
        graph = self.service.knowledge_graph(filters, offset=request.offset or 0, limit=request.limit or 5)
        return {"status": "ok", "filters": filters.model_dump(mode="json"), "graph": graph,
                "coverage": "paged_neighborhood_not_complete_graph"}
