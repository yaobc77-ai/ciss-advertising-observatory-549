"""Complete typed statistics and ambiguity contracts without paid model calls."""

from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
from test_research_tools import make_catalog, source

from observatory.db import Database
from observatory.models import Filters
from observatory.research_agent import SYSTEM
from observatory.research_tools import StatisticsRequest
from observatory.service import Service


def collection(total=8, unknown=2, inferred=0):
    return {"dataset": "native", "total": total, "retrievable": total - 1,
            "unknown_dates": unknown, "source_unknown_dates": unknown + inferred,
            "inferred_dates": inferred}


def attach_complete_read(db, *, periods=False, inferred=0):
    db.statistics_reads = []

    def read(filters, **kwargs):
        db.statistics_reads.append((filters.model_copy(deep=True), deepcopy(kwargs)))
        return {"collections": [collection(inferred=inferred)],
            "groups": [] if periods else [{"dataset": "native", "name": "2018", "count": 3},
                                          {"dataset": "native", "name": "2020", "count": 3}],
            "periods": [{"label": item["label"], "filters": item["filters"].model_dump(mode="json"),
                         "collections": [collection(total=n, unknown=0)]}
                        for item, n in zip(kwargs["periods"], (2, 4), strict=False)],
            "records": [source("r1", "The Washington Post", "exxonmobil")],
            "inferred_tiers": {"A": inferred} if inferred else {},
            "date_basis": "source_or_supplemented" if filters.include_inferred_dates else "source_only",
            "snapshot": "repeatable_read_read_only"}

    db.research_statistics = read


@pytest.mark.parametrize("ranking,kind", [("all", "list_years"), ("highest", "top_years")])
def test_year_results_preserve_each_tied_group_and_unknown_separately(ranking, kind):
    catalog, db = make_catalog()
    attach_complete_read(db)
    result = catalog.call("record_statistics", {"group_by": "years", "ranking": ranking})
    assert result["status"] == "ok" and result["kind"] == kind
    assert result["group_by"] == "years"
    assert [(row["name"], row["count"]) for row in result["groups"]] == [("2018", 3), ("2020", 3)]
    assert result["unknown_dates"] == [{"dataset": "native", "count": 2}]
    assert len(db.statistics_reads) == 1
    assert result["snapshot"] == "repeatable_read_read_only"
    assert "raw" not in result["records"][0] and result["records"][0]["archive_url"] == ""


def test_periods_share_scope_and_do_not_assign_missing_dates_to_either_side():
    catalog, db = make_catalog(Filters(sponsors=["exxonmobil"]))
    attach_complete_read(db, periods=True)
    result = catalog.call("record_statistics", {"periods": [
        {"label": "Before 2020", "date_to": "2019-12-31"},
        {"label": "2020 onward", "date_from": "2020-01-01"}]})
    assert result["status"] == "ok" and result["kind"] == "compare_periods"
    assert [row["collections"][0]["total"] for row in result["periods"]] == [2, 4]
    assert result["unknown_dates"] == [{"dataset": "native", "count": 2}]
    assert len(db.statistics_reads) == 1
    assert all(row["filters"]["sponsors"] == ["exxonmobil"] for row in result["periods"])
    assert all(row["filters"]["include_unknown_dates"] is False for row in result["periods"])
    assert result["periods"][0]["filters"]["date_from"] is None
    assert result["periods"][1]["filters"]["date_to"] is None


def test_period_endpoints_intersect_active_dates_without_widening_them():
    catalog, db = make_catalog(Filters(date_from=date(2018, 1, 1), date_to=date(2021, 12, 31)))
    attach_complete_read(db, periods=True)
    result = catalog.call("record_statistics", {"periods": [
        {"label": "Earlier", "date_to": "2019-12-31"},
        {"label": "Later", "date_from": "2020-01-01"}]})
    assert result["periods"][0]["filters"]["date_from"] == "2018-01-01"
    assert result["periods"][1]["filters"]["date_to"] == "2021-12-31"


@pytest.mark.parametrize("arguments", [
    {"group_by": "years"},
    {"group_by": "years", "ranking": "highest"},
    {"periods": [{"label": "Before 2020", "date_to": "2019-12-31"},
                 {"label": "2020 onward", "date_from": "2020-01-01"}]},
])
def test_actual_tool_output_passes_trusted_statistics_formatter(arguments):
    catalog, db = make_catalog(Filters(sponsors=["exxonmobil"]))
    attach_complete_read(db, periods=bool(arguments.get("periods")))
    data = catalog.call("record_statistics", arguments)
    assert data["method"] == "database"
    answer = Service._tool_statistics_answer(data, base_filters=catalog.base_filters)
    assert answer.status == "answered" and answer.answer_mode == "statistics"
    assert answer.structured_result == data
    assert "2" in answer.answer  # Missing dates are retained by every route.
    if arguments.get("periods"):
        assert "Before 2020" in answer.answer and "2020 onward" in answer.answer
    elif arguments.get("ranking") == "highest":
        assert "2018" in answer.answer and "2020" in answer.answer
    else:
        assert {row["name"] for row in answer.structured_result["groups"]} == {"2018", "2020"}


