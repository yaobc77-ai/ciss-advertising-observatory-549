"""Percentages use independent source rows and a trusted, unchanged scope.

The SQL transport below is a snapshot stub, not a PostgreSQL integration test.
No database, model, or network is accessed.
"""

from collections import Counter
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_research_agent import harness, response
from test_research_service import setup_service
from test_research_tools import SourceDB, source

from observatory.config import Settings
from observatory.db import Database
from observatory.models import Filters
from observatory.research_agent import ResearchRun
from observatory.research_tools import ToolCatalog
from observatory.service import Service
from observatory.structured_queries import QuestionPlan, plan_question


class SnapshotDB(SourceDB):
    """Pair parameter-bound scopes within one immutable connection snapshot."""

    def __init__(self):
        super().__init__()
        self.scopes, self.transactions = [], []
        self.change_after_totals = False

    def health(self):
        return {"status": "ok", "record_counts": dict(Counter(row["dataset"] for row in self.rows)),
                "data_version": self.version}

    def _public_query(self, filters):
        index = len(self.scopes)
        self.scopes.append(filters.model_copy(deep=True))
        return f"SELECT * FROM pinned_scope_{index}", [index]

    def connect(self):
        db = self
        captured = deepcopy(self.rows)
        queries = []
        db.transactions.append(queries)

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                queries.append((sql, params))
                if sql.startswith("SET TRANSACTION"):
                    return None
                assert "JOIN denominator d USING(record_id,version_id)" in sql
                assert len(params) == 2
                view = SourceDB()
                view.rows = captured
                denominator = view.select(db.scopes[params[0]])
                keys = {(row["record_id"], row["version_id"]) for row in denominator}
                numerator = [row for row in view.select(db.scopes[params[1]])
                             if (row["record_id"], row["version_id"]) in keys]
                if "count(*) AS numerator" in sql:
                    counts = {"numerator": len(numerator), "denominator": len(denominator),
                              "retrievable": sum(row["retrievable"] for row in numerator),
                              "unknown_dates": sum(not row.get("date") for row in numerator)}
                    if db.change_after_totals:
                        for row in db.rows:
                            row["sponsor"] = "changed-after-snapshot"
                    return SimpleNamespace(fetchone=lambda: counts)
                assert "LIMIT 10" in sql
                return SimpleNamespace(fetchall=lambda: numerator[:10])

        return Connection()


def catalog(base=None, *, links=True):
    db = SnapshotDB()
    service = Service(Settings(show_source_links=links), db=db, rag=object())
    return ToolCatalog(service, base or Filters()), db


def share_call(tools, **filters):
    return tools.call("record_statistics", {"measure": "share", "group_by": "none", "filters": filters})


def test_target_outlet_does_not_become_its_own_denominator():
    tools, db = catalog()
    result = share_call(tools, publishers=["The New York Times"])
    assert result["status"] == "ok" and result["kind"] == "share"
    assert result["denominator_filters"] == Filters().model_dump(mode="json")
    assert result["denominator_basis"] == "current_selection_before_question_targets"
    assert result["filters"]["publishers"] == ["The New York Times"]
    assert result["collections"] == [{"dataset": "native", "total": 1, "numerator": 1,
        "denominator": 4, "percentage": 25.0, "percentage_status": "defined",
        "retrievable": 1, "unknown_dates": 0}]
    assert db.scopes[0].publishers == [] and db.scopes[1].publishers == ["The New York Times"]
    assert [row["record_id"] for row in result["records"]] == ["r3"]
    assert "private" not in str(result) and "raw" not in str(result)


def test_preselected_outlet_remains_denominator_and_input_is_immutable():
    base = Filters(publishers=["The Washington Post"])
    tools, db = catalog(base)
    base.publishers.clear()  # A later caller mutation cannot alter the bound scope.
    result = share_call(tools, sponsors=["exxonmobil"])
    item = result["collections"][0]
    assert (item["numerator"], item["denominator"]) == (1, 3)
    assert item["percentage"] == pytest.approx(100 / 3)
    assert result["denominator_filters"]["publishers"] == ["The Washington Post"]
    assert db.scopes[0].sponsors == [] and db.scopes[1].sponsors == ["exxonmobil"]
    # A display alias never merges the distinct source spelling ExxonMobil.
    assert [row["record_id"] for row in result["records"]] == ["r1"]


