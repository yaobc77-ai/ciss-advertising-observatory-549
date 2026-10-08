"""Unified selection reaches the existing query tools without widening scope."""

import json
from collections import Counter
from types import SimpleNamespace

import pytest
from test_app import FakeService, callback, component_tree, defaults, research_values
from test_dashboard_aggregate_reuse import (  # noqa: F401
    FixtureDatabase,
    fixture_rows,
    readonly_pg,
)

from observatory.app import _collection_filter_values, _filters, create_app
from observatory.models import Filters


@pytest.fixture
def application():
    service = FakeService()
    app = create_app(service, SimpleNamespace(show_source_links=True, cookie_secret="combined-scope-tests"))
    return app, app.server.test_client(), service


def test_default_collection_is_unified_and_retains_specific_exploration(application):
    _app, client, _service = application
    response = client.get("/_dash-layout")
    components = {node["props"]["id"]: node["props"] for node in component_tree(response.json)
                  if "id" in node["props"]}
    assert components["active-dataset"]["value"] == "all"
    assert [o["value"] for o in components["active-dataset"]["options"]] == ["all", "native", "social"]
    assert {"all-panel", "all-filter-panel", "native-panel", "social-panel"} <= components.keys()
    assert "record_type" in {column["field"] for column in components["all-grid"]["columnDefs"]}


def test_collection_switch_shows_unified_records_and_preserves_all_filter_values(application):
    app, client, _service = application
    response = callback(app, client, "native-panel.style", {"active-dataset.value": "all"}, "active-dataset.value")
    assert response["all-panel"]["style"] == response["all-filter-panel"]["style"] == {}
    assert response["native-panel"]["style"] == response["social-panel"]["style"] == {"display": "none"}
    assert all(set(value) == {"style"} for value in response.values())


def test_unified_filter_values_are_a_third_independent_scope():
    assert list(_collection_filter_values("all", list(range(27)))) == list(range(18, 27))
    for field in ("publishers", "platforms", "labels", "accounts"):
        values = [[], [], [], [], [], [], None, None, ["include"]]
        values[("publishers", "sponsors", "platforms", "keywords", "labels", "accounts").index(field)] = ["hidden"]
        with pytest.raises(ValueError, match="specific collection"):
            _filters("all", *values)


@pytest.mark.parametrize("button", ["search-free", "answer-paid"])
def test_both_query_paths_keep_unified_filters_and_ignore_other_collection_controls(application, button):
    app, client, service = application
    selection = research_values(dataset="all") | defaults("all") | {
        "all-sponsors.value": ["Sponsor A"], "all-keywords.value": ["CCS"],
        "all-dates.start_date": "2020-01-01", "all-dates.end_date": "2025-01-01",
        "all-unknown-dates.value": [], "native-sponsors.value": ["different-native"],
        "social-sponsors.value": ["different-social"], "page-location.pathname": "/query",
        f"{button}.n_clicks": 1,
    }
    response = callback(app, client, "research-results.children", selection, f"{button}.n_clicks")
    filters = (service.search_calls[-1] if button == "search-free" else service.answer_calls[-1])[1]
    assert filters.dataset == "all" and filters.sponsors == ["Sponsor A"]
    assert filters.keywords == ["CCS"] and filters.include_unknown_dates is False
    assert str(filters.date_from) == "2020-01-01" and str(filters.date_to) == "2025-01-01"
    assert response["research-submission"]["data"]["filters"] == filters.model_dump(mode="json")


def test_changing_unified_filters_marks_previous_results_stale(application):
    app, client, _service = application
    selection = research_values(dataset="all") | defaults("all") | {"page-location.pathname": "/query"}
    result = callback(app, client, "research-results.children", selection, "search-free.n_clicks")
    old = result["research-submission"]["data"]
    assert callback(app, client, "research-stale.children", selection | {"research-submission.data": old},
                    "research-submission.data")["research-stale"]["children"] is None
    changed = callback(app, client, "research-stale.children", selection | {
        "research-submission.data": old, "all-keywords.value": ["different"],
    }, "all-keywords.value")
    assert "earlier selection" in json.dumps(changed)


@pytest.mark.integration
@pytest.mark.parametrize("rows", [[], fixture_rows(), [r for r in fixture_rows() if r["dataset"] == "native"]])
def test_readonly_combined_aggregates_have_complete_type_denominators(readonly_pg, rows):  # noqa: F811
    result = FixtureDatabase(readonly_pg, rows).dashboard(Filters(dataset="all"), limit=3)
    combined = result["combined"]
    assert sum(c["total"] for c in combined["collections"]) == len(rows) == result["page"]["total"]
    for item in combined["collections"]:
        selected = [r for r in rows if r["dataset"] == item["dataset"]]
        assert item["total"] == len(selected)
        assert item["retrievable"] == sum(r["retrievable"] for r in selected)
        assert item["unknown_dates"] == sum(not r["effective_date"] for r in selected)
        assert sum(c[item["dataset"]] for c in combined["companies"]) == len(selected)
        expected = Counter((r["effective_date"] or "Unknown")[:4] if r["effective_date"] else "Unknown"
                           for r in selected)
        assert {v["year"]: v["count"] for v in combined["timeline"] if v["dataset"] == item["dataset"]} == expected
    assert "PRIVATE_FIXTURE" not in json.dumps(combined)
