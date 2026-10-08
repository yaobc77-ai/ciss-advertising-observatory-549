"""Conservative task cues, not a general natural-language intent classifier.

These rules prevent an observed failure: a model dropping an explicit content
predicate and returning an unrelated metadata total. They do not establish
article matches, semantic support, complete recall, or scientific consensus.
"""

from __future__ import annotations

import re
from datetime import timedelta

from .structured_queries import (
    _YEAR_OR_DAY,
    _date_endpoint,
    _entity_keys,
    canonical_source_values,
)

POLICY_VERSION = "question-task-v7"

# There is no trusted calendar reference in the tool contract. A relative
# interval therefore needs clarification, even if a model guessed endpoints.
_RELATIVE_CALENDAR = re.compile(
    r"\b(?:last|this|current|previous|next|past)\s+(?:calendar\s+)?"
    r"(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+)?"
    r"(?:years?|months?|quarters?|weeks?|days?)\b"
    r"|\b(?:today|yesterday|tomorrow|year\s+to\s+date|month\s+to\s+date|YTD|MTD)\b"
    r"|去年|今年|明年|上一年|下一年|上个月|本月|这个月|下个月|上季度|本季度|下季度"
    r"|上周|本周|这周|下周|昨天|今天|明天"
    r"|(?:过去|最近|近)\s*(?:\d+|[一二两三四五六七八九十几]+)\s*(?:年|个月|月|周|天)", re.I,
)

_ORIGINAL_SOURCE = re.compile(
    r"\b(?:excel|spreadsheet|stored|source data|original metadata|original field)\b"
    r"|原(?:Excel|表格|件|字段|始资料)|保存的|已存储的", re.IGNORECASE,
)
_METADATA_FIELD = re.compile(
    r"\b(?:title|date|url|link|publisher|sponsor|search term|keyword|disclosure|label|wording|location)\b"
    r"|标题|日期|网址|链接|媒体|赞助|搜索词|收集词|披露|位置|原文用语", re.IGNORECASE,
)
_MULTI_READ = re.compile(
    r"(?:\b(?:and|also|plus|then)\s+|[;；.!?。！？]\s*)(?:also\s+)?(?:count|list|read|show|find|compare|how\s+many|which|what)\b"
    r"|(?:并且|同时|以及|再|还要).{0,8}?(?:统计|列出|读取|展示|查看|比较|多少|哪些)", re.I,
)
_READ_REQUEST = re.compile(
    r"\b(?:count|list|read|show|find|compare|search|retrieve|locate|summari[sz]e|"
    r"describe|explain|identify|translate|interpret|give|provide|tell|how\s+many|which|what)\b"
    r"|统计|列出|读取|展示|查看|比较|多少|哪些|什么|查找|找出|寻找|检索|总结|描述|解释|提供", re.I,
)
_RETRIEVAL_REQUEST = re.compile(
    r"^\s*(?:(?:please|can you|could you|would you)\s+)?"
    r"(?:find|search(?:\s+for)?|retrieve|locate)\b|^\s*(?:请)?(?:查找|找出|寻找|检索)", re.I,
)
_SAME_READ_DISPLAY = re.compile(
    r"(?:\b(?:and|also|plus|then)\s+|[;；.!?。！？]\s*)(?:also\s+)?"
    r"(?:show|read)\s+(?:its|their)\s+(?:(?:original|saved|stored)\s+)?"
    r"(?:wording|text|passages|quotes|quotations|excerpts|content|contents)\s*[.!?。！？]?"
    r"|(?:并且|同时|以及|再|还要)\s*(?:展示|查看|列出)\s*(?:它|它们|其)(?:的)?"
    r"(?:原文|正文|文字|措辞|内容)\s*[。！？]?", re.I,
)
_DATASETS = {
    "native": re.compile(r"\bnative\s+(?:advertising|ads?|advertisements?|records?|articles?)\b|原生(?:广告|数据|记录)", re.I),
    "social": re.compile(r"\bsocial\s+(?:media|posts?|records?|advertising|ads?)\b|社交(?:媒体|帖子|记录|广告|数据)", re.I),
}


def original_metadata_question(question: str) -> bool:
    """Conservative source-field boundary, independent of customer test wording."""
    return bool(_ORIGINAL_SOURCE.search(question) and _METADATA_FIELD.search(question))


