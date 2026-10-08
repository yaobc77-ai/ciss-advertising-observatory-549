"""Read-only research tools shared by model calls and the MCP server.

The language model chooses an operation, never SQL, a URL, or a source path.
Every tool intersects its request with the trusted collection selection. Source
names identify candidates; display aliases never merge organization identities.
The combined selection groups identical company spelling across casing only,
while retaining the exact source names and dataset-specific candidate IDs.
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
from typing import Annotated, Literal, TypeAliasType
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
from .chunking import retrieval_spans
from .date_inference import BASIS_NOTE
from .entities import expand as expand_entity
from .entities import registry as entity_registry
from .evaluation_mask import evaluation_active
from .knowledge_graph import (
    IDENTITY_POLICY,
    NODE_TYPES,
    PREDICATES,
    SCHEMA_VERSION,
    build_graph,
)
from .models import Filters, validate_historical_label_scope
from .original_metadata import METADATA_FIELDS, project_record_metadata
from .social_annotations import NOTE as SOCIAL_LABEL_NOTE
from .social_annotations import SCHEME as SOCIAL_LABEL_SCHEME
from .social_annotations import STATUS as SOCIAL_LABEL_STATUS
from .social_annotations import social_annotation_details, social_state_options
from .social_source_binding import evidence_source
from .structured_queries import canonical_source_values

Name = TypeAliasType("SourceName", Annotated[str, Field(min_length=1, max_length=200)])
Names = TypeAliasType("SourceNames", Annotated[list[Name], Field(max_length=20)])
OptionalNames = TypeAliasType("OptionalSourceNames", Names | None)
RecordId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")]
Question = Annotated[str, Field(min_length=1, max_length=2000)]
NcId = Annotated[str, Field(pattern=r"^NC_[1-9][0-9]*$", max_length=100)]
ScId = Annotated[str, Field(pattern=r"^SC_[1-9][0-9]*$", max_length=100)]
_DIMENSIONS = ("publishers", "sponsors", "platforms", "accounts", "keywords", "labels", "record_ids")
_RECORD_FIELDS = ("record_id", "version_id", "body_hash", "dataset", "title", "date", "source_date", "effective_date", "date_basis",
                  "inferred_date", "inferred_tier", "publisher", "sponsor", "platform", "account", "keyword",
                  "retrievable", "url", "archive_url", "collection_scope", "count_unit", "paid_ad_status")
_AFFILIATION_BASIS = "company_affiliation_not_verified_paid_sponsor"
ENTITY_CONTEXT_BYTES = 6_000
_NOTES = (
    "Counts describe stored records in the selected collection, not every advertisement published elsewhere.",
    "Sponsor and publisher fields are exact source candidates, not independently verified business relationships.",
    "Native articles and social posts retain separate counting units.",
    "The social collection contains collected company posts, not verified paid advertisements. A confirmed unique-post admission counts one platform and canonical original URL, not source observations; source_record units without that admission must not be described as deduplicated posts. Company/account disagreements are unknown; source variants remain available in the human detail view. Account fields are source names, not verified unique channel identities. Social sponsor fields describe source-listed company affiliations, not verified paid sponsorship.",
    LABEL_NOTE,
)


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FiltersRequest(Request):
    dataset: Literal["native", "social", "all"] | None = None
    publishers: OptionalNames = None
    sponsors: OptionalNames = None
    platforms: OptionalNames = None
    accounts: OptionalNames = Field(default=None, description="Exact social channel.name values, not unique identities; '(Unknown)' means missing. Requires dataset='social'.")
    keywords: OptionalNames = None
    labels: OptionalNames = Field(default=None, description="OR over supplied historical IDs, never AND. Social IDs require dataset='social'; unverified, Unknown is not False.")
    record_ids: Annotated[list[RecordId], Field(max_length=20)] | None = None
    date_from: date | None = None
    date_to: date | None = None
    include_unknown_dates: StrictBool | None = None
    date_presence: Literal["any", "known", "missing"] | None = Field(default=None, description="Publication-date presence; missing is null/empty. Intersects active dates, never widens unknown-date exclusion.")
    include_inferred_dates: StrictBool | None = Field(default=None, description="Must match the active source/inferred date basis; omission preserves it. Results label supplemented dates.")


class ScopedRequest(Request):
    filters: FiltersRequest | None = None


class ContentMatchesRequest(ScopedRequest):
    question_id: Annotated[str, Field(pattern=r"^Q(?:0[1-9]|1[0-9]|2[0-4])$")]
    offset: Annotated[StrictInt, Field(ge=0, le=10000)] | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=20)] | None = None


class ResolveEntityRequest(ScopedRequest):
    query: Annotated[str, Field(min_length=1, max_length=200)]
    entity_type: Literal["sponsor", "publisher", "account"]
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
    paid_ad_status: Literal["verified_paid"] | None = Field(default=None, description="Use for verified-paid social-ad questions; unknown paid status returns clarification, never post counts. Omit for collected posts.")
    group_by: Literal["none", "publishers", "sponsors", "platforms", "accounts", "years", "social_historical_labels"] | None = None
    ranking: Literal["all", "highest"] | None = Field(default=None, description="Years: full distribution or every tied highest year. Unknown dates stay separate.")
    periods: Annotated[list[StatisticsPeriod], Field(min_length=2, max_length=3)] | None = Field(default=None, description="2–3 named ranges in one snapshot; inclusive endpoints, null open-ended, shared filters. Missing dates stay outside periods.")
    measure: Literal["count", "share"] | None = None
    denominator_filters: FiltersRequest | None = Field(default=None, description="Share only: named comparison group narrows active_scope; filters narrow its numerator. Omit to use the active denominator.")

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
        default=None, description="Compare 2–3 separate company/outlet scopes; each narrows one sponsor/publisher. Keep shared topic qualifiers; no target substitution."
    )


class WebGapRequest(Request):
    ticket: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class RecordRequest(ScopedRequest):
    record_id: RecordId


class RecordMetadataRequest(RecordRequest):
    fields: Annotated[list[Literal[
        "title", "publisher", "sponsor", "original_url", "publication_date",
        "collection_search_term", "disclosure_language", "disclosure_location",
    ]], Field(min_length=1, max_length=len(METADATA_FIELDS),
              description="Only the original fields requested by the user, together in one read. Stored disclosure wording needs disclosure_language; request disclosure_location only for location. Omit only when all original fields are requested.")] | None = None


class FindRecordsRequest(ScopedRequest):
    title: Annotated[str, Field(min_length=1, max_length=500)]
    limit: Annotated[StrictInt, Field(ge=1, le=10)] | None = None

    @model_validator(mode="after")
    def nonblank_title(self):
        if not self.title.strip():
            raise ValueError("A literal nonblank stored title is required")
        return self


class RecordTextRequest(RecordRequest):
    source_observation_id: Annotated[str, Field(pattern=r"^junkipedia:[0-9]{1,30}$", description="Saved observation ID; required for differing text versions.")] | None = None
    body_start: Annotated[StrictInt, Field(ge=0, le=10000000)] | None = None
    body_limit: Annotated[StrictInt, Field(ge=1, le=12000)] | None = None


class MediaEvidenceRequest(ScopedRequest):
    query: Question
    media_types: Annotated[list[Literal["text", "image", "video"]], Field(min_length=1, max_length=3)] | None = None
    limit: Annotated[StrictInt, Field(ge=1, le=5)] | None = None


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
    "get_media_evidence": (MediaEvidenceRequest,
        "Read separately indexed text/image/video evidence from an operator-mounted frozen local bundle, then merge by current record and version. Defaults to image and video, up to five candidates per route and five evidence units total. Current collection filters and retrievable permission remain binding; paused records cannot be read through media. Returns missing_material when requested media is absent, no_match when supplied media has no text match, not_configured when no bundle is mounted, unavailable when validation fails. OCR, visual descriptions and subtitles retain distinct source types and locations; they are never original article quotes or proof of factual truth. No paths, URLs, OCR, transcription, model calls or writes are accepted."),
    "get_content_matches": (ContentMatchesRequest,
        "Read reviewed native decisions for one fixed client question Q01–Q24. Counts cover the full current scope as relevant/not_relevant/unknown, separately from pagination. Only classification_complete=true establishes completeness; pending/stale/missing/unreviewed is unknown. No classification, approval, path or write; search and CLAIMS2 assignments cannot substitute."),
    "resolve_entity": (ResolveEntityRequest,
        "Resolve a sponsor, publisher or social account against actual source names. Account resolution requires a social selection and keeps exact channel.name values without publisher aliases. Returns candidates, not identity merges; ask for clarification when ambiguous."),
    "record_statistics": (StatisticsRequest,
        "Exact SQL counts of selected stored records, including unsearchable bodies; never count retrieved passages. Group by publishers/sponsors/platforms/accounts/years; accounts require social scope (native blanks are not Unknown accounts). Years highest returns every tie; Unknown dates stay separate. periods compares 2–3 inclusive named ranges with shared filters in one snapshot, not one total. Missing-date requests use date_presence='missing'. Share requires group_by='none': default denominator is active_scope, filters narrow numerator; a named comparison group goes in denominator_filters, with target filters inside it. Clarify ambiguous denominators; separate native/social units; zero denominator is undefined. Preserve source/inferred date_basis. Historical social distribution requires explicit social scope, group_by='social_historical_labels', count, ranking=all, no periods/denominator. It returns all 13 codes' source True/False/Unknown, binding coverage and full denominator; OR filters, no AND, no reviewed greenwashing/content findings."),
    "search_records": (SearchRequest,
        "Free keyword retrieval of bounded source passages within the collection selection. Use for article content, never corpus totals or factual verification. Each passage carries date_basis (source, inferred:<method> or missing); when citing a passage whose date is supplemented, say so."),
    "find_records": (FindRecordsRequest,
        "Resolve an article or post title to current exact record IDs within all active collection filters. Prefer case-insensitive exact display/original metadata titles; if absent, return bounded literal substring candidates without body search. SQL wildcard characters are literal. Multiple candidates return ambiguous and require selection; never choose the first. not_found means no matching stored title, not absence on the web. Each candidate binds record/version/body-hash. No classification, arbitrary URL, model call or write."),
    "get_record": (RecordTextRequest,
        "Read a bounded unchanged native article or collected company post by exact record ID within all active filters, with version/body-hash and character positions. Social records paused for retrieval return metadata and review_required, never source text. Company affiliation does not establish paid sponsorship."),
    "get_record_metadata": (RecordMetadataRequest,
        "Read stored original title/publisher/sponsor/URL/publication date/collection search term/disclosure language and location by an already selected exact record ID, within all current filters. Use find_records first when only a title is given; never guess an ID. Request only the user's fields, together in one read: disclosure wording needs disclosure_language, while disclosure_location is separate and needed only when requested. Omit fields only for an explicit all-fields request. Returns exact original cells with origin, payload-field location and current record/version/body-hash; normalized display values are separate. Return recorded requested fields even if another requested field is unknown, and preserve each field's status. Excel metadata outranks cleaned CSV copies; conflicting original values require review. Missing/blank cells and missing row/hash provenance remain unknown/not_recorded, never substitute body search or an external page. Stored values do not establish current online truth. No arbitrary URL, source path, full raw payload, model call or write."),
    "get_record_sources": (RecordRequest,
        "Read version-bound public original/archive references for native articles or social posts, company-affiliation basis and historical annotation limitations. Social source labels distinguish old True/False values from unknown; they are unverified, separate from native labels and never reviewed greenwashing findings. No file access or arbitrary URL fetching."),
    "get_graph_schema": (Request,
        "Read graph node/predicate definitions, source identity policy and adapter limitations. The graph expresses recorded provenance, not verified greenwashing."),
    "get_graph_neighborhood": (GraphRequest,
        "Read a paged typed source graph for at most five selected native articles. This bounded neighborhood is not the complete graph or a basis for corpus totals."),
    "get_claims_matches": (ClaimsRequest,
        "Read published CLAIMS2 NC_/SC_ assignments, definitions, review states and exact version-bound quotes. IDs/taxonomy fingerprint/review state narrow current scope. Counts are published positives only; unmatched is not negative. Assignments do not verify greenwashing or factual truth. No classification or writes."),
}


class ScopeConflict(ValueError):
    """An attempted narrowing has no valid intersection with trusted scope."""


def strict_schema(schema):
    """Responses strict schemas require every optional property to be nullable."""
    schema = deepcopy(schema)

    def walk(node):
        if not isinstance(node, dict):
            return
        node.pop("default", None)
        # Visit schema nodes only. A properties/$defs mapping is a namespace:
        # its keys can legitimately be 'title', 'default' or 'properties'.
        node.pop("title", None)
        if node.get("type") == "object" or isinstance(node.get("properties"), dict):
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}))
        for key in ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas"):
            children = node.get(key)
            if isinstance(children, dict):
                for child in children.values():
                    walk(child)
        for key in ("items", "additionalProperties", "unevaluatedProperties", "contains",
                    "propertyNames", "not", "if", "then", "else"):
            walk(node.get(key))
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    walk(child)

    walk(schema)
    return schema


def _filter_policy():
    """Shared planner guidance; MCP schemas retain their field descriptions."""
    return {
        "accounts": "Exact channel.name, not identity; '(Unknown)' is missing; social only.",
        "labels": "OR over supplied historical IDs, never AND; social IDs require social scope. Unreviewed; Unknown is not False.",
        "date_presence": "any/known/missing; missing is null/empty. Intersect active dates, never widen unknown-date exclusion.",
        "include_inferred_dates": "Must match active date basis; omission preserves it; supplemented dates are labeled.",
    }


def _planner_schema(model):
    schema = strict_schema(model.model_json_schema())
    filters = schema.get("$defs", {}).get("FiltersRequest", {})
    for item in filters.get("properties", {}).values():
        item.pop("description", None)
    return schema


def _bounded_entity_context(result, selected):
    """Bound optional namespace hints, never the executor's real source scope."""
    result["namespace_lookup"] = (
        "This is a bounded source-name directory, not record counts or absence evidence. "
        "Use resolve_entity for omitted or ambiguous names; tools validate complete real facets."
    )

    def size():
        # Account for the extra escaping when context is embedded in the user
        # content string and the entire model input is serialized again.
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
        return len(json.dumps(encoded, ensure_ascii=False).encode("utf-8"))

    # Accounts are usually the largest directory. Keep explicitly selected
    # entries and, where space permits, a candidate in every dimension. Remove
    # optional tails deterministically, without guessing a query-specific name.
    for dimension in ("accounts", "sponsors", "publishers"):
        entries = result[dimension]
        trusted = set(getattr(selected, dimension))
        for index in range(len(entries) - 1, -1, -1):
            if size() <= ENTITY_CONTEXT_BYTES or len(entries) <= 1:
                break
            entry = entries[index]
            if trusted.intersection(entry.get("source_variants", [entry["value"]])):
                continue
            entries.pop(index)
            result["truncated"] = True
        coverage = result["namespace_coverage"][dimension]
        coverage["shown"] = len(entries)
        coverage["omitted"] = coverage["available"] - len(entries)
    omitted_hints = 0
    while size() > ENTITY_CONTEXT_BYTES and result["ambiguity_hints"]:
        # Runtime canonical-name checks and resolve_entity still see every
        # source candidate, even when an optional ambiguity hint is omitted.
        result["ambiguity_hints"].pop()
        omitted_hints += 1
        result["truncated"] = True
    if omitted_hints:
        result["ambiguity_hints_omitted"] = omitted_hints
    # A single very long optional source name can consume the remaining space.
    # Its dimension's coverage and lookup instruction still remain visible.
    for dimension in ("accounts", "sponsors", "publishers"):
        entries = result[dimension]
        trusted = set(getattr(selected, dimension))
        if (size() > ENTITY_CONTEXT_BYTES and len(entries) == 1
                and not trusted.intersection(entries[0].get("source_variants", [entries[0]["value"]]))):
            entries.clear()
            result["truncated"] = True
            result["namespace_coverage"][dimension].update(shown=0,
                omitted=result["namespace_coverage"][dimension]["available"])
    # A pathological explicit selection can exceed this soft namespace budget;
    # keep its identities and let the unchanged full-input guard reject it.
    if size() > ENTITY_CONTEXT_BYTES:
        result["explicit_context_over_budget"] = True
    return result


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


