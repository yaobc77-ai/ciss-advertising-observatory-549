"""Real Dash HTTP checks for deferred reads and current, guarded social pages."""

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_app import callback, defaults
from test_social_accounts_ui import values
from test_social_label_ui import GREEN_FALSE, GREEN_TRUE, SourceStateService

from observatory.app import create_app


class MeasuredService(SourceStateService):
    def __init__(self):
        super().__init__()
        self.data_version = "current-data-v1"
        self.dashboard_calls = 0
        self.page_calls = 0
        self.version_calls = 0
        self.after_page = None
        self.fail_page = False
        self.after_dashboard = None
        self.health_failure = False

    def health(self):
        if self.health_failure:
            return {"status": "unavailable"}
        return super().health() | {"data_version": self.data_version}

    def social_source_state_version(self, filters):
        self.version_calls += 1
        return hashlib.sha256(json.dumps([
            (row["record_id"], row["version_id"], row["social_historical_states"])
            for row in self._selected(filters)
        ], sort_keys=True).encode()).hexdigest()

    def dashboard(self, *args, **kwargs):
        self.dashboard_calls += 1
        result = super().dashboard(*args, **kwargs)
        if self.after_dashboard:
            self.after_dashboard()
        return result

    def page(self, *args, **kwargs):
        self.page_calls += 1
        if self.fail_page:
            raise RuntimeError("PRIVATE_FAILURE_DETAIL")
        result = super().page(*args, **kwargs)
        if self.after_page:
            action, self.after_page = self.after_page, None
            action()
        return result


@pytest.fixture
def measured_app():
    service = MeasuredService()
    app = create_app(service, SimpleNamespace(
        show_source_links=True, cookie_secret="signed-page-tests", monthly_budget_usd=0,
    ))
    return app, app.server.test_client(), service


def load(application, selection, changed="social-accounts.value", status=200):
    app, client, _ = application
    return callback(app, client, "social-grid.rowData", selection, changed, expected_status=status)


def first_page(application):
    selection = values(**{
        "active-dataset.value": "social", "social-accounts.value": ["Shared name"],
        "social-platforms.value": ["Twitter"], "social-sponsors.value": ["Company A"],
        "social-labels.value": [GREEN_TRUE], "social-dates.start_date": "2024-01-01",
        "social-dates.end_date": "2024-12-31", "social-unknown-dates.value": [],
    })
    result = load(application, selection)
    return selection, result


def next_page(selection, result):
    return selection | {
        "social-network-data.data": deepcopy(result["social-network-data"]["data"]),
        "social-page-next.n_clicks": 1, "social-page-offset.data": 0,
    }


def identifiers(result):
    return [row["record_id"] for row in result["social-grid"]["rowData"]]


@pytest.mark.parametrize("dataset,active,path", [
    ("social", "native", "/data"), ("native", "social", "/data"),
    ("social", "social", "/query"), ("native", "native", "/evaluation"),
    ("social", None, "/data"), ("social", "social", None),
])
def test_invisible_collections_do_not_read_dashboard_or_pages(measured_app, dataset, active, path):
    app, client, service = measured_app
    selection = defaults(dataset) | {"active-dataset.value": active, "page-location.pathname": path}
    callback(app, client, f"{dataset}-grid.rowData", selection, "active-dataset.value", expected_status=204)
    assert service.dashboard_calls == service.page_calls == service.version_calls == 0


def test_current_next_page_keeps_members_and_does_not_recalculate_charts(measured_app):
    selection, initial = first_page(measured_app)
    service = measured_app[2]
    result = load(measured_app, next_page(selection, initial), "social-page-next.n_clicks")
    assert identifiers(initial) + identifiers(result) == [f"social-{i}" for i in range(24)]
    assert result["social-page-label"]["children"] == "21–24 of 24"
    assert service.dashboard_calls == 1 and service.page_calls == 1
    assert set(result) == {"social-grid", "social-page-prev", "social-page-next", "social-page-label", "social-page-offset"}
    assert not any(key in json.dumps(result) for key in ("PRIVATE_BODY", "PRIVATE_ANNOTATION", "raw"))


@pytest.mark.parametrize("field,new_value", [("sort", "title:asc"), ("page-size", 50)])
def test_sort_or_size_reads_only_the_page_for_the_same_scope(measured_app, field, new_value):
    selection, initial = first_page(measured_app)
    request = next_page(selection, initial) | {f"social-{field}.value": new_value, "social-page-offset.data": 20}
    result = load(measured_app, request, f"social-{field}.value")
    expected_size = 24 if field == "page-size" else 20
    assert identifiers(result) == [f"social-{i}" for i in range(expected_size)]
    assert result["social-page-offset"]["data"] == 0
    assert measured_app[2].dashboard_calls == 1 and measured_app[2].page_calls == 1