def _compound_read_request(text: str) -> bool:
    """Require a prior request; displaying retrieved text is the same read.

    This remains a conservative surface check. An anaphoric display clause is
    exempt only when it asks for the text of the retrieval and ends the request;
    new objects, source fields, collections or further operations still require
    a plan. Literal scope and metadata contracts are checked independently.
    """
    if _same_record_metadata_read(text):
        return False
    for match in _MULTI_READ.finditer(text):
        prior = text[:match.start()]
        if not _READ_REQUEST.search(prior):
            continue
        # A relative clause restricts the records of one read. Its predicate
        # must still survive the independent content/scope guards.
        if (re.match(r"\band\s+which\s+(?:are|were|have|had)\b", text[match.start():], re.I)
                and re.search(r"\b(?:that|which)\s+(?:are|were|have|had|ran|published)\b", prior, re.I)
                and not re.search(r"[;.!?。！？]", prior)):
            continue
        if (_QUANTITY.search(prior) and re.fullmatch(
                r"\band\s+show\s+(?:(?:the|its|their)\s+)?(?:breakdown|counts|totals)\s+by\s+"
                r"(?:publisher|sponsor|platform|account|year)\s*[.!?]?", text[match.start():], re.I)):
            continue  # The statistics tool returns a total and this grouping.
        if (_RETRIEVAL_REQUEST.search(prior)
                and _SAME_READ_DISPLAY.fullmatch(text[match.start():])):
            continue
        return True
    return False


def _same_record_metadata_read(text: str) -> bool:
    """Several stored fields of one explicitly located record use one read.

    This exemption needs a single literal locator and field-only clauses. A
    second record, a count or a content operation retains the plan obligation.
    """
    if not original_metadata_question(text):
        return False
    locators = re.findall(
        r"\b(?:article|record|post)\s+(?:titled|called|named|with\s+(?:the\s+)?title)\b"
        r"|\brecord\s+(?:id\s*[:=]?\s*)?(?:native|social):[\w-]+"
        r"|(?:文章|记录|帖子).{0,6}?(?:标题为|名为)", text, re.I)
    if len(locators) != 1:
        return False
    # Quoted locator titles are data, including any operation-looking words.
    field_text = _QUOTED_LITERAL.sub(" ", text)
    if len(re.findall(r"\b(?:article|record|post)\b|文章|记录|帖子", field_text, re.I)) != 1:
        return False
    if re.search(r"\b(?:count|list|compare|summari[sz]e|explain|translate|how\s+many)\b"
                 r"|统计|列出|比较|总结|解释|翻译|多少", field_text, re.I):
        return False
    if _MENTION.search(field_text) or _FRAMING.search(field_text) or _TOPIC.search(field_text):
        return False
    if re.search(r"\b(?:body|paragraphs?|passages?|contents?|main\s+text|article\s+text)\b"
                 r"|正文|段落|内容", field_text, re.I):
        return False
    clauses = re.split(r"\band\s+(?=(?:what|show|read)\b)|并且|以及|同时", field_text, flags=re.I)
    return len(clauses) > 1 and all(_METADATA_FIELD.search(clause) for clause in clauses)


def effective_dataset_scope(requested: str | None, trusted: str) -> str | None:
    """Intersect a collection request with the active UI collection."""
    if trusted not in {"native", "social", "all"} or requested not in {None, "native", "social", "all"}:
        return None
    if requested in {None, "all"}:
        return trusted
    return requested if trusted in {"all", requested} else None


