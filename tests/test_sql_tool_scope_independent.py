"""Fresh SQL-tool scope checks. FixtureDB is Python enumeration, NOT SQL.

No customer catalog, old fixtures, provider, or production DB is used.
Scripted tool choices exercise actual ToolCatalog/ResearchAgent/Service paths.
"""

import json
from collections import Counter
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pgvector.psycopg import register_vector
from postgres_fixtures import configured_test_url, verified_test_connection
from psycopg import sql

from observatory.config import Settings
from observatory.db import Database
from observatory.models import Filters, ImportBatch, RecordInput
from observatory.research_agent import ResearchAgent
from observatory.research_tools import FiltersRequest, ScopeConflict, ToolCatalog
from observatory.service import Service
from observatory.structured_queries import plan_question


def row(record_id, publisher, sponsor, day, *, dataset="native", retrievable=True):
    return dict(record_id=record_id, version_id="v-" + record_id,
                dataset=dataset, title="Synthetic scope row " + record_id,
                publisher=publisher, sponsor=sponsor, date=day,
                keyword="fixture", labels=[], platform="FixtureNet" if dataset == "social" else "",
                account="Fixture channel" if dataset == "social" else "",
                retrievable=retrievable, date_basis="source" if day else "missing",
                url="", archive_url="", inferred_tier=None)


ROWS = [
    row("scope-n1", "Cedar Ledger", "Larch Works", "1996-02-12"),
    row("scope-n2", "Cedar Ledger", "Birch Studio", "1996-09-10"),
    row("scope-n3", "Cedar Ledger", "Larch Works", "1997-04-22", retrievable=False),
    row("scope-n4", "Granite Review", "Larch Works", "1996-11-21"),
    row("scope-n5", "Cedar Ledger", "Birch Studio", None),
    row("scope-n6", "Granite Review", "Birch Studio", "1997-03-05"),
    row("scope-n7", "Granite Review", "Larch Works", "1997-07-04"),
    row("scope-n8", "Granite Review", "Birch Studio", None),
    row("scope-s1", "Threadhouse", "Larch Works", "1996-03-01", dataset="social"),
    row("scope-s2", "Threadhouse", "Birch Studio", "1997-03-01", dataset="social"),
    row("scope-s3", "Threadhouse", "Birch Studio", None, dataset="social"),
]