def test_changed_filters_or_metric_recalculate_current_full_selection(measured_app):
    selection, initial = first_page(measured_app)
    result = load(measured_app, next_page(selection, initial) | {
        "social-labels.value": [GREEN_FALSE],
    }, "social-labels.value")
    assert identifiers(result) == []
    assert result["social-record-count"]["children"].startswith("0 unique posts")
    assert measured_app[2].dashboard_calls == 2 and measured_app[2].page_calls == 0
    result = load(measured_app, next_page(selection, initial) | {"social-metric.value": "percent"}, "social-metric.value")
    assert measured_app[2].dashboard_calls == 3
    assert "social-labels-chart" in result


def change_annotation(service):
    row = service.social[23]
    row["social_historical_states"] = [GREEN_FALSE if value == GREEN_TRUE else value for value in row["social_historical_states"]]


@pytest.mark.parametrize("change,during", [("annotation", False), ("annotation", True), ("data_version", False), ("data_version", True)])
def test_source_changes_before_or_during_paging_refresh_the_first_page(measured_app, change, during):
    selection, initial = first_page(measured_app)
    service = measured_app[2]

    def mutate():
        if change == "annotation":
            change_annotation(service)
        else:
            service.data_version = "current-data-v2"

    if during:
        service.after_page = mutate
    else:
        mutate()
    result = load(measured_app, next_page(selection, initial), "social-page-next.n_clicks")
    assert identifiers(result) == [f"social-{i}" for i in range(20)]
    assert result["social-page-offset"]["data"] == 0 and "Selection refreshed" in json.dumps(result["social-status"])
    assert service.dashboard_calls == 2
    assert result["social-network-data"]["data"]["social_historical_labels"]["total"] == (23 if change == "annotation" else 24)


def test_forged_browser_total_does_not_authorize_skipping_aggregates(measured_app):
    selection, initial = first_page(measured_app)
    request = next_page(selection, initial)
    request["social-network-data.data"]["social_historical_labels"]["total"] = 999
    result = load(measured_app, request, "social-page-next.n_clicks")
    assert result["social-page-label"]["children"] == "1–20 of 24"
    assert measured_app[2].dashboard_calls == 2 and measured_app[2].page_calls == 0


def test_new_browser_without_signed_session_recalculates_even_with_a_valid_store(measured_app):
    selection, initial = first_page(measured_app)
    app, _, service = measured_app
    fresh = (app, app.server.test_client(), service)
    result = load(fresh, next_page(selection, initial), "social-page-next.n_clicks")
    assert result["social-page-label"]["children"] == "1–20 of 24"
    assert service.dashboard_calls == 2 and service.page_calls == 0


def test_page_failure_withholds_records_and_private_error(measured_app):
    selection, initial = first_page(measured_app)
    measured_app[2].fail_page = True
    result = load(measured_app, next_page(selection, initial), "social-page-next.n_clicks")
    assert identifiers(result) == [] and result["social-page-label"]["children"] == "Unavailable"
    assert "PRIVATE_FAILURE_DETAIL" not in json.dumps(result)


def test_unknown_data_version_uses_full_reads(measured_app):
    measured_app[2].data_version = None
    selection, initial = first_page(measured_app)
    result = load(measured_app, next_page(selection, initial), "social-page-next.n_clicks")
    assert result["social-page-offset"]["data"] == 0
    assert measured_app[2].dashboard_calls == 2 and measured_app[2].page_calls == 0


@pytest.mark.parametrize("persistent", [False, True])
def test_change_during_full_dashboard_retries_once_or_withholds_results(measured_app, persistent):
    service = measured_app[2]

    def mutate():
        service.data_version += "-changed"
        service.social[0]["version_id"] += "-changed"
        if not persistent:
            service.after_dashboard = None

    service.after_dashboard = mutate
    _, result = first_page(measured_app)
    assert service.dashboard_calls == 2
    if persistent:
        assert identifiers(result) == [] and result["social-page-label"]["children"] == "Unavailable"
        assert "social_historical_labels" not in json.dumps(result)
    else:
        assert result["social-grid"]["rowData"][0]["version_id"].endswith("-changed")
        assert result["social-network-data"]["data"]["social_admission"]["data_version"] == service.data_version
        assert "Selection refreshed" in json.dumps(result["social-status"])


@pytest.mark.parametrize("failure", ["service", "admission"])
def test_second_health_failure_or_withdrawal_does_not_report_invalid_dates(measured_app, failure):
    selection, initial = first_page(measured_app)
    service = measured_app[2]

    def mutate():
        if failure == "service":
            service.health_failure = True
        else:
            service.admitted = False

    service.after_page = mutate
    result = load(measured_app, next_page(selection, initial), "social-page-next.n_clicks")
    assert identifiers(result) == [] and "Check the filters" not in json.dumps(result)
    if failure == "service":
        assert result["social-page-label"]["children"] == "Unavailable"
    else:
        assert "No admitted company social posts" in json.dumps(result)