@pytest.mark.parametrize("arguments", [
    {"ranking": "highest"}, {"group_by": "years", "measure": "share"},
    {"periods": [{"label": "A"}, {"label": "B", "date_to": "2019-12-31"}]},
    {"periods": [{"label": "A", "date_from": "2020-01-01", "date_to": "2019-01-01"},
                 {"label": "B", "date_to": "2021-01-01"}]},
    {"periods": [{"label": "Same", "date_to": "2019-01-01"},
                 {"label": "same", "date_from": "2020-01-01"}]},
    {"group_by": "years", "periods": [{"label": "A", "date_to": "2019-01-01"},
                                      {"label": "B", "date_from": "2020-01-01"}]},
    {"periods": [{"label": str(i), "date_from": "2020-01-01"} for i in range(4)]},
])
def test_incompatible_or_unbounded_statistics_are_rejected_before_reads(arguments):
    catalog, db = make_catalog()
    assert catalog.call("record_statistics", arguments)["status"] == "invalid_request"
    assert db.reads == []


def test_source_only_setting_and_supplement_counts_are_explicit():
    for include in (False, True):
        catalog, db = make_catalog(Filters(include_inferred_dates=include))
        attach_complete_read(db, inferred=2 if include else 0)
        result = catalog.call("record_statistics", {"group_by": "years"})
        assert db.statistics_reads[0][0].include_inferred_dates is include
        assert result["date_inference"]["enabled"] is include
        assert result["date_inference"]["used"] == (2 if include else 0)
        assert result["date_basis"] == ("source_or_supplemented" if include else "source_only")


def test_missing_collection_is_omitted_instead_of_zero_in_periods():
    catalog, db = make_catalog(Filters(dataset="all"))
    attach_complete_read(db, periods=True)
    read = db.research_statistics

    def with_missing(*args, **kwargs):
        result = read(*args, **kwargs)
        result["collections"].append({"dataset": "social", "total": 0, "unknown_dates": 0})
        for period in result["periods"]:
            period["collections"].append({"dataset": "social", "total": 0})
        return result

    db.research_statistics = with_missing
    result = catalog.call("record_statistics", {"periods": [
        {"label": "Earlier", "date_to": "2019-12-31"},
        {"label": "Later", "date_from": "2020-01-01"}]})
    assert [row["dataset"] for row in result["collections"]] == ["native"]
    assert all([row["dataset"] for row in period["collections"]] == ["native"]
               for period in result["periods"])


def test_exact_short_name_does_not_hide_related_candidates_or_merge_unrelated_names():
    catalog, db = make_catalog()
    names = ["williams", "williams companies", "the williams companies, inc.",
             "williams plumbing", "williamson"]
    db.rows = [source(str(i), "The Washington Post", name) for i, name in enumerate(names)]
    result = catalog.call("resolve_entity", {"query": "Williams", "entity_type": "sponsor"})
    assert result["status"] == "clarify" and result["candidate_count"] == 4
    assert {row["source_value"] for row in result["candidates"]} == set(names[:-1])
    assert result["candidates"][0]["source_value"] == "williams"
    assert all(row["identity_status"] == "source_candidate_not_resolved" for row in result["candidates"])
    context = catalog.entity_context()
    hint = next(row for row in context["ambiguity_hints"] if row["query"] == "williams")
    assert set(hint["source_candidates"]) == set(names[:-1])


def test_exact_active_source_selection_remains_available():
    catalog, db = make_catalog(Filters(sponsors=["williams"]))
    db.rows = [source("w", "The Washington Post", "williams"),
               source("wc", "The Washington Post", "williams companies")]
    assert catalog.call("resolve_entity", {"query": "Williams", "entity_type": "sponsor"})["status"] == "ok"
    result = catalog.call("record_statistics", {"filters": {"sponsors": ["williams"]}})
    assert result["status"] == "ok" and result["collections"][0]["total"] == 1
    assert catalog.entity_context()["ambiguity_hints"] == []