class FixtureDB:
    """Deliberately small enumerator, with no connection/SQL behavior."""

    def __init__(self):
        self.rows = deepcopy(ROWS)
        self.saved = []
        self.selections = []

    def connect(self):
        raise AssertionError("Mock checks must not connect to any database")

    def health(self):
        return {"status": "ok", "data_version": "fresh-scope-v1",
                "countable_record_counts": {d: sum(r["dataset"] == d for r in self.rows)
                                            for d in ("native", "social")}}

    def facets(self, dataset):
        rows = [r for r in self.rows if dataset == "all" or r["dataset"] == dataset]
        result = {plural: sorted({r[singular] or "(Unknown)" for r in rows})
                  for plural, singular in (("publishers", "publisher"), ("sponsors", "sponsor"),
                                            ("platforms", "platform"), ("accounts", "account"))}
        return {**result, "keywords": ["fixture"], "labels": []}

    def selected(self, filters):
        self.selections.append(filters.model_copy(deep=True))
        picked = []
        for r in self.rows:
            if filters.dataset != "all" and r["dataset"] != filters.dataset:
                continue
            if any(getattr(filters, plural) and (r[singular] or "(Unknown)") not in getattr(filters, plural)
                   for plural, singular in (("publishers", "publisher"), ("sponsors", "sponsor"),
                                            ("platforms", "platform"), ("accounts", "account"),
                                            ("record_ids", "record_id"), ("keywords", "keyword"))):
                continue
            day = date.fromisoformat(r["date"]) if r["date"] else None
            if filters.date_presence == "known" and day is None:
                continue
            if filters.date_presence == "missing" and day is not None:
                continue
            if day is None:
                if not filters.include_unknown_dates:
                    continue
            elif ((filters.date_from and day < filters.date_from)
                  or (filters.date_to and day > filters.date_to)):
                continue
            picked.append(deepcopy(r))
        return picked

    def dashboard(self, filters, offset=0, limit=20, *_):
        selected = self.selected(filters)
        stats = {"total": len(selected), "retrievable": sum(r["retrievable"] for r in selected),
                 "unknown_dates": sum(not r["date"] for r in selected), "inferred_dates": {}}
        for plural, singular in (("publishers", "publisher"), ("sponsors", "sponsor"),
                                 ("platforms", "platform"), ("accounts", "account")):
            stats[plural] = [{"name": k, "count": v} for k, v in
                             sorted(Counter(r[singular] or "(Unknown)" for r in selected).items())]
        return {"stats": stats, "page": {"rows": selected[offset:offset + limit]}}

    def research_statistics(self, filters, *, group_by, ranking, periods):
        selected = self.selected(filters)
        datasets = ("native", "social") if filters.dataset == "all" else (filters.dataset,)
        collections, groups = [], []
        for dataset in datasets:
            members = [r for r in selected if r["dataset"] == dataset]
            collections.append({"dataset": dataset, "total": len(members),
                                "unknown_dates": sum(not r["date"] for r in members),
                                "retrievable": sum(r["retrievable"] for r in members),
                                "inferred_dates": 0})
            years = Counter(r["date"][:4] for r in members if r["date"])
            peak = max(years.values(), default=0)
            groups += [{"dataset": dataset, "name": y, "count": n}
                       for y, n in sorted(years.items()) if ranking != "highest" or n == peak]
        period_results = []
        for period in periods:
            members = self.selected(period["filters"])
            period_results.append({"label": period["label"], "filters": period["filters"].model_dump(mode="json"),
                "collections": [{"dataset": d, "total": sum(r["dataset"] == d for r in members)} for d in datasets]})
        return {"filters": filters.model_dump(mode="json"), "collections": collections,
                "groups": groups if group_by == "years" else [], "periods": period_results,
                "records": selected[:10], "inferred_tiers": {}}

    def save_answer(self, *args):
        self.saved.append(args)


class NoProvider:
    @property
    def client(self):
        raise AssertionError("No provider call is authorized by this test")


class ScriptedStatistics(ResearchAgent):
    def __init__(self, catalog, arguments):
        super().__init__(NoProvider(), catalog)
        self.arguments = arguments

    def _dispatch(self, inputs, definitions, visitor, step):
        response = SimpleNamespace(status="completed", output=[SimpleNamespace(
            type="function_call", name="record_statistics", call_id=f"fixture-call-{step}",
            arguments=json.dumps(self.arguments))])
        return response, {"mock_dispatch": True, "provider_calls": 0}, 0.0


def service_for(base, arguments=None):
    service = Service(Settings(research_agent_enabled=True, web_search_enabled=False),
                      db=FixtureDB(), rag=NoProvider())
    if arguments is not None:
        service.research_agent = ScriptedStatistics(ToolCatalog(service, base), arguments)
    return service


@pytest.mark.parametrize("question,arguments", [
    ("How many records published by Cedar Ledger in 1996?",
     {"filters": {"publishers": ["Cedar Ledger"]}, "group_by": "none", "measure": "count"}),
    ("How many records published by Cedar Ledger and sponsored by Larch Works?",
     {"filters": {"publishers": ["Cedar Ledger"]}, "group_by": "none", "measure": "count"}),
    ("Which publication year has the most records published by Cedar Ledger?",
     {"filters": {"publishers": ["Cedar Ledger"]}, "group_by": "none", "measure": "count"}),
    ("Compare records published by Cedar Ledger in 1996 versus 1997.",
     {"filters": {"publishers": ["Cedar Ledger"], "date_from": "1996-01-01", "date_to": "1996-12-31"},
      "group_by": "none", "measure": "count"}),
])
def test_single_statistics_must_preserve_question_predicates(question, arguments):
    base = Filters(include_inferred_dates=False)
    service = service_for(base, arguments)
    result = service.answer(question, base, "fresh-scope-mock")
    assert result.status == "insufficient_evidence" and result.answer_mode == "clarification", result.model_dump(mode="json")


