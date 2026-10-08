"""Public legacy counts distinguish unavailable admission from a scoped zero.

All rows, health maps and transports are synthetic. No database, model or
network is used; admission maps follow the real Database.health contract.
"""

from copy import deepcopy
from datetime import date

import pytest
from test_statistics_answers import NoPaidCalls, StatisticsOnlyDB, record

from observatory.config import Settings
from observatory.models import Filters
from observatory.service import Service
from observatory.structured_queries import QuestionPlan


class AdmissionDB(StatisticsOnlyDB):
    def __init__(self, rows, health_counts):
        super().__init__(rows)
        self.health_counts = deepcopy(health_counts)

    def health(self):
        return {"status": "ok", "data_version": self.version, **deepcopy(self.health_counts)}


def legacy_service(health_counts, *, rows=None):
    if rows is None:
        rows = [record("n1", "native", "The New York Times", "exxonmobil"),
                record("s1", "social", "The New York Times", "exxonmobil", countable=False)]
    db, rag = AdmissionDB(rows, health_counts), NoPaidCalls()
    service = Service(Settings(research_agent_enabled=False), db=db, rag=rag)
    web_calls = []

    def forbidden_web(*args, **kwargs):
        web_calls.append((args, kwargs))
        raise AssertionError("A missing collection admission must not become a web count")

    service._search_external = forbidden_web
    return service, db, rag, web_calls


@pytest.mark.parametrize("counts,scope", [
    ({"record_counts": {"native": 1, "social": 1},
      "countable_record_counts": {"native": 1, "social": 0}}, Filters(dataset="social")),
    ({"record_counts": {"native": 1, "social": 1},
      "countable_record_counts": {}}, Filters(dataset="social")),
    ({"record_counts": {"native": 1, "social": 1},
      "countable_record_counts": {"native": 0, "social": 0}}, Filters(dataset="all")),
    ({}, Filters(dataset="social")),
    ({}, Filters(dataset="native")),
    ({}, Filters(dataset="all")),
])
def test_unavailable_admission_is_not_an_answered_zero_or_web_total(counts, scope):
    service, db, rag, web_calls = legacy_service(counts)
    result = service.answer("How many records?", scope, "visitor")
    assert result.status == "service_unavailable" and result.answer_mode == "statistics"
    assert result.failure_reason == "collection_unavailable"
    assert result.structured_result is None
    assert "no advertisement count has been established" in result.answer
    assert "0 social ad records" not in result.answer
    assert not db.dashboard_calls and not db.facet_calls
    assert not db.forbidden_calls and not rag.calls and not web_calls
    assert result.cost_usd == 0 and not result.external_research


@pytest.mark.parametrize("admitted,missing", [("native", "social"), ("social", "native")])
def test_combined_keeps_requested_scope_and_omits_unadmitted_collection(admitted, missing):
    rows = [record("n1", "native", "The New York Times", "exxonmobil"),
            record("s1", "social", "The New York Times", "exxonmobil")]
    service, db, rag, web_calls = legacy_service({
        "record_counts": {"native": 1, "social": 1},
        "countable_record_counts": {admitted: 1, missing: 0},
    }, rows=rows)
    scope = Filters(dataset="all", sponsors=["exxonmobil"], record_ids=["n1", "s1"],
                    date_from=date(2021, 1, 1), include_unknown_dates=False,
                    include_inferred_dates=False)
    before = scope.model_dump(mode="json")
    result = service.answer("How many records?", scope, "visitor")
    data = result.structured_result
    assert result.status == "answered" and result.answer_mode == "statistics"
    assert data["filters"] == before and scope.model_dump(mode="json") == before
    assert data["missing_datasets"] == [missing]
    assert data["collections"] == [{"dataset": admitted, "total": 1, "retrievable": 1, "unknown_dates": 0}]
    assert {item["dataset"] for item in data["records"]} == {admitted}
    assert [item.dataset for item in db.dashboard_calls] == [admitted]
    assert any("omitted" in note and "not a zero" in note for note in data["scope_notes"])
    assert f"The {'company social posts' if missing == 'social' else 'native advertising'} collection" in result.answer
    assert "is omitted" in result.answer and "0 eligible" not in result.answer
    assert data["data_version"] == db.version
    assert not rag.calls and not web_calls