def _literal_date_scope(text: str) -> tuple[str | None, str | None] | None:
    """Parse bounded year/ISO scope using the executor's calendar endpoints.

    None means there is no supported explicit scope. Multiple or invalid
    ranges fail rather than being narrowed to a convenient subset.
    """
    token = rf"(?<![A-Za-z0-9_-])({_YEAR_OR_DAY})(?![A-Za-z0-9_-])"
    ranges = list(re.finditer(
        rf"{token}\s*(?:年\s*)?(?:to|through|and|[–—]|至|到)\s*{token}(?:年)?"
        rf"|\b(?:in|during)\s+({ _YEAR_OR_DAY })\s*-\s*({ _YEAR_OR_DAY })\b", text, re.I))
    if ranges:
        if len(ranges) != 1:
            raise ValueError("Multiple date ranges require period comparison")
        match = ranges[0]
        # 'and' connects endpoints only when introduced by 'between'.
        if re.search(r"\band\b", match.group(), re.I) and not re.search(
                r"\bbetween\s*$", text[:match.start()], re.I):
            raise ValueError("Unspecified relation between date endpoints")
        remainder = text[:match.start()] + text[match.end():]
        if re.search(rf"\b{_YEAR_OR_DAY}\b", remainder):
            raise ValueError("Additional dates were not covered by the range")
        values = [value for value in match.groups() if value is not None]
        lower, upper = _date_endpoint(values[0], False), _date_endpoint(values[1], True)
        if lower > upper:
            raise ValueError("Invalid date order")
        return lower.isoformat(), upper.isoformat()
    tokens = list(re.finditer(token, text))
    if len(tokens) > 1:
        raise ValueError("Multiple dates need an explicit range or comparison")
    if not tokens:
        return None
    match = tokens[0]
    value = match.group(1)
    prefix = text[:match.start()]
    suffix = text[match.end():]
    after = bool(re.search(r"\bafter\s*$", prefix, re.I) or re.match(r"\s*(?:年)?之后", suffix))
    before = bool(re.search(r"\bbefore\s*$", prefix, re.I) or re.match(r"\s*(?:年)?之前", suffix))
    lower_only = after or bool(re.search(r"\b(?:since|from)\s*$|自\s*$", prefix, re.I))
    upper_only = before or bool(re.search(r"\b(?:until|through|to)\s*$|截至\s*$", prefix, re.I))
    if lower_only:
        lower = _date_endpoint(value, after) + (timedelta(days=1) if after else timedelta())
        return lower.isoformat(), None
    if upper_only:
        upper = _date_endpoint(value, not before) - (timedelta(days=1) if before else timedelta())
        return None, upper.isoformat()
    if len(value) == 10 or re.search(r"\b(?:in|during|year)\s*$|(?:在|年份)\s*$", prefix, re.I):
        return _date_endpoint(value, False).isoformat(), _date_endpoint(value, True).isoformat()
    raise ValueError("Unsupported wording around an explicit date token")


def _unsupported_period_dates(text: str) -> bool:
    """Keep unsupported sub-year/open periods out of the annual heuristic."""
    months = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
              r"Aug(?:ust)?|Sep(?:tember)?|Sept(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
    if re.search(rf"\b{months}\.?\s+(?:\d{{1,2}}(?:st|nd|rd|th)?\s*,?\s*)?\d{{4}}\b"
                 rf"|\b\d{{4}}\s+{months}\b|\d{{4}}年\s*\d{{1,2}}月"
                 r"|\b(?:Q[1-4]|quarters?)\b|季度", text, re.I):
        return True
    numeric_dates = re.findall(
        r"(?<![A-Za-z0-9_])(?:\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?|\d{1,2}[-/]\d{4})"
        r"(?![A-Za-z0-9_])", text)
    for value in numeric_dates:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return True
        try:
            _date_endpoint(value, False)
        except ValueError:
            return True
    closed = list(re.finditer(
        rf"\bfrom\s+{_YEAR_OR_DAY}\s+(?:to|through)\s+{_YEAR_OR_DAY}\b", text, re.I))
    for match in re.finditer(r"\b(?:before|after|since|until|from|through)\s+(?=\d{4}\b)"
                             r"|(?:自|截至)\s*(?=\d{4})|(?<=年)(?:之前|之后)", text, re.I):
        if not any(interval.start() <= match.start() < interval.end() for interval in closed):
            return True
    return False


