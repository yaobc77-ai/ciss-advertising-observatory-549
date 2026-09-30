"""Plan bounded record-statistics questions without generated SQL or an LLM.

Entity values come from the corpus facets. Display aliases help locate a source
category, but never merge categories or establish a real-world partnership.
Unrecognized semantic questions return ``None`` for the evidence-search route.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from .analytics import sponsor_display
from .models import Filters

PlanStatus = Literal["ready", "clarify", "unsupported"]
PlanKind = Literal["count", "list_publishers", "list_sponsors"]


@dataclass(frozen=True)
class QuestionPlan:
    status: PlanStatus
    kind: PlanKind | None = None
    filters: Filters | None = None
    message: str = ""
    matched_entities: dict[str, tuple[str, ...]] = field(default_factory=dict)
    scope_notes: tuple[str, ...] = ()

    @property
    def group_by(self) -> str | None:
        return {"list_publishers": "publishers", "list_sponsors": "sponsors"}.get(self.kind)


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in value if character.isalnum())


# These are name/spelling aliases, enabled only when a corresponding facet exists.
_PUBLISHER_ALIASES = (
    ("newyorktimes", "thenewyorktimes", "nyt", "nytimes", "nytimescom", "纽约时报"),
    ("washingtonpost", "thewashingtonpost", "wapo", "washingtonpostcom", "华盛顿邮报"),
    ("wallstreetjournal", "thewallstreetjournal", "wsj", "wsjcom", "华尔街日报"),
)


def _entity_keys(value: str, field_name: str) -> set[str]:
    key = _normalize(value)
    keys = {key}
    if field_name == "sponsors":
        keys.add(_normalize(sponsor_display(value)))
    else:
        for aliases in _PUBLISHER_ALIASES:
            if key in aliases:
                keys.update(aliases)
    return keys


def _resolve_one(value: str, field_name: str, facets: dict) -> tuple[str, ...]:
    key = _normalize(value.strip(" \"'‘’“”"))
    return tuple(
        candidate
        for candidate in facets.get(field_name, [])
        if isinstance(candidate, str)
        and candidate.strip()
        and candidate != "(Unknown)"
        and key in _entity_keys(candidate, field_name)
    )


def _resolve_list(value: str, field_name: str, facets: dict) -> tuple[tuple[str, ...], str]:
    exact = _resolve_one(value, field_name, facets)
    if len(exact) == 1:
        return exact, ""
    if len(exact) > 1:
        return (), f"The name '{value}' matches multiple source categories. Select the exact filter value."
    parts = re.split(r"\s+and\s+|\s*[,&、]\s*|和|与", value, flags=re.IGNORECASE)
    if len(parts) == 1:
        return (), f"I could not identify '{value}' in the current collection's {field_name}. Select an available filter value."
    matches = []
    for part in parts:
        resolved = _resolve_one(part, field_name, facets)
        if len(resolved) != 1:
            return (), f"I could not identify '{part.strip()}' as one unambiguous {field_name} category. Select the exact filter value."
        matches.extend(resolved)
    return tuple(dict.fromkeys(matches)), ""


_YEAR_OR_DAY = r"(?:\d{4}-\d{2}-\d{2}|\d{4})"
_DATE_RANGE = re.compile(
    rf"\s+(?:from\s+({_YEAR_OR_DAY})\s+to\s+({_YEAR_OR_DAY})|"
    rf"between\s+({_YEAR_OR_DAY})\s+and\s+({_YEAR_OR_DAY})|"
    rf"in\s+({_YEAR_OR_DAY})\s*[–—-]\s*({_YEAR_OR_DAY}))$",
    re.IGNORECASE,
)
_DATE_SINGLE = re.compile(rf"\s+(?:in|during)\s+({_YEAR_OR_DAY})$", re.IGNORECASE)


def _date_endpoint(value: str, end: bool) -> date:
    if len(value) == 4:
        return date(int(value), 12 if end else 1, 31 if end else 1)
    return date.fromisoformat(value)


def _extract_dates(question: str) -> tuple[str, date | None, date | None, str]:
    match = _DATE_RANGE.search(question)
    if match:
        start, end = next(
            pair for pair in (match.groups()[0:2], match.groups()[2:4], match.groups()[4:6])
            if pair[0] is not None
        )
    else:
        match = _DATE_SINGLE.search(question)
        start = end = match.group(1) if match else None
    if not match:
        return question, None, None, ""
    try:
        lower, upper = _date_endpoint(start, False), _date_endpoint(end, True)
    except ValueError:
        return question, None, None, "Use valid calendar dates or years, for example 'in 2021' or 'from 2020 to 2022'."
    if lower > upper:
        return question, None, None, "The start date is after the end date. Please correct the date range."
    return question[:match.start()].strip(), lower, upper, ""


_COUNT = re.compile(
    r"^(?:how many|what is the (?:number|count) of)\s+"
    r"(?:(?:native|social(?: media)?)\s+)?(?:ads|advertisements|articles|records)\b(?P<tail>.*)$",
    re.IGNORECASE,
)
_GROUP_REQUEST = re.compile(
    r"^(?:which|what|list(?: all)?|show(?: me)?(?: all)?)\s+"
    r"(?:(?:fossil fuel|energy)\s+)?"
    r"(?P<group>publishers|news outlets|outlets|companies|sponsors|advertisers|organizations)\b"
    r"(?P<tail>.*)$",
    re.IGNORECASE,
)
_WORK_TAIL = re.compile(
    r"^(?:is|are|does|do|has|have)\s+(.+?)\s+"
    r"(?:working with|worked with|work with|partnered with|advertising (?:in|with)|using)$",
    re.IGNORECASE,
)
_SPONSOR_TAIL = re.compile(
    r"^(?:are |have been |were )?(?:sponsoring|sponsor|funding|advertising in)\s+"
    r"(?:native (?:ads|advertisements)\s+)?(?:at|in|on|from|with)\s+(.+)$",
    re.IGNORECASE,
)
_PUBLISHER_TAIL = re.compile(
    r"^(?:have |has )?(?:published|publish|carry|carried)\s+"
    r"(?:native (?:ads|advertisements)\s+)?(?:from|by|sponsored by|for)\s+(.+)$",
    re.IGNORECASE,
)
_CLAUSE = re.compile(r"\b(sponsored by|published by|at|from|by|in)\s+", re.IGNORECASE)
_CONTENT_CONSTRAINT = re.compile(
    r"\b(?:contain|containing|mention|mentioning|about|discuss|greenwashing|claims|themes|topics)\b|漂绿|主题|包含|提到",
    re.IGNORECASE,
)


def _parse_count(tail: str) -> tuple[list[tuple[tuple[str, ...], str]], str]:
    tail = re.sub(r"^(?:are|were)(?:\s+there)?\s+", "", tail.strip(), flags=re.IGNORECASE)
    clauses = list(_CLAUSE.finditer(tail))
    if not clauses or clauses[0].start() != 0:
        return [], "Specify a news outlet or sponsor, for example 'How many native ads are from the New York Times?'"
    result = []
    for index, clause in enumerate(clauses):
        end = clauses[index + 1].start() if index + 1 < len(clauses) else len(tail)
        entity = re.sub(r"\s+and\s*$", "", tail[clause.end():end], flags=re.IGNORECASE).strip()
        marker = clause.group(1).casefold()
        fields = ("publishers",) if marker in {"published by", "at", "in"} else (
            ("sponsors",) if marker == "sponsored by" else ("publishers", "sponsors")
        )
        result.append((fields, entity))
    return result, ""


def _parse_group(group: str, tail: str) -> tuple[PlanKind, list[tuple[tuple[str, ...], str]], str]:
    publishers = group.casefold() in {"publishers", "news outlets", "outlets"}
    kind: PlanKind = "list_publishers" if publishers else "list_sponsors"
    field_name = "sponsors" if publishers else "publishers"
    tail = tail.strip()
    match = _WORK_TAIL.fullmatch(tail)
    if match:
        return kind, [((field_name,), match.group(1))], ""
    match = (_PUBLISHER_TAIL if publishers else _SPONSOR_TAIL).fullmatch(tail)
    if match:
        return kind, [((field_name,), match.group(1))], ""
    match = re.fullmatch(r"(?:for|at|from|by|with)\s+(.+)", tail, re.IGNORECASE)
    if match:
        return kind, [((field_name,), match.group(1))], ""
    return kind, [], f"Specify the {'sponsor' if publishers else 'news outlet'} to explore."


def _parse_chinese(question: str):
    """Two explicit metadata question forms; no translation or topic inference."""
    match = re.fullmatch(r"(.+?)(?:有|发表了|发布了)(?:多少|几)(?:篇|条)?(?:原生广告|广告)", question)
    if match:
        return "count", [(('publishers', 'sponsors'), match.group(1))], ""
    match = re.fullmatch(r"(.+?)(?:与|和)(?:哪些|什么)(出版商|媒体|公司|赞助商)(?:合作|合作过)", question)
    if match:
        publishers = match.group(2) in {"出版商", "媒体"}
        return ("list_publishers" if publishers else "list_sponsors"), [(
            ("sponsors",) if publishers else ("publishers",), match.group(1)
        )], ""
    return None


def plan_question(question: str, filters: Filters, facets: dict) -> QuestionPlan | None:
    """Return a safe statistics plan, a clarification, or ``None`` for RAG.

    Only known metadata entities and explicit dates are executable. Existing
    scope is narrowed by intersection; an empty intersection is never represented
    by an empty filter list (which would broaden the database query).
    """
    question = re.sub(r"\s+", " ", question).strip().rstrip("?.!？。！").strip()
    chinese = _parse_chinese(question)
    count = _COUNT.fullmatch(question)
    grouped = _GROUP_REQUEST.fullmatch(question)
    if not count and not grouped and not chinese:
        # A broader quantitative form must not fall through to guessed counts.
        if re.match(r"^(?:how many|what is the (?:number|count) of)\b", question, re.IGNORECASE):
            return QuestionPlan("unsupported", message="Record counts support outlet, sponsor and explicit date filters. Topic or claim counts require validated annotations.")
        return None
    if _CONTENT_CONSTRAINT.search(question):
        return QuestionPlan("unsupported", message="Topic and greenwashing counts require a defined, validated annotation set. Use evidence search to inspect relevant articles.")
    question, lower, upper, error = _extract_dates(question)
    if error:
        return QuestionPlan("clarify", message=error)
    if count:
        match = _COUNT.fullmatch(question)
        kind: PlanKind = "count"
        clauses, error = _parse_count(match.group("tail"))
    elif grouped:
        match = _GROUP_REQUEST.fullmatch(question)
        kind, clauses, error = _parse_group(match.group("group"), match.group("tail"))
    else:
        kind, clauses, error = chinese
    if error:
        return QuestionPlan("clarify", kind=kind, message=error)
    narrowed = filters.model_copy(deep=True)
    if narrowed.date_from and narrowed.date_to and narrowed.date_from > narrowed.date_to:
        return QuestionPlan("clarify", kind=kind, message="The active start date is after the end date. Correct the filters first.")
    if re.search(r"\bnative\b|原生广告", question, re.IGNORECASE):
        if narrowed.dataset == "social":
            return QuestionPlan("clarify", kind=kind, message="This question asks about native ads, but the active collection is social advertising. Switch collection first.")
        narrowed.dataset = "native"
    elif re.search(r"\bsocial(?: media)?\b", question, re.IGNORECASE):
        if narrowed.dataset == "native":
            return QuestionPlan("clarify", kind=kind, message="This question asks about social ads, but the active collection is native advertising. Switch collection first.")
        narrowed.dataset = "social"
    matched_entities: dict[str, tuple[str, ...]] = {}
    for possible_fields, entity in clauses:
        results = []
        errors = []
        for field_name in possible_fields:
            values, error = _resolve_list(entity, field_name, facets)
            if values:
                results.append((field_name, values))
            else:
                errors.append(error)
        if len(results) != 1:
            message = (
                f"'{entity}' matches both outlet and sponsor categories. Specify 'published by' or 'sponsored by'."
                if results else " ".join(dict.fromkeys(errors))
            )
            return QuestionPlan("clarify", kind=kind, message=message)
        field_name, values = results[0]
        previous = matched_entities.get(field_name)
        if previous is not None:
            values = tuple(value for value in previous if value in values)
        if not values:
            return QuestionPlan("clarify", kind=kind, message="The question contains conflicting constraints for the same field. Please clarify the selection.")
        active = getattr(narrowed, field_name)
        intersection = [value for value in values if not active or value in active]
        if not intersection:
            return QuestionPlan("clarify", kind=kind, message=f"The requested {field_name} do not intersect the active filters. Adjust the selection first.")
        setattr(narrowed, field_name, intersection)
        matched_entities[field_name] = tuple(intersection)
    if lower is not None:
        narrowed.date_from = max(lower, narrowed.date_from) if narrowed.date_from else lower
        narrowed.date_to = min(upper, narrowed.date_to) if narrowed.date_to else upper
        narrowed.include_unknown_dates = False
    if narrowed.date_from and narrowed.date_to and narrowed.date_from > narrowed.date_to:
        return QuestionPlan("clarify", kind=kind, message="The requested dates do not intersect the active date filters. Adjust the date selection first.")
    notes = ["Counts use current eligible advertising records within the active filters."]
    if lower is not None:
        notes.append("Records with unknown dates are excluded from the requested date range.")
    if kind == "list_sponsors":
        notes.append("Results are source-listed sponsors and organizations; company classification has not been independently verified.")
    if kind != "count":
        notes.append("A source-listed advertising relationship does not establish a wider commercial partnership.")
    return QuestionPlan("ready", kind=kind, filters=narrowed, matched_entities=matched_entities, scope_notes=tuple(notes))