@pytest.mark.parametrize("dataset", ["native", "social", "all"])
def test_loaded_collection_empty_filter_intersection_is_a_legitimate_zero(dataset):
    service, db, rag, web_calls = legacy_service({
        "record_counts": {"native": 1, "social": 1},
        "countable_record_counts": {"native": 1, "social": 1},
    })
    scope = Filters(dataset=dataset, keywords=["absent-keyword"], include_inferred_dates=False)
    result = service.answer("How many records?", scope, "visitor")
    assert result.status == "answered" and result.answer_mode == "statistics"
    assert all(item["total"] == 0 for item in result.structured_result["collections"])
    assert "missing_datasets" not in result.structured_result
    assert result.structured_result["filters"] == scope.model_dump(mode="json")
    assert "No eligible records match" in result.answer
    assert len(db.dashboard_calls) == (2 if dataset == "all" else 1)
    assert not rag.calls and not web_calls


def test_absent_countable_map_uses_active_record_counts_for_legacy_compatibility():
    service, db, rag, web_calls = legacy_service({"record_counts": {"native": 1, "social": 0}})
    result = service.answer("How many records?", Filters(dataset="all"), "visitor")
    assert result.status == "answered"
    assert result.structured_result["missing_datasets"] == ["social"]
    assert result.structured_result["collections"][0]["total"] == 1
    assert [item.dataset for item in db.dashboard_calls] == ["native"]
    assert not rag.calls and not web_calls


def test_named_entity_resolution_cannot_bypass_admission_gate():
    service, db, rag, web_calls = legacy_service({
        "record_counts": {"native": 1, "social": 1},
        "countable_record_counts": {"native": 1, "social": 0},
    })
    result = service.answer("How many ads from NYT?", Filters(dataset="social"), "visitor")
    assert result.status == "service_unavailable" and result.failure_reason == "collection_unavailable"
    assert not db.facet_calls and not db.dashboard_calls
    assert not rag.calls and not web_calls


def test_available_combined_group_list_contains_only_admitted_dataset():
    service, db, rag, web_calls = legacy_service({
        "record_counts": {"native": 1, "social": 1},
        "countable_record_counts": {"native": 1, "social": 0},
    })
    result = service.answer("Which publishers is ExxonMobil working with?", Filters(dataset="all"), "visitor")
    assert result.status == "answered"
    assert result.structured_result["filters"]["dataset"] == "all"
    assert result.structured_result["groups"] == [{"dataset": "native", "name": "The New York Times",
                                                  "count": 1, "display_name": "The New York Times"}]
    assert [item.dataset for item in db.dashboard_calls] == ["native"]
    assert not rag.calls and not web_calls


def test_admission_snapshot_does_not_weaken_source_version_guard():
    service, db, rag, web_calls = legacy_service({"countable_record_counts": {"native": 1, "social": 0}})
    dashboard = db.dashboard

    def changed(*args, **kwargs):
        result = dashboard(*args, **kwargs)
        db.version = "source-v2"
        return result

    db.dashboard = changed
    result = service.answer("How many records?", Filters(dataset="all"), "visitor")
    assert result.status == "service_unavailable"
    assert result.failure_reason == "data_changed_during_statistics" and result.structured_result is None
    assert not rag.calls and not web_calls


def test_database_failure_cannot_replace_collection_statistics_with_a_web_answer():
    service, db, rag, web_calls = legacy_service({"countable_record_counts": {"native": 1, "social": 0}})

    def failed_dashboard(*args, **kwargs):
        raise RuntimeError("PRIVATE-SYNTHETIC-SQL-FAILURE")

    db.dashboard = failed_dashboard
    result = service.answer("How many records?", Filters(dataset="all"), "visitor")
    assert result.status == "service_unavailable" and result.answer_mode == "statistics"
    assert result.failure_reason == "statistics_unavailable" and result.structured_result is None
    assert "PRIVATE-SYNTHETIC" not in result.model_dump_json()
    assert not rag.calls and not web_calls and result.cost_usd == 0


def test_direct_private_statistics_default_does_not_add_a_health_dependency():
    service, db, rag, web_calls = legacy_service({})

    def forbidden_health():
        raise AssertionError("Direct private count rendering retains its existing caller contract")

    db.health = forbidden_health
    plan = QuestionPlan("ready", kind="count", filters=Filters(dataset="native"))
    result = service._statistics_answer(plan)
    assert result.status == "answered" and result.structured_result["collections"][0]["total"] == 1
    assert not rag.calls and not web_calls