def test_rule_planner_does_not_drop_one_named_source_outside_selection():
    base = Filters(publishers=["Cedar Ledger"], include_inferred_dates=False)
    service = service_for(base)
    plan = plan_question("How many records published by Cedar Ledger and Granite Review?",
                         base, service.facets("native"))
    assert plan.status == "clarify", plan


def test_mcp_rejects_partial_outside_selection_instead_of_dropping_it():
    base = Filters(publishers=["Cedar Ledger"], include_inferred_dates=False)
    catalog = ToolCatalog(service_for(base), base)
    result = catalog.call("record_statistics", {"filters": {"publishers": ["Cedar Ledger", "Granite Review"]}})
    assert result["status"] == "clarify"


def test_all_request_cannot_expand_native_selection():
    base = Filters(publishers=["Cedar Ledger"], include_inferred_dates=False)
    service = service_for(base)
    result = ToolCatalog(service, base).call("record_statistics", {"filters": {"dataset": "all"}})
    assert result["status"] == "ok"
    assert result["filters"]["dataset"] == "native"
    assert result["collections"] == [{"dataset": "native", "total": 4, "retrievable": 3, "unknown_dates": 1}]


def test_explicit_date_basis_and_missing_date_permissions_are_preserved():
    base = Filters(include_unknown_dates=False, include_inferred_dates=False)
    catalog = ToolCatalog(service_for(base), base)
    with pytest.raises(ScopeConflict):
        catalog.narrow(FiltersRequest(include_inferred_dates=True))
    with pytest.raises(ScopeConflict):
        catalog.narrow(FiltersRequest(date_presence="missing", include_unknown_dates=True))


def test_year_ties_and_unknowns_keep_separate_units_through_renderer():
    base = Filters(dataset="all", include_inferred_dates=False)
    service = service_for(base)
    data = ToolCatalog(service, base).call("record_statistics", {"group_by": "years", "ranking": "highest"})
    assert data["status"] == "ok"
    assert data["groups"] == [{"dataset": "native", "name": "1996", "count": 3},
                              {"dataset": "native", "name": "1997", "count": 3},
                              {"dataset": "social", "name": "1996", "count": 1},
                              {"dataset": "social", "name": "1997", "count": 1}]
    assert data["unknown_dates"] == [{"dataset": "native", "count": 2}, {"dataset": "social", "count": 1}]
    answer = service._tool_statistics_answer(data, base_filters=base)
    assert "1996, 1997 tie" in answer.answer
    assert "2 native ad records have no date" in answer.answer
    assert "1 company social posts have no date" in answer.answer


def test_named_periods_preserve_both_counts_and_exclude_unknowns():
    base = Filters(publishers=["Cedar Ledger"], include_inferred_dates=False)
    service = service_for(base)
    data = ToolCatalog(service, base).call("record_statistics", {"periods": [
        {"label": "Earlier window", "date_from": "1996-01-01", "date_to": "1996-12-31"},
        {"label": "Later window", "date_from": "1997-01-01", "date_to": "1997-12-31"}]})
    assert data["status"] == "ok"
    assert [p["collections"][0]["total"] for p in data["periods"]] == [2, 1]
    assert all(p["filters"]["include_unknown_dates"] is False for p in data["periods"])
    answer = service._tool_statistics_answer(data, base_filters=base)
    assert "2 in Earlier window; 1 in Later window" in answer.answer
    assert "Earlier window has 1 more records" in answer.answer