def test_question_date_and_unknown_exclusion_narrow_numerator_only():
    tools, _ = catalog()
    result = share_call(tools, date_from="2021-01-01", date_to="2021-12-31")
    assert (result["collections"][0]["numerator"], result["collections"][0]["denominator"]) == (1, 4)
    assert result["filters"]["include_unknown_dates"] is False
    assert result["denominator_filters"]["include_unknown_dates"] is True
    assert result["denominator_filters"]["date_from"] is None


def test_trusted_date_record_and_metadata_constraints_stay_in_denominator():
    base = Filters(record_ids=["r1", "r2", "r4"], publishers=["The Washington Post"],
                   date_from="2020-01-01", date_to="2020-12-31", include_unknown_dates=False)
    tools, _ = catalog(base)
    result = share_call(tools, sponsors=["ExxonMobil"])
    assert result["denominator_filters"] == base.model_dump(mode="json")
    assert (result["collections"][0]["numerator"], result["collections"][0]["denominator"]) == (1, 2)


def test_unsearchable_unknown_date_target_still_counts():
    tools, _ = catalog()
    result = share_call(tools, sponsors=["bp"])
    item = result["collections"][0]
    assert (item["numerator"], item["denominator"], item["percentage"]) == (1, 4, 25)
    assert item["retrievable"] == 0 and item["unknown_dates"] == 1


def test_empty_selection_is_undefined_and_empty_target_is_zero_percent():
    tools, _ = catalog(Filters(record_ids=["absent"]))
    empty = share_call(tools)["collections"][0]
    assert empty["numerator"] == empty["denominator"] == 0
    assert empty["percentage"] is None and empty["percentage_status"] == "empty_selection"
    tools, _ = catalog()
    zero = share_call(tools, publishers=["The New York Times"], sponsors=["bp"])["collections"][0]
    assert (zero["numerator"], zero["denominator"], zero["percentage"]) == (0, 4, 0)


def test_native_and_social_units_never_share_a_denominator():
    tools, db = catalog(Filters(dataset="all"))
    db.rows.extend([{**source("s1", "The New York Times", "bp"), "dataset": "social"},
                    {**source("s2", "Other Outlet", "bp"), "dataset": "social"}])
    result = share_call(tools, publishers=["The New York Times"])
    assert [(item["dataset"], item["numerator"], item["denominator"], item["percentage"])
            for item in result["collections"]] == [("native", 1, 4, 25), ("social", 1, 2, 50)]
    assert len(db.transactions) == 2
    assert "total" not in result  # No combined article/post denominator.


def test_explicit_native_target_in_all_scope_uses_native_denominator():
    tools, db = catalog(Filters(dataset="all"))
    db.rows.append({**source("s1", "The New York Times", "bp"), "dataset": "social"})
    result = share_call(tools, dataset="native", publishers=["The New York Times"])
    assert [(item["dataset"], item["denominator"]) for item in result["collections"]] == [("native", 4)]
    assert result["denominator_filters"]["dataset"] == "all"
    assert all(scope.dataset == "native" for scope in db.scopes)


def test_unloaded_social_is_omitted_not_zero_percent():
    tools, _ = catalog(Filters(dataset="all"))
    result = share_call(tools, publishers=["The New York Times"])
    assert result["status"] == "ok" and result["missing_datasets"] == ["social"]
    assert [item["dataset"] for item in result["collections"]] == ["native"]
    assert any("Unloaded" in note for note in result["scope_notes"])
    tools, db = catalog(Filters(dataset="social"))
    assert share_call(tools)["status"] == "unavailable" and db.scopes == []