def test_short_sponsor_cannot_bypass_ambiguity_by_selecting_exact_spelling():
    catalog, db = make_catalog()
    names = ["williams", "williams companies", "the williams companies, inc."]
    db.rows = [source(str(i), "The Washington Post", name) for i, name in enumerate(names)]
    result = catalog.call("record_statistics", {"filters": {"sponsors": ["williams"]}})
    assert result["status"] == "clarify" and "collections" not in result
    assert all(name in result["message"] for name in names)
    explicit = catalog.call("record_statistics", {"filters": {"sponsors": names}})
    assert explicit["status"] == "ok" and explicit["collections"][0]["total"] == 3
    assert explicit["filters"]["sponsors"] == names


def test_unfamiliar_short_name_and_unrelated_prefix_are_not_silently_combined():
    catalog, db = make_catalog()
    names = ["northstar", "northstar energy", "northstar plumbing", "northstarlight"]
    db.rows = [source(str(i), "The Washington Post", name) for i, name in enumerate(names)]
    resolved = catalog.call("resolve_entity", {"query": "Northstar", "entity_type": "sponsor"})
    assert resolved["candidate_count"] == 3
    assert {row["source_value"] for row in resolved["candidates"]} == set(names[:-1])
    incomplete = catalog.call("record_statistics", {"filters": {"sponsors": names[:2]}})
    assert incomplete["status"] == "clarify" and "northstar plumbing" in incomplete["message"]
    explicit = catalog.call("record_statistics", {"filters": {"sponsors": names[:3]}})
    assert explicit["status"] == "ok" and explicit["collections"][0]["total"] == 3
    assert set(explicit["filters"]["sponsors"]) == set(names[:3])


class ScriptedConnection:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, deepcopy(params)))
        rows = [] if sql.startswith("SET TRANSACTION") else next(self.results)
        return SimpleNamespace(fetchall=lambda: deepcopy(rows))


def test_year_sql_uses_one_read_only_snapshot_and_all_ties(monkeypatch):
    conn = ScriptedConnection([[collection()], [
        {"dataset": "native", "name": "2018", "count": 3},
        {"dataset": "native", "name": "2020", "count": 3}], [], []])
    db = Database("unused")
    connections = []
    monkeypatch.setattr(db, "connect", lambda: connections.append(conn) or conn)
    result = db.research_statistics(Filters(), ranking="highest")
    assert len(connections) == 1
    assert conn.calls[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    group_sql = conn.calls[2][0]
    assert "dense_rank()" in group_sql and "PARTITION BY dataset" in group_sql and "position=1" in group_sql
    assert "effective_date IS NOT NULL" in group_sql
    assert len(result["groups"]) == 2
    assert result["collections"][0]["unknown_dates"] == 2


def test_multi_period_sql_keeps_every_query_in_one_snapshot_with_bound_parameters(monkeypatch):
    conn = ScriptedConnection([[collection()], [collection(2, 0)], [collection(4, 0)], [], []])
    db = Database("unused")
    monkeypatch.setattr(db, "connect", lambda: conn)
    base = Filters(sponsors=["O'Reilly"], include_inferred_dates=False)
    earlier = base.model_copy(update={"date_to": date(2019, 12, 31), "include_unknown_dates": False})
    later = base.model_copy(update={"date_from": date(2020, 1, 1), "include_unknown_dates": False})
    result = db.research_statistics(base, group_by="none", periods=[
        {"label": "Earlier", "filters": earlier}, {"label": "Later", "filters": later}])
    assert [row["collections"][0]["total"] for row in result["periods"]] == [2, 4]
    for query, params in conn.calls[1:]:
        assert "O'Reilly" not in query and ["O'Reilly"] in params
    assert date(2019, 12, 31) in conn.calls[2][1]
    assert date(2020, 1, 1) in conn.calls[3][1]
    assert sum(query.startswith("SET TRANSACTION") for query, _ in conn.calls) == 1


def test_public_date_projection_preserves_source_and_effective_basis():
    db = Database("unused")
    source_sql, _ = db._public_query(Filters(include_inferred_dates=False))
    supplemented_sql, _ = db._public_query(Filters(include_inferred_dates=True))
    assert "v.payload->>'published_at' AS date" in source_sql
    assert "AS source_date" in source_sql and "AS effective_date" in source_sql
    assert "WHEN false AND di.method" in source_sql
    assert "COALESCE(NULLIF(v.payload->>'published_at',''),di.inferred_date::text) AS effective_date" in supplemented_sql
    assert "d.precision='day'" in source_sql and "WHEN true AND di.method" in supplemented_sql


def test_tool_and_prompt_expose_general_statistics_not_specific_question_ids():
    schema = StatisticsRequest.model_json_schema()
    assert "years" in schema["properties"]["group_by"]["anyOf"][0]["enum"]
    assert schema["properties"]["periods"]["anyOf"][0]["maxItems"] == 3
    for instruction in ("group_by='years'", "ranking='highest'", "every tied", "periods",
                        "ambiguity_hints", "Never silently combine"):
        assert instruction in SYSTEM