@pytest.mark.parametrize("question,arguments,kind", [
    ("How many records published by Cedar Ledger in 1996?",
     {"filters": {"publishers": ["Cedar Ledger"], "date_from": "1996-01-01", "date_to": "1996-12-31"},
      "group_by": "none", "measure": "count"}, "count"),
    ("How many records published by Cedar Ledger and sponsored by Larch Works?",
     {"filters": {"publishers": ["Cedar Ledger"], "sponsors": ["Larch Works"]},
      "group_by": "none", "measure": "count"}, "count"),
    ("Which publication year has the most records published by Cedar Ledger?",
     {"filters": {"publishers": ["Cedar Ledger"]}, "group_by": "years", "ranking": "highest"}, "top_years"),
    ("Compare records published by Cedar Ledger in 1996 versus 1997.",
     {"filters": {"publishers": ["Cedar Ledger"]}, "periods": [
         {"label": "Earlier window", "date_from": "1996-01-01", "date_to": "1996-12-31"},
         {"label": "Later window", "date_from": "1997-01-01", "date_to": "1997-12-31"}]}, "compare_periods"),
])
def test_complete_request_is_answered_instead_of_blanket_blocked(question, arguments, kind):
    base = Filters(include_inferred_dates=False)
    service = service_for(base, arguments)
    result = service.answer(question, base, "fresh-complete-mock")
    assert result.status == "answered", result.model_dump(mode="json")
    assert result.structured_result["kind"] == kind
    if kind == "count":
        assert result.structured_result["collections"][0]["total"] == 2
    elif kind == "top_years":
        assert result.structured_result["groups"] == [{"dataset": "native", "name": "1996", "count": 2}]
    else:
        assert [p["collections"][0]["total"] for p in result.structured_result["periods"]] == [2, 1]


def test_rule_planner_preserves_complete_allowed_entity_list():
    base = Filters(publishers=["Cedar Ledger", "Granite Review"], include_inferred_dates=False)
    service = service_for(base)
    plan = plan_question("How many records published by Cedar Ledger and Granite Review?",
                         base, service.facets("native"))
    assert plan.status == "ready"
    assert plan.filters.publishers == ["Cedar Ledger", "Granite Review"]


def test_current_alias_resolves_on_rule_and_single_tool_paths():
    base = Filters(publishers=["nytimes.com"], include_inferred_dates=False)
    service = service_for(base, {"filters": {"publishers": ["NYT"]}, "measure": "count"})
    service.db.rows = [row("scope-alias", "nytimes.com", "Birch Studio", "1996-02-13")]
    question = "How many records published by NYT?"
    plan = plan_question(question, base, service.facets("native"))
    assert plan.status == "ready" and plan.filters.publishers == ["nytimes.com"]
    result = service.answer(question, base, "fresh-alias-mock")
    assert result.status == "answered", result.model_dump(mode="json")
    assert result.structured_result["filters"]["publishers"] == ["nytimes.com"]
    assert result.structured_result["collections"][0]["total"] == 1


def test_single_statistics_cannot_hide_requested_source_outside_active_selection():
    base = Filters(publishers=["Cedar Ledger"], include_inferred_dates=False)
    service = service_for(base, {"filters": {"publishers": ["Cedar Ledger"]}, "measure": "count"})
    result = service.answer("How many records published by Cedar Ledger and Granite Review?",
                            base, "fresh-hidden-scope-mock")
    assert result.status == "insufficient_evidence" and result.answer_mode == "clarification", result.model_dump(mode="json")


def test_correct_publisher_grouping_and_missing_grouping_are_distinguished():
    base = Filters(include_inferred_dates=False)
    question = "Which publishers have records sponsored by Larch Works?"
    good = service_for(base, {"filters": {"sponsors": ["Larch Works"]}, "group_by": "publishers"})
    answer = good.answer(question, base, "fresh-publisher-groups")
    assert answer.status == "answered", answer.model_dump(mode="json")
    assert [(item["dataset"], item["name"], item["count"])
            for item in answer.structured_result["groups"]] == [
        ("native", "Cedar Ledger", 2), ("native", "Granite Review", 2)]
    bad = service_for(base, {"filters": {"sponsors": ["Larch Works"]}, "group_by": "none"})
    answer = bad.answer(question, base, "fresh-missing-publisher-groups")
    assert answer.answer_mode == "clarification", answer.model_dump(mode="json")