def literal_scope_preserved(question: str, context: dict, supplied_filters: dict, base_filters: dict,
                            *, check_dates: bool = True) -> bool:
    """Check visible literal entities and explicit date tokens, not inferred intent."""
    effective = {**base_filters, **{key: value for key, value in supplied_filters.items() if value is not None}}
    dataset = effective_dataset_scope(supplied_filters.get("dataset"), base_filters.get("dataset", "all"))
    explicit = question_contract(question).get("explicit_datasets", [])
    if dataset is None or explicit and dataset != ("all" if len(explicit) > 1 else explicit[0]):
        return False
    for key, choose in (("date_from", max), ("date_to", min)):
        bounds = [str(value) for value in (base_filters.get(key), supplied_filters.get(key)) if value]
        effective[key] = choose(bounds) if bounds else None
    mentions = []
    for dimension in ("sponsors", "publishers", "accounts"):
        items = context.get(dimension) or []
        if isinstance(items, dict):
            items = items.get("options") or []
        for item in items:
            item = {"value": item} if isinstance(item, str) else item
            spellings = {item.get("value"), item.get("display")} - {None, ""}
            # Use the same bounded source aliases as the tool executor.
            aliases = _entity_keys(item["value"], dimension) if dimension != "accounts" else set()
            patterns = [re.escape(spelling) for spelling in spellings]
            patterns.extend(r"[\s._-]*".join(map(re.escape, spelling)) for spelling in aliases)
            for spelling in patterns:
                pattern = r"(?<![A-Za-z0-9_])" + spelling + r"(?![A-Za-z0-9_])"
                for match in re.finditer(pattern, question, flags=re.I):
                    mentions.append((match.start(), match.end(), dimension, item))
    covered = set()
    required = {}
    for start, end, dimension, item in sorted(mentions, key=lambda m: m[1] - m[0], reverse=True):
        if any(index in covered for index in range(start, end)):
            continue
        namespaces = {m[2] for m in mentions if m[0] == start and m[1] == end}
        same_namespace = {str(m[3]["value"]).strip().casefold() for m in mentions
                          if m[0] == start and m[1] == end and m[2] == dimension}
        if len(same_namespace) > 1:
            return False  # A shared alias cannot choose the first source value.
        if len(namespaces) > 1:
            prefix = question[max(0, start - 70):start]
            roles = {key for key, pattern in {
                "publishers": r"(?:publisher|outlet|newspaper|媒体|出版商)\s*[:=]?\s*['\"]?\s*$",
                "sponsors": r"(?:sponsor|company|advertiser|sponsored by|赞助方|公司)\s*[:=]?\s*['\"]?\s*$",
                "accounts": r"(?:account|账号|账户)\s*[:=]?\s*['\"]?\s*$",
            }.items() if re.search(pattern, prefix, re.I)}
            if not roles:
                roles = {m[2] for m in mentions if m[0] == start and m[1] == end
                         and str(m[3]["value"]).casefold() in {str(v).casefold() for v in base_filters.get(m[2]) or []}}
            if len(roles) != 1:
                return False  # The model cannot choose a namespace for the user.
            if dimension not in roles:
                continue
        covered.update(range(start, end))
        wanted = {str(value).strip().casefold() for value in item.get("source_variants") or [item["value"]]}
        required.setdefault(dimension, set()).update(wanted)
    for dimension in ("sponsors", "publishers", "accounts"):
        wanted = required.get(dimension, {str(value).strip().casefold() for value in base_filters.get(dimension) or []})
        known = [item if isinstance(item, str) else item.get("value") for item in (
            (context.get(dimension) or {}).get("options", []) if isinstance(context.get(dimension), dict)
            else context.get(dimension) or [])]
        selected = {str(canonical).strip().casefold() for value in effective.get(dimension) or []
                    for canonical in (canonical_source_values(value, dimension, known) or [value])}
        if wanted != selected:
            return False
    if not check_dates:
        return True
    # Entity-name dates do not become publication-date constraints.
    date_text = ''.join(char if index not in covered else ' ' for index, char in enumerate(question))
    if _RELATIVE_CALENDAR.search(date_text):
        return False
    try:
        scope = _literal_date_scope(date_text)
    except (ValueError, OverflowError):
        return False
    for key, choose, endpoint in (("date_from", max, scope[0] if scope else None),
                                  ("date_to", min, scope[1] if scope else None)):
        bounds = [str(value) for value in (base_filters.get(key), endpoint) if value]
        expected = choose(bounds) if bounds else None
        if effective.get(key) != expected:
            return False
    return True