def _source_spelling_groups(values):
    """Group only strip/casefold equality, preserving every exact source value."""
    groups = {}
    for value in values:
        if isinstance(value, str) and value.strip():
            groups.setdefault(value.strip().casefold(), set()).add(value)
    return [sorted(variants) for _key, variants in sorted(groups.items())]


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
                 "parameters": _planner_schema(model), "strict": True}
                for name, (model, description) in TOOLS.items()
                # Web content answers read fixed media internally after search_records.
                # Direct media results remain a separate MCP read, rather than a
                # terminal route for the web planner; tool counts remain explicit.
                if name != "get_media_evidence"
                and (name != "get_content_matches" or evaluation_active())]

    def mcp_definitions(self):
        return [{"name": name, "description": description,
                 "inputSchema": model.model_json_schema()}
                for name, (model, description) in TOOLS.items()
                if name != "get_content_matches" or evaluation_active()]

    def statistics_validation_context(self):
        """Complete server-only source namespaces for checking omitted scope.

        Active filters and public context size limits must not hide a named
        source that the question requested. This context is never a tool result
        or model payload; execution still intersects the trusted selection.
        """
        facets = self.service.facets(self._base.dataset)
        result = {}
        for dimension in ("publishers", "sponsors", "accounts"):
            values = [value for value in facets.get(dimension, [])
                      if isinstance(value, str) and value and value != "(Unknown)"]
            if dimension == "sponsors" and self._base.dataset == "all":
                result[dimension] = [
                    {"value": variants[0], "display": sponsor_display(variants[0]),
                     "source_variants": variants}
                    for variants in _source_spelling_groups(values)
                ]
            else:
                result[dimension] = [{"value": value, "display": sponsor_display(value)
                                      if dimension == "sponsors" else value}
                                     for value in values]
        return result

    def entity_context(self, limit=200):
        """Small public namespace context; unknown names still fail at execution."""
        limit = max(1, min(int(limit), 200))
        facets = self.service.facets(self._base.dataset)
        result = {"identity_policy": IDENTITY_POLICY, "truncated": False,
                  "ambiguity_hints": [], "namespace_coverage": {},
                  "filter_policy": _filter_policy()}
        seen_hints = set()
        for dimension in ("publishers", "sponsors", "accounts"):
            values = list(facets.get(dimension, []))
            if getattr(self._base, dimension):
                values = [value for value in values if value in getattr(self._base, dimension)]
            values = [value for value in values if value and value != "(Unknown)"]
            dimension_limit = len(values) if getattr(self._base, dimension) else limit
            combined_companies = dimension == "sponsors" and self._base.dataset == "all"
            if combined_companies:
                groups = _source_spelling_groups(values)
                result[dimension] = [{"value": variants[0], "display": sponsor_display(variants[0]),
                                      "source_variants": variants}
                                     for variants in groups[:dimension_limit]]
                result["source_variant_policy"] = (
                    "All: group only strip/casefold-identical company spellings; source_variants stay within active filters. "
                    "Different aliases and candidate IDs remain separate."
                )
                result["truncated"] |= len(groups) > dimension_limit
            else:
                result[dimension] = [{"value": value, "display": sponsor_display(value)
                                      if dimension == "sponsors" else value}
                                     for value in values[:dimension_limit]]
                result["truncated"] |= len(values) > dimension_limit
            available = len(groups) if combined_companies else len(values)
            result["namespace_coverage"][dimension] = {"available": available,
                "shown": len(result[dimension]), "omitted": available - len(result[dimension])}
            if dimension == "accounts":
                continue  # Account source names never borrow outlet aliases.
            for value in values[:limit]:
                query = _normal(value).removeprefix("the ")
                related = [candidate for candidate in values if _related_name(query, {
                    _normal(candidate), _normal(candidate).removeprefix("the ")})]
                if combined_companies:
                    related = [variants[0] for variants in _source_spelling_groups(related)]
                if len(related) > 1 and len(result["ambiguity_hints"]) < 30:
                    hint_key = (dimension, query, tuple(sorted(related)))
                    if hint_key in seen_hints:
                        continue
                    seen_hints.add(hint_key)
                    result["ambiguity_hints"].append({"field": dimension, "query": value,
                        "source_candidates": related[:10], "candidate_count": len(related),
                        "instruction": "Resolve or clarify; never silently select or merge related source candidates. Exact active filters still choose a source value."})
        if self._base.dataset in {"social", "all"}:
            result["social_historical_labels"] = {
                "dataset": "social", "scheme": SOCIAL_LABEL_SCHEME, "status": SOCIAL_LABEL_STATUS,
                # Keep every allowed ID explicit; human state definitions are
                # shared once rather than repeated for all 13 source codes.
                "options": [{"value": item["value"]} for item in social_state_options()],
                "states": {"source_true": "Source export recorded True",
                           "source_false": "Source export recorded False",
                           "unknown": "No usable annotation bound to this stored text; not False"},
                "required_scope": "Explicitly select filters.dataset='social' within an all selection",
                "match": "any_selected_state_OR_not_AND",
                "note": SOCIAL_LABEL_NOTE,
            }
        return _bounded_entity_context(result, self._base)

    def narrow(self, requested: FiltersRequest | None, *, base: Filters | None = None):
        # ``base`` lets a share numerator narrow its own trusted denominator.
        filters = base.model_copy(deep=True) if base is not None else self.base_filters
        if requested is None:
            if filters.accounts and filters.dataset != "social":
                raise ScopeConflict("Account filters require the social collection.")
            self._label_scope(filters)
            return filters
        updates = requested.model_dump(exclude_none=True)
        dataset = updates.pop("dataset", None)
        if dataset and dataset != "all":
            if filters.dataset not in ("all", dataset):
                raise ScopeConflict("The requested collection is outside the current selection.")
            if filters.dataset == "all" and filters.sponsors:
                # The union company's source spellings may occur in only one
                # collection. Narrowing datasets keeps only that collection's
                # exact spellings, never replacing them with another identity.
                known = set(self.service.facets(dataset).get("sponsors", []))
                filters.sponsors = [value for value in filters.sponsors if value in known]
                if not filters.sponsors:
                    raise ScopeConflict("The selected company source names are not present in that collection.")
            filters.dataset = dataset
        # Asking for all never broadens a native/social selection.
        if (filters.accounts or updates.get("accounts")) and filters.dataset != "social":
            raise ScopeConflict("Account filters require the social collection. Explicitly select filters.dataset='social' within an all selection.")
        for dimension in _DIMENSIONS:
            values = updates.pop(dimension, None)
            if not values:
                continue
            if dimension in ("publishers", "sponsors"):
                if dimension == "sponsors" and filters.dataset == "all":
                    values = self._combined_sponsors(values, selected=filters.sponsors)
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
        if inferred is not None and inferred != filters.include_inferred_dates:
            raise ScopeConflict("The requested publication-date basis differs from the active selection. Change the collection's source-only or supplemented-date selection first.")
        include_unknown = updates.pop("include_unknown_dates", None)
        if lower or upper:
            filters.include_unknown_dates = False
        elif include_unknown is not None:
            filters.include_unknown_dates = filters.include_unknown_dates and include_unknown
        if filters.date_presence == "missing" and not filters.include_unknown_dates:
            raise ScopeConflict("The active selection excludes missing publication dates; their count cannot be read by widening the date filter.")
        self._label_scope(filters)
        return filters

    def _combined_sponsors(self, values, *, selected):
        """Expand same-spelling company requests within the trusted union scope."""
        known = set()
        for dataset in ("native", "social"):
            known.update(value for value in self.service.facets(dataset).get("sponsors", []) if isinstance(value, str))
        result = []
        for value in values:
            matches = sorted(candidate for candidate in known if candidate.strip().casefold() == value.strip().casefold())
            if matches:
                matches = [candidate for candidate in matches if not selected or candidate in selected]
                if not matches:
                    raise ScopeConflict("The requested company spelling is outside the active filters.")
                result.extend(matches)
            else:
                # Existing unique display-alias resolution and unknown-name
                # rejection still apply; no approximate name grouping occurs.
                result.append(value)
        return list(dict.fromkeys(result))

    @staticmethod
    def _label_scope(filters):
        try:
            validate_historical_label_scope(filters.dataset, filters.labels)
        except ValueError as exc:
            raise ScopeConflict(str(exc)) from None

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
            if value in result:
                continue  # already included by an earlier organization expansion
            # A registry organization expands to every spelling it lists within
            # this scope, so native "exxonmobil" and social "ExxonMobil" are
            # filtered together; the mapping and registry version are recorded.
            entity, spellings = expand_entity(value, dimension, known)
            if selected and spellings:
                # An explicit active selection is never widened by the registry.
                spellings = [spelling for spelling in spellings if spelling in selected]
            if entity is not None and spellings:
                if spellings != [value]:
                    item = {"field": dimension, "requested": value, "source_values": spellings,
                            "organization": entity.id, "organization_type": entity.type,
                            "review_status": entity.review_status, "registry_version": entity_registry().version}
                    if item not in self.alias_resolutions:
                        self.alias_resolutions.append(item)
                result.extend(spelling for spelling in spellings if spelling not in result)
                continue
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
                raise ScopeConflict("A requested source name, account or label is not in this collection. Check the exact value or adjust the selection.")

    def _context(self, name, filters=None):
        selected = filters or self._base
        notes = [*_NOTES[:-1], SOCIAL_LABEL_NOTE if selected.dataset == "social"
                 else "Native historical labels: " + LABEL_NOTE]
        if selected.dataset == "all":
            notes.append(SOCIAL_LABEL_NOTE)
        context = {"tool": name, "filters": selected.model_dump(mode="json"), "scope_notes": notes}
        if self.alias_resolutions:
            context["alias_resolutions"] = [dict(item) for item in self.alias_resolutions]
        return context

    def _availability(self, filters):
        health = self.service.health()
        if health.get("status") != "ok":
            return {"status": "unavailable", "message": "The collection database is unavailable.",
                    "data_version": "unavailable"}
        datasets = ("native", "social") if filters.dataset == "all" else (filters.dataset,)
        counts = health.get("countable_record_counts", health.get("record_counts", {}))
        missing = [dataset for dataset in datasets if not counts.get(dataset)]
        return {"status": "unavailable" if len(missing) == len(datasets) else "ok",
                "message": "No selected collection is loaded." if len(missing) == len(datasets) else "",
                "data_version": health.get("data_version", "unavailable"),
                "statistics_version": health.get("statistics_version"),
                "missing_datasets": missing}

    def call(self, name, arguments):
        if name == "get_content_matches" and not evaluation_active():
            return {"status": "invalid_request", "tool": "unknown",
                    "message": "This read is available only in the isolated local evaluation context."}
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
            if name == "record_statistics" and request.paid_ad_status == "verified_paid":
                return {**context, "status": "clarify", "reason": "paid_ad_evidence_missing",
                        "paid_ad_status": "unknown", "review_required": True,
                        "available_scope": "collected_company_posts",
                        "message": "Verified paid social-ad status is not established in this collection. No paid-ad count is available. You can explore collected company posts and their sources; their counts are not a substitute for verified paid advertising."}
            if name == "get_graph_schema":
                return {**context, "status": "ok", **self._graph_schema()}
            availability = self._availability(filters)
            if availability["status"] != "ok":
                return {**context, **availability}
            if filters.dataset in availability["missing_datasets"]:
                return {**context, **availability, "status": "unavailable",
                        "message": "This collection has not been loaded; zero is not an established advertising count."}
            self._known_filters(filters)
            label_distribution = (name == "record_statistics"
                                  and getattr(request, "group_by", None) == "social_historical_labels")
            if label_distribution:
                self._social_distribution_scope(request, filters)
            label_target = bool(filters.dataset == "social" and filters.labels)
            denominator = getattr(request, "denominator_filters", None)
            label_denominator = bool(denominator and denominator.labels and any(
                value == SOCIAL_LABEL_SCHEME or value.startswith(SOCIAL_LABEL_SCHEME + ":")
                for value in denominator.labels))
            if label_denominator:
                self.narrow(denominator)  # Reject a forbidden comparison group before reading its state.
            label_comparisons = False
            for comparison in getattr(request, "comparison_scopes", None) or []:
                scoped = self.narrow(comparison.filters, base=filters)
                label_comparisons |= bool(scoped.dataset == "social" and scoped.labels)
            tracked_scope = None
            source_state_version = None
            if label_distribution or label_target or label_denominator or label_comparisons:
                # A percentage may narrow its own comparison group inside the
                # tool. Guard the trusted social selection without clearing any
                # active filter; this also catches members entering or leaving
                # that selected label state without a new body version.
                tracked_scope = (filters if label_distribution else
                                 self._base.model_copy(update={"dataset": "social"}))
                self._label_scope(tracked_scope)
                source_state_version = self._source_state_version(tracked_scope)
            result = getattr(self, "_" + name)(request, filters)
            final_health = self.service.health()
            if (final_health.get("status") != "ok" or final_health.get("data_version") != availability["data_version"]):
                return {**context, "status": "unavailable", "message": "Data changed during the read; retry against a current version."}
            if (name == "record_statistics"
                    and final_health.get("statistics_version") != availability.get("statistics_version")):
                return {**context, "status": "unavailable", "message":
                        "Statistical records, annotations or date estimates changed during the read. Refresh the selection and try again."}
            if tracked_scope is not None:
                if (self._source_state_version(tracked_scope) != source_state_version
                        or label_distribution and result.get("source_state_version") != source_state_version):
                    return {**context, "status": "unavailable",
                            "message": "Historical source states changed during the read. Refresh the selection and try again."}
                result["historical_source_state_guard"] = {
                    "filters": tracked_scope.model_dump(mode="json"),
                    "source_state_version": source_state_version,
                }
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

    def _get_media_evidence(self, request, filters):
        return self.service.media_evidence(
            filters, query=request.query, media_types=request.media_types,
            limit=request.limit or 5,
        )

    def _resolve_entity(self, request, filters):
        if request.entity_type == "account" and filters.dataset != "social":
            raise ScopeConflict("Account resolution requires the social collection.")
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
                if request.entity_type == "account":
                    exact = request.query == value
                    if exact or request.query.casefold() in value.casefold():
                        candidates.append({"entity_id": _candidate_id(dataset, "account", value),
                            "dataset": dataset, "source_field": "account", "source_value": value,
                            "display_name": value, "match": "exact_or_display" if exact else "partial",
                            "identity_status": "source_candidate_not_resolved"})
                    continue
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
        organizations = set()
        if request.entity_type != "account":
            for candidate in candidates:
                owner = entity_registry().owner(candidate["source_value"], request.entity_type)
                candidate["organization"] = owner.public() if owner else None
                organizations.add(owner.id if owner else None)
        one_organization = len(candidates) > 1 and len(organizations) == 1 and None not in organizations
        spelling_groups = _source_spelling_groups(candidate["source_value"] for candidate in candidates)
        combined_single_group = filters.dataset == "all" and request.entity_type == "sponsor" and len(spelling_groups) == 1
        unambiguous = len(candidates) == 1 or combined_single_group or one_organization
        result = {"status": "ok" if unambiguous else "clarify",
                "candidates": candidates[:limit], "candidate_count": len(candidates),
                "truncated": len(candidates) > limit,
                "message": ("All candidates are spellings of one registry organization; filter by its name to include every spelling."
                            if one_organization else "Use the exact source value in filters.") if unambiguous
                else "Choose a source candidate; ambiguous names are never merged." if candidates
                else "No matching source candidate was found. Clarify the name or collection."}
        if filters.dataset == "all" and request.entity_type == "sponsor":
            result["source_variants"] = spelling_groups
            result["source_variant_policy"] = "Only identical strip/casefold spelling groups; original source values and candidate IDs remain separate."
            if combined_single_group:
                result["message"] = "Use the identical-spelling company group in combined filters; its exact source variants and candidate IDs remain distinct."
        return result

    def _source_state_version(self, filters):
        version = self.service.db.social_source_state_version(filters)
        if not isinstance(version, str) or re.fullmatch(r"[0-9a-f]{64}", version) is None:
            raise ValueError("The historical social source version cannot be verified")
        return version

    @staticmethod
    def _social_distribution_scope(request, filters):
        if filters.dataset != "social":
            raise ScopeConflict("Historical social label distributions require the social collection.")
        if (request.measure not in (None, "count") or request.ranking not in (None, "all")
                or request.periods or request.denominator_filters is not None):
            raise ScopeConflict("Historical social label distributions use count, all codes, and no periods or comparison denominator.")

    def _record_statistics(self, request, filters):
        group_by = request.group_by or "none"
        if group_by == "social_historical_labels":
            self._social_distribution_scope(request, filters)
            dashboard = self.service.dashboard(filters, offset=0, limit=1)
            supplied = dashboard["social_historical_labels"]
            distribution = {key: supplied.get(key) for key in (
                "scheme", "status", "note", "total", "valid_annotation_records",
                "unknown_annotation_records", "source_state_version")}
            distribution["items"] = [{key: item.get(key) for key in (
                "key", "label", "level", "source_true", "source_false", "unknown")}
                for item in supplied["items"]]
            result = {"status": "ok", "kind": "social_historical_labels", "method": "database",
                    "group_by": group_by, "distribution": distribution,
                    "source_state_version": distribution["source_state_version"],
                    "collections": [{"dataset": "social", "total": distribution["total"]}],
                    "groups": [], "records": [{key: row.get(key) for key in _RECORD_FIELDS}
                        for row in dashboard["page"]["rows"]],
                    "coverage": "full_current_filtered_social_source_states"}
            self.service._social_label_statistics_answer(
                {**result, "filters": filters.model_dump(mode="json")}, base_filters=self._base)
            return result
        if group_by == "accounts" and filters.dataset != "social":
            raise ScopeConflict("Account grouping requires the social collection.")
        if group_by == "years" or request.periods:
            return self._complete_statistics(request, filters)
        if request.measure == "share" and group_by != "none":
            raise ScopeConflict("A percentage compares target records with the current selection. Use no grouping, or ask for a count distribution separately.")
        kind = "share" if request.measure == "share" else {
            "none": "count", "publishers": "list_publishers", "sponsors": "list_sponsors",
            "platforms": "list_platforms", "accounts": "list_accounts"}[group_by]
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
        plan = SimpleNamespace(kind=kind, filters=filters,
                               scope_notes=self._context("record_statistics", filters)["scope_notes"],
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
        result["scope_notes"] = self._context("record_statistics", filters)["scope_notes"]
        if missing:
            result["scope_notes"].append("Unloaded collections are omitted, not reported as zero advertisements.")
        used = sum(item.get("inferred_dates", 0) for item in result["collections"])
        result["date_inference"] = {"enabled": filters.include_inferred_dates,
            "used": used, "tiers": result.pop("inferred_tiers", {}),
            "note": BASIS_NOTE if used else "Counts use source publication dates only." if not filters.include_inferred_dates else "No supplemented dates were used in this selection."}
        return {"status": "ok", **result}

    def _row(self, record_id, filters):
        if filters.record_ids and record_id not in filters.record_ids:
            raise ScopeConflict("The record is outside the selected record IDs.")
        row = self.service.db.versioned_record(filters, record_id)
        if (not row or row.get("record_id") != record_id
                or row.get("dataset") not in {"native", "social"}
                or filters.dataset not in ("all", row.get("dataset"))
                or filters.accounts and (row.get("account") or "(Unknown)") not in filters.accounts
                or not isinstance(row.get("version_id"), str) or not row["version_id"]):
            raise ScopeConflict("The record does not match the current collection filters.")
        return row

    def _record_projection(self, row):
        result = {key: row.get(key) for key in _RECORD_FIELDS}
        if row.get("dataset") == "social":
            basis = _AFFILIATION_BASIS if row.get("sponsor_basis") == _AFFILIATION_BASIS else "not_recorded"
            result.update(sponsor_basis=basis,
                          company_affiliation=(row.get("sponsor") or "") if basis == _AFFILIATION_BASIS else "",
                          relation_note="A source-listed company affiliation is not proof of paid sponsorship.")
            admission = row.get("social_admission")
            allowed_conflicts = {"body", "sponsor", "account", "published_at", "platform", "historical_labels"}
            if (isinstance(admission, dict)
                    and admission.get("scheme") == "collected-company-posts-unique-url-v1"
                    and admission.get("scope") == "collected_company_posts"
                    and admission.get("count_unit") == "platform_canonical_original_post_url"
                    and admission.get("paid_ad_status") == "unknown"
                    and type(admission.get("member_count")) is int and admission["member_count"] > 0
                    and isinstance(admission.get("conflicting_fields"), list)
                    and all(isinstance(field, str) and field in allowed_conflicts for field in admission["conflicting_fields"])
                    and admission.get("retrieval_status") in {
                        "enabled_source_post_text_only", "paused_body_disagreement", "paused_text_quality",
                        "paused_sentence_source_validation", "enabled_source_observations"}):
                result["social_admission"] = {key: admission[key] for key in (
                    "scope", "count_unit", "paid_ad_status", "member_count", "conflicting_fields", "retrieval_status")}
                result["social_admission"]["source_variants_access"] = "human_record_detail_only"
            if row.get("social_source_observations"):
                result["source_observation_retrieval"] = {
                    "status": "enabled_source_observations", "semantic_completeness_verified": False,
                    "observations": len(row["social_source_observations"]),
                }
        return self.service._public_rows([result])[0]

    @staticmethod
    def _observation_directory(row):
        return [{key: source[key] for key in (
            "source_observation_id", "source_version_id", "source_body_hash", "quality_codes",
            "source_conflicts", "observation_count")} | {"characters": len(source["body"])}
                for source in row.get("social_source_observations", [])]

    def _get_record(self, request, filters):
        row = self._row(request.record_id, filters)
        if row.get("dataset") == "social" and row.get("retrievable") is not True:
            return {"status": "ok", "record": self._record_projection(row),
                    "text_status": "paused", "review_required": True, "source_refs": [],
                    "message": "This collected company post is included in counts, but its text is paused for review. No source excerpt is supplied to the model. Inspect all observations in the human record detail view."}
        body = row.get("body") or ""
        source = None
        observations = row.get("social_source_observations", [])
        if observations:
            if request.source_observation_id:
                source = next((item for item in observations
                               if item["source_observation_id"] == request.source_observation_id), None)
                if source is None:
                    raise ScopeConflict("The requested observation does not belong to this current post.")
            elif len({item["source_body_hash"] for item in observations}) > 1:
                return {"status": "ok", "record": self._record_projection(row),
                        "text_status": "source_observation_selection", "review_required": True,
                        "source_observations": self._observation_directory(row), "source_refs": [],
                        "message": "This post has differing saved text observations. Select a source_observation_id to read its exact text; the display observation is not an adjudicated post."}
            else:
                source = observations[0]
            body = source["body"]
        elif request.source_observation_id:
            raise ScopeConflict("No matching saved observation is available for this current source.")
        if not isinstance(body, str):
            body = ""
        start = request.body_start or 0
        if start > len(body):
            raise ScopeConflict("The requested text position is beyond the stored source text.")
        end = min(len(body), start + (request.body_limit or 12000))
        digest = hashlib.sha256(body.encode()).hexdigest()
        source_hash = source["source_body_hash"] if source else row.get("body_hash")
        matched = digest == source_hash
        if not matched:
            return {"status": "unavailable", "message": "The stored text does not match its body hash; no exact source excerpt is published."}
        return {"status": "ok", "record": self._record_projection(row),
                "body": {"text": body[start:end], "start": start, "end": end,
                         "total_characters": len(body), "truncated": start > 0 or end < len(body),
                         "body_hash": source_hash if matched else "",
                         "hash_status": "matched" if matched else "missing_or_mismatched",
                         "completeness": "not_established", "semantic_support": "not_verified",
                         **({key: source[key] for key in ("source_observation_id", "source_version_id", "source_body_hash",
                                                        "source_conflicts", "quality_codes", "observation_count")} if source else {})},
                "source_refs": [{"record_id": row["record_id"], "version_id": row["version_id"],
                                 "body_hash": source_hash if matched else "",
                                 "start": start, "end": end,
                                 **({key: source[key] for key in ("source_observation_id", "source_version_id", "source_body_hash")} if source else {})}]}

    def _get_record_metadata(self, request, filters):
        if filters.record_ids and request.record_id not in filters.record_ids:
            raise ScopeConflict("The record is outside the selected record IDs.")
        row = self.service.db.original_record_metadata(filters, request.record_id)
        if (not row or row.get("record_id") != request.record_id
                or row.get("dataset") not in {"native", "social"}
                or filters.dataset not in ("all", row.get("dataset"))
                or filters.accounts and (row.get("account") or "(Unknown)") not in filters.accounts):
            raise ScopeConflict("The record does not match the current collection filters.")

        def public_url(value):
            return self.service._public_rows([{"url": value}])[0]["url"]

        return project_record_metadata(row, request.fields, public_url=public_url,
                                       links_enabled=self.service.settings.show_source_links)

    def _find_records(self, request, filters):
        result = self.service.db.find_records(filters, request.title, request.limit or 10)
        rows = result.get("rows") or []
        total = result.get("total_candidates", 0)
        if type(total) is not int or total < len(rows):
            raise ValueError("Invalid title candidate count")
        records = []
        for row in rows:
            if (row.get("dataset") not in {"native", "social"}
                    or filters.dataset not in ("all", row.get("dataset"))
                    or filters.record_ids and row.get("record_id") not in filters.record_ids
                    or not row.get("version_id") or not row.get("body_hash")):
                raise ValueError("Title candidate is not bound to the selected current scope")
            records.append({**self._record_projection(row), "body_hash": row["body_hash"]})
        return {"status": "not_found" if not total else "ambiguous" if total > 1 else "ok",
                "records": records, "total_candidates": total,
                "match_type": result.get("match_type"), "truncated": total > len(records),
                "selection_required": total > 1, "title": request.title,
                "message": "Select an exact record ID before reading original metadata." if total > 1
                    else "No matching stored title in the current collection filters." if not total
                    else "A current stored title candidate is available.",
                "online_truth": "not_established"}

    def _get_record_sources(self, request, filters):
        row = self._row(request.record_id, filters)
        record = self._record_projection(row)
        if row["dataset"] == "social":
            # A post's affiliation is not a native-ad sponsorship edge. Publish
            # source references directly; do not construct a social graph here.
            artifacts, relations = [], []
            for field, label, kind, predicate in (
                ("url", "Original post reference", "web_reference", "has_source_reference"),
                ("archive_url", "Archive URL reference", "archive_reference", "has_archive_reference"),
            ):
                if not record[field]:
                    continue
                identity = json.dumps((row["record_id"], row["version_id"], field, record[field]))
                identifier = "sourceartifact:" + hashlib.sha256(identity.encode()).hexdigest()[:32]
                artifacts.append({"id": identifier, "type": "SourceArtifact", "label": label,
                    "properties": {"kind": kind, "url": record[field], "immutable_capture": False,
                                   "verification_status": "url_recorded_contents_unverified"},
                    "record_ids": [row["record_id"]]})
                relations.append({"predicate": predicate, "target": identifier,
                    "provenance": {"record_id": row["record_id"], "version_id": row["version_id"],
                                   "field": field, "status": "source_recorded_unverified"}})
            social_history = social_annotation_details(row)
            paused = row.get("retrievable") is not True
            if paused:
                # Generated old explanations can repeat source text. Keep only
                # source states and references when text is withheld.
                social_history["explanations"] = []
            annotations = [social_history] if social_history["validation_state"] == "bound" else []
            if paused:
                annotations = []
            warnings = [{"code": "social_annotations_unverified", "record_id": row["record_id"],
                "message": "Historical social labels use a separate scheme; they are not verified greenwashing findings or mapped native labels."}]
            if paused:
                warnings.append({"code": "social_source_text_paused", "record_id": row["record_id"],
                                 "message": "Source text and generated explanations are withheld from model tools. Counts still include the unique original post; source variants are available for human review."})
        else:
            graph = build_graph([row], links_enabled=self.service.settings.show_source_links)
            artifacts = [node for node in graph["nodes"] if node["type"] == "SourceArtifact"]
            annotations = [node for node in graph["nodes"] if node["type"] in ("Annotation", "Label")]
            relations = [edge for edge in graph["edges"]
                         if edge["predicate"] in ("has_source_reference", "has_archive_reference", "derived_from")]
            warnings = graph["warnings"]
        body_hash = row.get("body_hash") or ""
        hash_matched = body_hash == hashlib.sha256((row.get("body") or "").encode()).hexdigest()
        directory = self._observation_directory(row) if row.get("dataset") == "social" else []
        return {"status": "ok", "record": record,
                **({"source_observations": directory} if directory else {}),
                "source_refs": [{"record_id": row["record_id"], "version_id": row["version_id"],
                                 "body_hash": body_hash if hash_matched else "",
                                 "verification_status": "body_hash_matched" if hash_matched else "missing_or_mismatched"}],
                "source_artifacts": artifacts, "source_relations": relations,
                "annotations": annotations, "warnings": warnings,
                **({"social_historical_annotation": social_history} if row["dataset"] == "social" else {}),
                **({"text_status": "paused", "review_required": True} if row["dataset"] == "social" and row.get("retrievable") is not True else {}),
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
            outcomes = []
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
                               "keyword_passages": len(item["evidence"]), "rejected_evidence": item["rejected_evidence"],
                               "search_status": item["search_status"], "diagnostics": item["diagnostics"]})
                outcomes.append(item["search_status"])
                evidence.extend(item["evidence"])
                refs.extend(item["source_refs"])
                rejected += item["rejected_evidence"]
            if "failed" in outcomes:
                return {"status": "unavailable", "search_status": "failed", "query": request.query,
                        "evidence": [], "source_refs": [], "rejected_evidence": rejected,
                        "retrieval_groups": groups,
                        "message": "A comparison search was unavailable. No complete comparison or database-miss ticket is established.",
                        "coverage": "separate_target_retrieval_not_complete_corpus", "semantic_support": "not_verified"}
            return {"status": "ok", "query": request.query, "evidence": evidence,
                    "search_status": "complete" if all(outcome == "complete" for outcome in outcomes) else "partial",
                    "source_refs": refs, "rejected_evidence": rejected, "retrieval_groups": groups,
                    "coverage": "separate_target_retrieval_not_complete_corpus", "semantic_support": "not_verified"}
        result = self._search_one(request.query, filters, request.limit or 5)
        if (result["search_status"] == "empty"
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
            ref = {"evidence_id": item.evidence_id, "record_id": item.record_id,
                   "version_id": item.version_id, "dataset": item.dataset, "start": item.start, "end": item.end,
                   "body_hash": "", "verification_status": "adapter_unavailable"}
            try:
                row = self._row(item.record_id, filters)
            except ScopeConflict:
                rejected += 1
                continue
            if item.dataset in {"native", "social"}:
                try:
                    source = evidence_source(row, item)
                    spans = ([(0, len(source["body"]))] if item.source_observation_id else
                             retrieval_spans(source["body"], retrieval_ranges=row.get("retrieval_ranges"),
                                             retrieval_end=row.get("retrieval_end")))
                    if not any(start <= item.start < item.end <= end for start, end in spans):
                        raise ValueError("Excerpt is outside allowed source intervals")
                except (TypeError, ValueError):
                    rejected += 1
                    continue
                ref.update(body_hash=source["body_hash"], verification_status="exact_character_match")
                if item.source_observation_id:
                    ref.update({key: getattr(item, key) for key in (
                        "source_observation_id", "source_version_id", "source_body_hash")})
            else:
                rejected += 1
                continue
            # Retrieval chunks are already small; reject an unexpectedly huge
            # adapter response instead of falsifying its original offsets.
            if len(item.text) > 12000:
                rejected += 1
                continue
            evidence.append(item.model_dump(mode="json"))
            refs.append(ref)
        notice = self._attach_date_basis(evidence, filters)
        diagnostics = report.get("diagnostics", {})
        if not isinstance(diagnostics, dict):
            raise ValueError("Search coverage diagnostics must be an object")
        allowed = {key: diagnostics[key] for key in (
            "status", "operator", "configuration", "terms", "term_limit", "returned_matched_terms",
            "missing_from_results", "missing_from_scope", "term_details", "ignored_terms",
            "scope_records", "scope_chunks", "reason")
                   if key in diagnostics}
        coverage_status = diagnostics.get("status")
        indexed_scope = all(type(diagnostics.get(key)) is int and diagnostics[key] > 0
                            for key in ("scope_records", "scope_chunks"))
        if (rejected and not evidence) or coverage_status not in {"available", "partial"}:
            # Exact bound passages remain usable when English-only coverage is
            # unavailable, but an empty failed read is never a database miss.
            search_status = "partial" if evidence else "failed"
        elif coverage_status == "partial" or rejected:
            search_status = "partial"
        elif not evidence:
            search_status = "empty" if indexed_scope and not diagnostics.get("ignored_terms") else "partial"
        else:
            search_status = "complete"
        return {"status": "unavailable" if search_status == "failed" else "ok",
                "search_status": search_status, "query": query, "evidence": evidence,
                **({"date_notice": notice} if notice else {}),
                "source_refs": refs, "diagnostics": allowed, "rejected_evidence": rejected,
                "retrieval_limit": limit,
                "coverage": "retrieved_passages_only_not_complete_corpus",
                "semantic_support": "not_verified"}

    def _get_content_matches(self, request, filters):
        return self.service.content_matches(
            filters, question_id=request.question_id,
            offset=request.offset or 0, limit=request.limit or 5,
        )

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
                             "versioned_record_detail": ["native", "social"], "knowledge_graph": ["native"],
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