@pytest.mark.parametrize("windows,base_dates,expected", [
    ([("1996-01-01", "1996-12-31"), ("1997-01-01", "1997-12-31")],
     ("1996-07-01", "1997-06-30"), [1, 1]),
    ([("1996-01-01", "1996-06-30"), ("1996-07-01", "1996-12-31"), ("1997-01-01", "1997-12-31")],
     ("1996-02-01", "1997-06-30"), [1, 1, 1]),
])
def test_valid_iso_periods_intersect_active_outer_dates(windows, base_dates, expected):
    base = Filters(publishers=["Cedar Ledger"], include_inferred_dates=False,
                   date_from=date.fromisoformat(base_dates[0]), date_to=date.fromisoformat(base_dates[1]))
    question = "Compare records published by Cedar Ledger in " + " versus ".join(
        f"{lower} to {upper}" for lower, upper in windows) + "."
    args = {"filters": {"publishers": ["Cedar Ledger"]}, "periods": [
        {"label": "Window " + str(i), "date_from": lower, "date_to": upper}
        for i, (lower, upper) in enumerate(windows)]}
    answer = service_for(base, args).answer(question, base, "fresh-iso-periods")
    assert answer.status == "answered", answer.model_dump(mode="json")
    periods = answer.structured_result["periods"]
    assert [p["collections"][0]["total"] for p in periods] == expected
    assert periods[0]["filters"]["date_from"] == base_dates[0]
    assert periods[-1]["filters"]["date_to"] == base_dates[1]


def test_yearly_all_ties_answer_survives_question_guard():
    base = Filters(include_inferred_dates=False)
    answer = service_for(base, {"group_by": "years", "ranking": "highest"}).answer(
        "Which publication years have the most records?", base, "fresh-tied-years")
    assert answer.status == "answered", answer.model_dump(mode="json")
    assert answer.structured_result["groups"] == [
        {"dataset": "native", "name": "1996", "count": 3},
        {"dataset": "native", "name": "1997", "count": 3}]
    assert answer.structured_result["unknown_dates"] == [{"dataset": "native", "count": 2}]


def test_human_new_york_times_alias_to_source_domain_is_valid():
    base = Filters(publishers=["nytimes.com"], include_inferred_dates=False)
    service = service_for(base, {"filters": {"publishers": ["New York Times"]}, "measure": "count"})
    service.db.rows = [row("scope-human-alias", "nytimes.com", "Birch Studio", "1996-02-13")]
    answer = service.answer("How many records published by New York Times?", base, "fresh-human-alias")
    assert answer.status == "answered", answer.model_dump(mode="json")
    assert answer.structured_result["filters"]["publishers"] == ["nytimes.com"]
    assert answer.structured_result["collections"][0]["total"] == 1


def test_validation_namespace_has_no_active_filter_or_public_count_truncation():
    base = Filters(publishers=["Cedar Ledger"], include_inferred_dates=False)
    service = service_for(base)
    service.db.rows += [row(f"scope-extra-{i}", f"Synthetic outlet {i:03d}", "Birch Studio", None)
                        for i in range(250)]
    catalog = ToolCatalog(service, base)
    public = catalog.entity_context()
    private = catalog.statistics_validation_context()
    assert [item["value"] for item in public["publishers"]] == ["Cedar Ledger"]
    assert len(private["publishers"]) == 252  # Cedar, Granite, and 250 extra native source names.