def statistics_request_preserves_question(question: str, context: dict, arguments: dict,
                                         base_filters: dict) -> bool:
    """Check literal scope plus requested time operation; never supply a count.

    This is a bounded omission guard, not general semantic interpretation. The
    executor remains responsible for canonical names and scope intersection.
    Compare complete period ranges rather than requiring them in outer filters.
    """
    filters = arguments.get("filters") or {}
    missing_date = bool(re.search(
        r"\b(?:no|unknown|missing|without)\s+(?:a\s+)?(?:publication\s+)?dates?\b"
        r"|\bpublication\s+dates?\s+(?:is|are)\s+(?:unknown|missing)\b|缺失日期|日期未知|没有.{0,6}日期|无日期",
        question, re.I))
    known_date = bool(re.search(
        r"\b(?:known|with\s+a)\s+(?:publication\s+)?dates?\b|已知日期|有.{0,6}发布日期",
        question, re.I))
    presence = filters.get("date_presence") or base_filters.get("date_presence") or "any"
    if missing_date and presence != "missing":
        return False
    if known_date and not missing_date and presence != "known":
        range_excludes_missing = bool(filters.get("date_from") or filters.get("date_to"))
        if not range_excludes_missing and filters.get("include_unknown_dates", base_filters.get("include_unknown_dates", True)):
            return False
    wants_share = bool(re.search(r"\b(?:percentage|percent|proportion|share)\b|百分比|占比", question, re.I))
    if wants_share and arguments.get("measure") != "share":
        return False
    grouping_prompt = r"\b(?:which|what|list|show|how\s+many)\s+(?:(?:all|the|distinct|different|source-listed)\s+)*"
    requested_groups = {dimension for dimension, (names, extra) in {
        "publishers": (r"publishers|outlets|newspapers", r"\bby\s+(?:publisher|outlet)\b|哪些媒体|列出.{0,12}媒体"),
        "sponsors": (r"sponsors|companies|advertisers", r"\bby\s+(?:sponsor|company)\b|哪些赞助方|列出.{0,12}赞助"),
        "platforms": (r"platforms", r"\bby\s+platform\b|哪些平台"),
        "accounts": (r"accounts", r"\bby\s+account\b|哪些账号"),
    }.items() if re.search(grouping_prompt + r"(?:" + names + r")\b|" + extra, question, re.I)}
    if requested_groups and (len(requested_groups) != 1 or arguments.get("group_by") not in requested_groups):
        return False
    entities = dict(filters)
    denominator = arguments.get("denominator_filters") or {}
    if arguments.get("measure") == "share" and denominator:
        for dimension in ("publishers", "sponsors", "accounts"):
            entities[dimension] = list(dict.fromkeys([
                *(filters.get(dimension) or base_filters.get(dimension) or []),
                *(denominator.get(dimension) or []),
            ]))
        # Names occurring in the comparison group cannot hide a changed
        # numerator. For an explicit "... are sponsored by X" target, read
        # the target independently, inheriting only the comparison scope.
        target = re.search(r"\b(?:are|were|is|was)\s+(.+)$|(?:其中|当中).{0,8}?(?:有|是)(.+)$", question, re.I)
        if target and not literal_scope_preserved(
                next(part for part in target.groups() if part is not None), context, filters,
                {**base_filters, **denominator}, check_dates=False):
            return False
    if not literal_scope_preserved(question, context, entities, base_filters, check_dates=False):
        return False

    # A literal stored name containing a year is not a date constraint.
    time_text = question
    for dimension in ("publishers", "sponsors", "accounts"):
        items = context.get(dimension) or []
        if isinstance(items, dict):
            items = items.get("options") or []
        for item in items:
            value = item if isinstance(item, str) else item.get("value", "")
            if value:
                time_text = re.sub(re.escape(value), " ", time_text, flags=re.I)
    if _RELATIVE_CALENDAR.search(time_text):
        return False
    yearly = bool(re.search(r"\b(?:by|per|each)\s+(?:publication\s+)?year\b|\byearly\b|按年|每年|逐年", time_text, re.I))
    highest_year = bool(re.search(
        r"\b(?:which|what)\b.{0,60}\byears?\b.{0,70}\b(?:most|highest|largest|maximum|greatest)\b"
        r"|\b(?:most|highest|largest|maximum|greatest)\b.{0,70}\byears?\b|哪.{0,8}年.{0,12}(?:最多|最高)", time_text, re.I))
    if (yearly or highest_year) and arguments.get("group_by") != "years":
        return False
    if highest_year and arguments.get("ranking") != "highest":
        return False

    iso_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", time_text)
    years_only = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", time_text)
    years = re.findall(r"\b(?:19|20)\d{2}\b", years_only)
    comparing = bool(re.search(r"\b(?:compare|versus|vs)\b|比较|对比", time_text, re.I))
    expects_periods = comparing and (len(iso_dates) >= 2 or len(set(years)) >= 2)
    periods = arguments.get("periods") or []
    if expects_periods or periods:
        if _unsupported_period_dates(time_text):
            return False
        if not expects_periods or not 2 <= len(periods) <= 3:
            return False
        if len(iso_dates) in (4, 6):
            expected = list(zip(iso_dates[::2], iso_dates[1::2]))
        elif len(iso_dates) == 2:
            expected = [(day, day) for day in iso_dates]
        elif len(years) in (2, 3) and len(set(years)) == len(years):
            expected = [(year + "-01-01", year + "-12-31") for year in years]
        else:
            return False  # Unsupported period wording needs clarification.
        requested = [(period.get("date_from"), period.get("date_to")) for period in periods]
        clipped = [(max(lower, base_filters.get("date_from") or lower),
                    min(upper, base_filters.get("date_to") or upper)) for lower, upper in expected]
        actual = [(max(str(lower), base_filters.get("date_from") or str(lower)),
                   min(str(upper), base_filters.get("date_to") or str(upper)))
                  for lower, upper in requested if lower and upper]
        return len(actual) == len(requested) == len(clipped) and sorted(actual) == sorted(clipped)
    return literal_scope_preserved(question, context, entities, base_filters)