@pytest.mark.parametrize("args,status", [
    ({"measure": "share", "denominator_filters": {"publishers": ["The New York Times"]}}, "invalid_request"),
    ({"measure": "share", "percentage": 99}, "invalid_request"),
    ({"measure": "share", "group_by": "publishers"}, "clarify"),
    ({"measure": "share", "filters": {"publishers": ["Missing Outlet"]}}, "clarify"),
])
def test_no_model_denominator_number_grouping_or_unknown_category(args, status):
    tools, db = catalog()
    assert tools.call("record_statistics", args)["status"] == status
    assert db.scopes == []


def test_target_cannot_escape_scope_and_links_follow_public_setting():
    tools, db = catalog(Filters(publishers=["The Washington Post"]))
    assert share_call(tools, publishers=["The New York Times"])["status"] == "clarify"
    assert db.scopes == []
    tools, _ = catalog(links=False)
    result = share_call(tools, sponsors=["bp"])
    assert all(row["url"] == row["archive_url"] == "" for row in result["records"])


def test_counts_and_examples_share_one_read_only_snapshot():
    tools, db = catalog()
    db.change_after_totals = True
    result = share_call(tools, sponsors=["bp"])
    assert result["status"] == "ok"
    assert len(result["records"]) == result["collections"][0]["numerator"] == 1
    assert result["collections"][0]["denominator"] == 4
    assert len(db.transactions) == 1
    assert db.transactions[0][0] == ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY", None)
    assert len(db.transactions[0]) == 3


def test_share_query_reuses_production_eligibility_and_bound_parameters():
    base = Filters(sponsors=["exxonmobil"], labels=["label-a"], date_from="2020-01-01")
    target = base.model_copy(update={"publishers": ["Publisher'; DROP TABLE records; --"]})
    queries = []
    row = source("r1", target.publishers[0], "exxonmobil")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            queries.append((sql, params))
            if sql.startswith("SET TRANSACTION"):
                return None
            if "count(*) AS numerator" in sql:
                return SimpleNamespace(fetchone=lambda: {
                    "numerator": 1, "denominator": 3, "retrievable": 1, "unknown_dates": 0,
                })
            return SimpleNamespace(fetchall=lambda: [deepcopy(row)])

    db = Database("")
    db.connect = lambda: Connection()
    db.health = lambda: {"status": "ok", "record_counts": {"native": 3}}
    service = Service(Settings(), db=db, rag=object())
    result = service._statistics_answer(QuestionPlan("ready", kind="share", filters=target,
                                                    denominator_filters=base))
    assert result.structured_result["collections"][0]["percentage"] == pytest.approx(100 / 3)
    base_sql, base_params = db._public_query(base)
    target_sql, target_params = db._public_query(target)
    assert "r.active" in base_sql and "countable" in base_sql and "EXISTS (SELECT 1 FROM annotations" in base_sql
    for sql, params in queries[1:]:
        assert base_sql in sql and target_sql in sql
        assert params == [*base_params, *target_params]
        assert target.publishers[0] not in sql  # Target stays a parameter, never SQL text.


def share_result():
    tools, _ = catalog()
    return share_call(tools, publishers=["The New York Times"])


@pytest.mark.parametrize("change", [
    lambda data: data.update(denominator_filters=data["filters"]),
    lambda data: data["collections"][0].update(percentage=100),
    lambda data: data["collections"][0].update(numerator=5),
    lambda data: data["collections"][0].update(denominator=True),
    lambda data: data["collections"].append(deepcopy(data["collections"][0])),
    lambda data: data.update(denominator_basis="model_defined"),
    lambda data: data.update(groups=[{"name": "NYT", "count": 1}]),
])
def test_service_refuses_forged_denominator_or_arithmetic(change):
    data = share_result()
    change(data)
    with pytest.raises(ValueError):
        Service._tool_statistics_answer(data, base_filters=Filters())