@pytest.fixture(scope="module")
def private_sql_sample():
    url = configured_test_url()
    schema = "obs_sql_tools_" + uuid4().hex[:12]
    with verified_test_connection(url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    class PrivateDB(Database):
        def connect(self, vector=False):
            conn = verified_test_connection(url, schema=schema)
            if vector:
                register_vector(conn)
            return conn

    db = PrivateDB(url)
    db.initialize()
    records = [RecordInput(record_id=r["record_id"], dataset="native", publisher=r["publisher"],
                           sponsor=r["sponsor"], keyword=r["keyword"], title=r["title"],
                           published_at=date.fromisoformat(r["date"]) if r["date"] else None,
                           body="Independent synthetic SQL scope fixture.", retrievable=r["retrievable"])
               for r in ROWS if r["dataset"] == "native"]
    db.import_batch(ImportBatch(records=records))
    return db, schema


def real_service(db, base, arguments):
    service = Service(Settings(research_agent_enabled=True, web_search_enabled=False), db=db, rag=NoProvider())
    service.research_agent = ScriptedStatistics(ToolCatalog(service, base), arguments)
    return service


@pytest.mark.integration
def test_real_sql_share_explicit_a_within_a_or_b_denominator(private_sql_sample):
    db, _ = private_sql_sample
    base = Filters(include_inferred_dates=False)
    args = {"measure": "share", "filters": {"sponsors": ["Larch Works"]},
            "denominator_filters": {"sponsors": ["Larch Works", "Birch Studio"]}}
    answer = real_service(db, base, args).answer(
        "What percentage of records sponsored by Larch Works or Birch Studio are sponsored by Larch Works?",
        base, "fresh-sql-share")
    assert answer.status == "answered", answer.model_dump(mode="json")
    collection = answer.structured_result["collections"][0]
    assert (collection["numerator"], collection["denominator"], collection["percentage"]) == (4, 8, 50.0)
    assert collection["unknown_dates"] == 0


@pytest.mark.integration
def test_real_sql_counts_dates_unretrievable_and_ties(private_sql_sample):
    db, _ = private_sql_sample
    base = Filters(include_inferred_dates=False)
    catalog = ToolCatalog(real_service(db, base, {}), base)
    complete = catalog.call("record_statistics", {"filters": {"publishers": ["Cedar Ledger"], "sponsors": ["Larch Works"]}})
    assert complete["status"] == "ok", complete
    assert complete["collections"] == [{"dataset": "native", "total": 2, "retrievable": 1, "unknown_dates": 0}]
    years = catalog.call("record_statistics", {"group_by": "years", "ranking": "highest"})
    assert years["status"] == "ok", years
    assert years["groups"] == [{"dataset": "native", "name": "1996", "count": 3},
                               {"dataset": "native", "name": "1997", "count": 3}]
    assert years["unknown_dates"] == [{"dataset": "native", "count": 2}]


@pytest.mark.integration
def test_real_sql_two_period_counts_final_answer(private_sql_sample):
    db, _ = private_sql_sample
    base = Filters(publishers=["Cedar Ledger"], include_inferred_dates=False,
                   date_from=date(1996, 7, 1), date_to=date(1997, 6, 30))
    args = {"periods": [{"label": "First", "date_from": "1996-01-01", "date_to": "1996-12-31"},
                        {"label": "Second", "date_from": "1997-01-01", "date_to": "1997-12-31"}]}
    answer = real_service(db, base, args).answer(
        "Compare records published by Cedar Ledger from 1996-01-01 to 1996-12-31 versus 1997-01-01 to 1997-12-31.",
        base, "fresh-sql-periods")
    assert answer.status == "answered", answer.model_dump(mode="json")
    assert [p["collections"][0]["total"] for p in answer.structured_result["periods"]] == [1, 1]


@pytest.mark.integration
def test_real_sql_share_does_not_swap_named_numerator(private_sql_sample):
    db, _ = private_sql_sample
    base = Filters(include_inferred_dates=False, include_unknown_dates=False)
    args = {"measure": "share", "filters": {"sponsors": ["Birch Studio"]},
            "denominator_filters": {"sponsors": ["Larch Works", "Birch Studio"]}}
    answer = real_service(db, base, args).answer(
        "What percentage of records sponsored by Larch Works or Birch Studio are sponsored by Larch Works?",
        base, "fresh-sql-swapped-numerator")
    assert answer.answer_mode == "clarification", answer.model_dump(mode="json")


@pytest.mark.integration
def test_real_sql_share_correct_named_numerator_excludes_unknowns(private_sql_sample):
    db, _ = private_sql_sample
    base = Filters(include_inferred_dates=False, include_unknown_dates=False)
    args = {"measure": "share", "filters": {"sponsors": ["Larch Works"]},
            "denominator_filters": {"sponsors": ["Larch Works", "Birch Studio"]}}
    answer = real_service(db, base, args).answer(
        "What percentage of records sponsored by Larch Works or Birch Studio are sponsored by Larch Works?",
        base, "fresh-sql-correct-numerator")
    assert answer.status == "answered", answer.model_dump(mode="json")
    collection = answer.structured_result["collections"][0]
    assert (collection["numerator"], collection["denominator"]) == (4, 6)
    assert collection["percentage"] == pytest.approx(200 / 3)