def entity_dates_used_as_filters(question: str, context: dict, args: dict) -> bool:
    """Block dates borrowed only from a literal entity name, not real date scope.

    This narrow guard does not parse dates generally. Existing UI date filters
    remain authoritative; only new model-supplied date endpoints are checked.
    """
    filters = args.get("filters") or {}
    endpoints = [filters.get(key) for key in ("date_from", "date_to") if filters.get(key)]
    if not endpoints:
        return False
    remainder = question.casefold()
    name_years = set()
    for dimension in ("sponsors", "publishers", "accounts"):
        values = context.get(dimension) or []
        if isinstance(values, dict):
            values = values.get("options") or []
        for item in values:
            name = item if isinstance(item, str) else (item.get("value") or item.get("name") or "")
            if name and name.casefold() in remainder and re.search(r"[（(].*\d{4}.*[)）]", name):
                name_years.update(re.findall(r"\b\d{4}\b", name))
                remainder = remainder.replace(name.casefold(), " ")
    if not name_years or re.search(r"\b\d{4}\b|\b(?:since|before|after|between|during|until)\b|自|之前|之后|期间|年份|年内", remainder):
        return False
    return any(str(value)[:4] in name_years for value in endpoints)

_MENTION = re.compile(
    r"\b(?:mention(?:s|ed|ing)?|refer(?:s|red|ring)?\s+to|"
    r"discuss(?:es|ed|ing)?|contain(?:s|ed|ing)?|feature(?:s|d|ing)?)\b"
    r"|提及|提到|谈到|讨论|含有|涉及",
    re.IGNORECASE,
)
_FRAMING = re.compile(
    r"\b(?:present(?:s|ed|ing)?|position(?:s|ed|ing)?|portray(?:s|ed|ing)?|"
    r"acknowledg(?:e|es|ed|ing)|assert(?:s|ed|ing)?|claim(?:ed|ing)?|"
    r"emphasiz(?:e|es|ed|ing)|highlight(?:s|ed|ing)?|"
    r"suggest(?:s|ed|ing)?|recommend(?:s|ed|ing)?|"
    r"offer(?:s|ed|ing)?|redirect(?:s|ed|ing)?|argu(?:e|es|ed|ing)|"
    r"describ(?:e|es|ed|ing)|say(?:s|ing)?|said|stat(?:ed|ing))\b"
    r"|\bpoint(?:s|ed|ing)?\s+out\b"
    r"|\b(?:ads?|advertisements?|articles?|posts?|records?|they)\s+"
    r"(?:(?:also|explicitly|not|do|does|did|often|ever)\s+)*states?\b"
    r"|\bstates?\s+(?:that|whether|how|what|their|its|our|the|a|an)\b"
    r"|\bmake(?:s)?\s+[^?。？！\n]{0,80}\b(?:claims|recommendations)\b"
    r"|声称|宣称|强调|承认|断言|暗示|建议|宣传|转移注意|指出|表示|表述|描述"
    r"|(?:描述|定位|塑造).{0,80}?为|(?:将|把).{0,80}?(?:作为|视为|称为)",
    re.IGNORECASE,
)
_TOPIC = re.compile(
    r"\b(?:ads?|advertisements?|articles?|posts?|records?)\b[^?。？！\n]{0,120}"
    r"\b(?:about|on the topic of)\b|关于|以.{1,60}?为主题",
    re.IGNORECASE,
)
_DISCLOSURE = re.compile(
    r"\bdisclos(?:e|es|ed|ing|ure|ures)\b|披露|广告标识|付费标识|赞助标识",
    re.IGNORECASE,
)
_QUANTITY = re.compile(
    r"\b(?:how many|count|counts|number of|percentage|percent|proportion|share)\b"
    r"|多少|几篇|几条|计数|数量|占比|百分比",
    re.IGNORECASE,
)
_LIST = re.compile(
    r"\b(?:which|list|show|find)\b[^?。？！\n]{0,100}"
    r"\b(?:ads?|advertisements?|articles?|posts?|records?)\b|哪些.{0,30}?(?:广告|文章|帖子)",
    re.IGNORECASE,
)
# A label predicate is metadata even when its literal value contains a content
# verb. Strip that value only, so a separate content condition remains visible.
_LABEL_PREDICATE = re.compile(
    r"\b(?:with|have|has|having|assigned|carry|carrying|under)\s+"
    r"(?:the\s+)?(?:historical\s+)?labels?\s*(?:=|:|of|named)?\s*"
    r"(?:['\"][^'\"]+['\"]|[\w-]+)"
    r"|\b(?:label(?:ed|led)\s+(?:as\s+)?)"
    r"(?:['\"][^'\"]+['\"]|[\w-]+)"
    r"|(?:带有|具有|有|属于|标为|标注为)\s*(?:历史)?标签\s*"
    r"(?:['\"][^'\"]+['\"]|[\w-]+)",
    re.IGNORECASE,
)
_QUOTED_LITERAL = re.compile(r"(['\"])[^'\"]*\1")