def test_share_prose_reports_actual_percentage_and_denominator_not_raw_count():
    data = share_result()
    result = Service._tool_statistics_answer(data, base_filters=Filters())
    assert "25.00% (1 of 4 eligible native ad records)" in result.answer
    assert result.answer_mode == "statistics" and result.structured_result == data
    service, db, _ = setup_service(ResearchRun(route="statistics", result=data))
    result = service.answer("What percentage is from NYT?", Filters(), "visitor")
    assert result.status == "answered" and "25.00%" in result.answer
    assert not db.searches


def test_invalid_share_on_model_route_is_unavailable_without_raising_or_rag():
    data = share_result()
    data["denominator_filters"] = data["filters"]
    service, db, _ = setup_service(ResearchRun(route="statistics", result=data))
    result = service.answer("What percentage is from NYT?", Filters(), "visitor")
    assert result.status == "service_unavailable" and not db.searches


def test_fake_model_calls_real_share_tool_and_cannot_write_the_percentage():
    tools, _ = catalog()
    agent, _, _, calls = harness([response("record_statistics", {
        "measure": "share", "group_by": "none", "filters": {"publishers": ["The New York Times"]},
    })], catalog=tools)
    result = agent.run("What percentage of native ads are from NYT?", Filters(), "visitor")
    assert result.route == "statistics" and result.result["collections"][0]["percentage"] == 25
    assert result.result["denominator_filters"] == Filters().model_dump(mode="json")
    assert "denominator" in calls[0]["input"][0]["content"]


def test_legacy_share_plan_keeps_trusted_scope_and_uses_current_facets():
    tools, _ = catalog(Filters(sponsors=["exxonmobil"]))
    base = tools.base_filters
    plan = plan_question("What percentage of native ads are from NYT?", base, tools.service.facets("native"))
    assert plan.status == "ready" and plan.kind == "share" and plan.group_by is None
    assert plan.denominator_filters == base and plan.filters.publishers == ["The New York Times"]
    assert plan.denominator_filters.publishers == []
    answer = tools.service.answer("What percentage of native ads are from NYT?", base, "visitor")
    assert answer.status == "answered" and "50.00% (1 of 2" in answer.answer


@pytest.mark.parametrize("question", [
    "What percentage of native ads are from NYT in 2021?",
    "Within 2021, what percentage of native ads are from NYT?",
    "What percentage of greenwashing ads are from NYT?",
    "What percentage of native ads are from Missing Outlet?",
])
def test_different_ambiguous_or_unsupported_denominators_never_become_counts(question):
    tools, db = catalog()
    result = tools.service.answer(question, Filters(), "visitor")
    assert result.answer_mode == "clarification" and result.status == "insufficient_evidence"
    assert not db.scopes


@pytest.mark.parametrize("target", [
    Filters(dataset="all"), Filters(publishers=[]), Filters(publishers=["The New York Times"]),
    Filters(publishers=["The Washington Post"], date_from="2019-01-01"),
])
def test_share_service_rejects_non_subset_plan_before_sql(target):
    tools, db = catalog()
    denominator = Filters(publishers=["The Washington Post"], date_from="2020-01-01")
    with pytest.raises(ValueError):
        tools.service._statistics_answer(QuestionPlan("ready", kind="share", filters=target,
                                                    denominator_filters=denominator))
    assert not db.scopes


def test_count_and_list_contracts_remain_unchanged_without_share_measure():
    tools, _ = catalog()
    count = tools.call("record_statistics", {"filters": {"publishers": ["The Washington Post"]}})
    assert count["kind"] == "count" and count["collections"][0]["total"] == 3
    listed = tools.call("record_statistics", {"measure": "count", "group_by": "sponsors",
                                             "filters": {"publishers": ["The Washington Post"]}})
    assert listed["kind"] == "list_sponsors"
    assert {row["name"]: row["count"] for row in listed["groups"]} == {"exxonmobil": 1, "ExxonMobil": 1, "bp": 1}
    assert "denominator_filters" not in count and "denominator_filters" not in listed