def question_contract(question: str) -> dict:
    """Preserve explicit task limits before the model chooses a data tool.

    An absent cue means unspecified, not proof of a metadata-only question.
    Literal labels and NC_/SC_ IDs remain usable metadata filters. The rules
    intentionally avoid maintaining lists of companies or scientific topics.
    """
    normalized = " ".join(question.translate(str.maketrans({
        "‘": "'", "’": "'", "“": '"', "”": '"',
    })).split())
    predicate_text = _QUOTED_LITERAL.sub(" ", _LABEL_PREDICATE.sub(" ", normalized))
    if _DISCLOSURE.search(predicate_text):
        task = "disclosure"
    elif _FRAMING.search(predicate_text):
        task = "content_framing"
    elif _MENTION.search(predicate_text) or _TOPIC.search(predicate_text):
        task = "content_mentions"
    else:
        task = "metadata_or_unspecified"
    content = task != "metadata_or_unspecified"
    return {
        "policy": POLICY_VERSION,
        "task": task,
        "content_condition": content,
        "requires_complete_list": bool(content and _LIST.search(normalized)),
        "requires_content_count": bool(content and _QUANTITY.search(normalized)),
        "coverage": "retrieved_examples_only" if content else "tool_defined",
        "detection": "conservative_surface_cues_not_general_semantic_classification",
        "original_metadata_only": original_metadata_question(normalized),
        "compound_read_request": _compound_read_request(predicate_text),
        "explicit_datasets": [dataset for dataset, pattern in _DATASETS.items() if pattern.search(predicate_text)],
    }


def metadata_content_clarification(question: str) -> str:
    """Explain the blocked operation without inventing a content result."""
    if re.search(r"[\u4e00-\u9fff]", question):
        return (
            "元数据统计无法判断广告是否提及或表达某种观点。请使用证据检索查看有原文支持的例子；"
            "完整广告名单或内容计数需要经过审查的逐篇标注。"
        )
    return (
        "Metadata counts cannot answer a condition about what an advertisement says. "
        "Use evidence search for supported examples; a complete matching-ad list or "
        "content count requires reviewed article annotations."
    )
