"""Social source states through offline Dash HTTP; no database or model calls."""

import csv
import json
from copy import deepcopy
from io import StringIO
from types import SimpleNamespace

import pytest
from test_app import callback, component_tree
from test_social_accounts_ui import SocialService, refresh, values

from observatory.app import NATIVE_COLUMNS, create_app
from observatory.models import Filters
from observatory.service import Service
from observatory.social_annotations import (
    SCHEME,
    STATUS,
    social_label_metadata,
    social_state_id,
)

GREEN_TRUE = social_state_id("green_binary", "source_true")
GREEN_FALSE = social_state_id("green_binary", "source_false")
FOSSIL_FALSE = social_state_id("fossil_fuel_binary", "source_false")
GREEN_UNKNOWN = social_state_id("green_binary", "unknown")


class SourceStateService(SocialService):
    def __init__(self):
        super().__init__()
        # 27 usable annotations, including three entirely False exports; three missing annotations.
        for index, row in enumerate(self.social):
            states = []
            for code in social_label_metadata():
                state = "unknown" if index in (26, 27, 28) else "source_false"
                if index < 24 and code["key"] == "green_binary":
                    state = "source_true"
                if index < 12 and code["key"] == "decreasing_emissions":
                    state = "source_true"
                states.append(social_state_id(code["key"], state))
            row["social_historical_states"] = states
            row["annotations"] = [{"private": "PRIVATE_ANNOTATION"}]
            row["body"] = "PRIVATE_BODY"


@pytest.fixture
def source_app():
    service = SourceStateService()
    app = create_app(service, SimpleNamespace(show_source_links=True, cookie_secret="source-test", monthly_budget_usd=0))
    return app, app.server.test_client(), service


def point_request(selection, result, key="green_binary", state="source_true"):
    traces = result["social-labels-chart"]["figure"]["data"]
    curve = ("source_true", "source_false", "unknown").index(state)
    index = traces[curve]["y"].index(key)
    return selection | {
        "social-network-data.data": deepcopy(result["social-network-data"]["data"]),
        "social-labels-chart.clickData": {"points": [{
            "curveNumber": curve, "pointNumber": index, "y": key,
            "customdata": deepcopy(traces[curve]["customdata"][index]),
        }]},
        "active-dataset.value": "social", "social-view.value": "overview",
    }


def select(application, request, status=200):
    app, client, _service = application
    return callback(app, client, "social-label-click-status.children", request, "social-labels-chart.clickData", expected_status=status)


def export(application, selection, result=None):
    app, client, _service = application
    request = selection | {"social-export.n_clicks": 1}
    if result is not None:
        request["social-network-data.data"] = result["social-network-data"]["data"]
    return callback(app, client, "social-download.data", request, "social-export.n_clicks")


def counts(result, key):
    traces = result["social-labels-chart"]["figure"]["data"]
    return [trace["x"][trace["y"].index(key)] for trace in traces]


def test_fixed_options_and_source_meaning_are_visible_without_native_changes(source_app):
    app, client, _service = source_app
    layout = client.get("/_dash-layout").json
    components = {node["props"]["id"]: node["props"] for node in component_tree(layout) if "id" in node["props"]}
    options = components["social-labels"]["options"]
    assert len(options) == 39 and len({option["value"] for option in options}) == 39
    assert {option["value"] for option in options if option["value"].split(":")[1] == "green_binary"} == {
        GREEN_TRUE, GREEN_FALSE, GREEN_UNKNOWN,
    }
    assert next(option["label"] for option in options if option["value"] == GREEN_FALSE) == "Green messaging · Source export recorded False"
    assert components["native-labels"]["options"] == [{"label": "historical_solution", "value": "historical_solution"}]
    text = json.dumps(layout)
    assert "Historical source state (OR)" in text and "Unknown annotation does not mean Source False" in text
    assert "not CLAIMS2 themes, greenwashing judgments or fact checks" in text
    assert "social-label-click-status" in components
    native = callback(app, client, "native-grid.rowData", values("native"), "native-labels.value")
    assert [row["record_id"] for row in native["native-grid"]["rowData"]] == ["native-a", "native-b"]
    assert "social_historical_states" not in native["native-grid"]["rowData"][0]


@pytest.mark.parametrize("metric", ["count", "percent"])
def test_thirteen_codes_share_full_denominator_and_distinguish_false_from_unknown(source_app, metric):
    result = refresh(source_app, values(**{"social-metric.value": metric}))
    traces = result["social-labels-chart"]["figure"]["data"]
    assert [trace["name"] for trace in traces] == ["Source True", "Source False", "Unknown annotation"]
    assert all(len(trace["y"]) == 13 for trace in traces)
    denominator = 1 if metric == "count" else 30 / 100
    assert counts(result, "green_binary") == pytest.approx([24 / denominator, 3 / denominator, 3 / denominator])
    assert counts(result, "decreasing_emissions") == pytest.approx([12 / denominator, 15 / denominator, 3 / denominator])
    assert counts(result, "fossil_fuel_binary") == pytest.approx([0, 27 / denominator, 3 / denominator])
    for index in range(13):
        assert sum(trace["x"][index] for trace in traces) == pytest.approx(30 if metric == "count" else 100)
    note = result["social-labels-chart-note"]["children"]
    assert "30 selected posts" in note and "27 with a usable historical annotation" in note and "3 unknown" in note
    assert "do not sum" in note and "not reviewed themes, greenwashing or fact checks" in note
    snapshot = result["social-network-data"]["data"]["social_historical_labels"]
    assert snapshot["valid_annotation_records"] == 27 and snapshot["unknown_annotation_records"] == 3


def test_point_selection_preserves_scope_then_pages_and_exports_all_members(source_app):
    _app, _client, service = source_app
    selection = values(**{
        "social-accounts.value": ["Shared name"], "social-platforms.value": ["Twitter"],
        "social-sponsors.value": ["Company A"], "social-dates.start_date": "2024-01-01",
        "social-dates.end_date": "2024-12-31", "social-unknown-dates.value": [],
    })
    initial = refresh(source_app, selection)
    clicked = select(source_app, point_request(selection, initial))
    assert clicked["social-labels"]["value"] == [GREEN_TRUE] and clicked["social-view"]["value"] == "records"
    narrowed = selection | {"social-labels.value": clicked["social-labels"]["value"]}
    first = refresh(source_app, narrowed, "social-labels.value")
    snapshot = first["social-network-data"]["data"]
    second = refresh(source_app, narrowed | {"social-page-next.n_clicks": 1, "social-network-data.data": snapshot}, "social-page-next.n_clicks")
    ids = {row["record_id"] for page in (first, second) for row in page["social-grid"]["rowData"]}
    assert ids == {f"social-{index}" for index in range(24)}
    assert second["social-page-offset"]["data"] == 20
    csv_result = export(source_app, narrowed, first)
    rows = list(csv.DictReader(StringIO(csv_result["social-download"]["data"]["content"])))
    assert {row["record_id"] for row in rows} == ids and len(rows) == 24
    assert all(GREEN_TRUE in row["social_historical_states"].split("; ") for row in rows)
    assert all(len(row["social_historical_states"].split("; ")) == 13 for row in rows)
    assert {row["social_historical_scheme"] for row in rows} == {SCHEME}
    assert {row["social_historical_status"] for row in rows} == {STATUS}
    scoped = service.browse_calls[-1]
    assert scoped.accounts == ["Shared name"] and scoped.platforms == ["Twitter"] and scoped.sponsors == ["Company A"]
    assert str(scoped.date_from) == "2024-01-01" and str(scoped.date_to) == "2024-12-31"
    assert not scoped.include_unknown_dates and scoped.labels == [GREEN_TRUE]
    assert "PRIVATE_" not in json.dumps(csv_result) and "channel_id" not in json.dumps(csv_result)


def test_existing_or_states_can_only_narrow_to_an_already_selected_state(source_app):
    selection = values(**{"social-labels.value": [GREEN_TRUE, GREEN_FALSE]})
    initial = refresh(source_app, selection)
    assert initial["social-record-count"]["children"].startswith("27 unique posts")
    assert select(source_app, point_request(selection, initial))["social-labels"]["value"] == [GREEN_TRUE]
    outside = select(source_app, point_request(selection, initial, "fossil_fuel_binary", "source_false"))
    assert set(outside) == {"social-label-click-status"}
    assert "Change or clear" in outside["social-label-click-status"]["children"]
    false = refresh(source_app, values(**{"social-labels.value": [GREEN_FALSE]}), "social-labels.value")
    assert {row["record_id"] for row in false["social-grid"]["rowData"]} == {"social-24", "social-25", "social-29"}
    unknown = refresh(source_app, values(**{"social-labels.value": [GREEN_UNKNOWN]}), "social-labels.value")
    assert {row["record_id"] for row in unknown["social-grid"]["rowData"]} == {"social-26", "social-27", "social-28"}


@pytest.mark.parametrize("mutation", ["scope", "source-version", "admission", "path", "dataset", "view", "index", "boolean", "curve", "y", "customdata", "multiple", "zero"])
def test_stale_or_forged_points_cannot_change_scope(source_app, mutation):
    _app, _client, service = source_app
    selection = values()
    result = refresh(source_app, selection)
    request = point_request(selection, result)
    point = request["social-labels-chart.clickData"]["points"][0]
    if mutation == "scope":
        request["social-accounts.value"] = ["Other account"]
    elif mutation == "source-version":
        service.social[0]["version_id"] = "changed-current-version"
    elif mutation == "admission":
        service.admitted = False
    elif mutation in {"path", "dataset", "view"}:
        request[{"path": "page-location.pathname", "dataset": "active-dataset.value", "view": "social-view.value"}[mutation]] = {
            "path": "/query", "dataset": "native", "view": "records",
        }[mutation]
    elif mutation == "index":
        point["pointNumber"] = -1
    elif mutation == "boolean":
        point["pointNumber"] = False
    elif mutation == "curve":
        point["curveNumber"] = 3
    elif mutation == "y":
        point["y"] = "invented_source_code"
    elif mutation == "customdata":
        point["customdata"][0] = GREEN_FALSE
    elif mutation == "multiple":
        request["social-labels-chart.clickData"]["points"].append(dict(point))
    else:
        request = point_request(selection, result, "fossil_fuel_binary", "source_true")
    assert select(source_app, request, 204) == {}


@pytest.mark.parametrize("mutation", ["missing", "coverage", "count", "scheme", "partial"])
def test_missing_or_invalid_distribution_does_not_invent_zero_counts(source_app, monkeypatch, mutation):
    _app, _client, service = source_app
    original = service.dashboard

    def dashboard(*args, **kwargs):
        result = original(*args, **kwargs)
        distribution = result["social_historical_labels"]
        if mutation == "missing":
            result.pop("social_historical_labels")
        elif mutation == "coverage":
            distribution["valid_annotation_records"] = 0
        elif mutation == "count":
            distribution["items"][0]["source_true"] = 0
        elif mutation == "scheme":
            distribution["scheme"] = "native_historical"
        else:
            distribution["items"].pop()
        return result

    monkeypatch.setattr(service, "dashboard", dashboard)
    result = refresh(source_app, values())
    assert result["social-labels-chart"]["figure"]["data"] == []
    assert "unavailable" in result["social-labels-chart-note"]["children"]
    assert "missing coverage is not zero" in result["social-labels-chart-note"]["children"]
    assert result["social-network-data"]["data"]["social_historical_labels"] is None
    assert result["social-record-count"]["children"].startswith("30 unique posts")
    assert export(source_app, values(), result)["social-download"]["data"] is None


@pytest.mark.parametrize("mutation", ["missing", "scope", "forged-snapshot", "source-before", "source-during", "admission-before", "admission-during", "row-state", "row-count"])
def test_csv_rejects_missing_or_changed_snapshot_and_membership(source_app, monkeypatch, mutation):
    _app, _client, service = source_app
    selection = values()
    result = refresh(source_app, selection)
    if mutation == "scope":
        selection["social-platforms.value"] = ["Twitter"]
    elif mutation == "forged-snapshot":
        result["social-network-data"]["data"]["social_historical_labels"]["source_state_version"] = "a" * 64
    elif mutation == "source-before":
        service.social[0]["version_id"] = "changed-before-export"
    elif mutation == "admission-before":
        service.admitted = False
    elif mutation in {"source-during", "admission-during", "row-state", "row-count"}:
        original = service.browse
        original_dashboard = service.dashboard
        in_dashboard = False

        def dashboard(*args, **kwargs):
            nonlocal in_dashboard
            in_dashboard = True
            try:
                return original_dashboard(*args, **kwargs)
            finally:
                in_dashboard = False

        def browse(filters):
            rows = original(filters)
            # Simulate a source change at the separate full-record read, independently of read counts.
            if not in_dashboard:
                if mutation == "source-during":
                    service.social[0]["version_id"] = "changed-during-export"
                elif mutation == "admission-during":
                    service.admitted = False
                elif mutation == "row-state":
                    rows = deepcopy(rows)
                    rows[0]["social_historical_states"] = [GREEN_TRUE] * 13
                else:
                    rows = rows[:-1]
            return rows

        monkeypatch.setattr(service, "browse", browse)
        monkeypatch.setattr(service, "dashboard", dashboard)
    response = export(source_app, selection, None if mutation == "missing" else result)
    assert response["social-download"]["data"] is None
    assert "Refresh the current source-state selection" in response["social-export-status"]["children"]


def test_click_rechecks_admission_after_reading_current_distribution(source_app, monkeypatch):
    _app, _client, service = source_app
    selection = values()
    initial = refresh(source_app, selection)
    original = service.dashboard

    def dashboard(*args, **kwargs):
        result = original(*args, **kwargs)
        service.admitted = False
        return result

    monkeypatch.setattr(service, "dashboard", dashboard)
    assert select(source_app, point_request(selection, initial), 204) == {}


def test_source_change_resets_live_data_pagination_to_current_first_page(source_app):
    _app, _client, service = source_app
    first = refresh(source_app, values())
    service.social[0]["version_id"] = "new-current-source"
    current = refresh(source_app, values(**{
        "social-page-next.n_clicks": 1, "social-network-data.data": first["social-network-data"]["data"],
    }), "social-page-next.n_clicks")
    assert current["social-page-offset"]["data"] == 0
    assert [row["record_id"] for row in current["social-grid"]["rowData"]] == [f"social-{index}" for index in range(20)]
    assert "Selection refreshed" in json.dumps(current["social-status"])
    assert current["social-network-data"]["data"]["social_historical_labels"]["source_state_version"] != first["social-network-data"]["data"]["social_historical_labels"]["source_state_version"]


def test_unadmitted_active_rows_and_empty_selection_do_not_show_false_source_zeroes(source_app):
    _app, _client, service = source_app
    service.admitted = False
    result = refresh(source_app, values())
    assert service.health()["record_counts"]["social"] == 30
    assert result["social-content"]["style"] == {"display": "none"}
    assert result["social-labels-chart"]["figure"]["data"] == []
    assert result["social-network-data"]["data"]["social_historical_labels"] is None
    assert "No admitted social records" in result["social-labels-chart-note"]["children"]
    assert export(source_app, values(), result)["social-download"]["data"] is None
    service.admitted = True
    result = refresh(source_app, values(**{"social-accounts.value": ["absent account"]}))
    assert result["social-labels-chart"]["figure"]["data"] == []
    assert "No matching records" in json.dumps(result["social-status"])
    assert "0 selected posts" in result["social-labels-chart-note"]["children"]


def test_social_csv_formula_guard_and_native_csv_contract_remain(source_app):
    app, client, service = source_app
    service.social[0]["title"] = "=UNSAFE_FORMULA()"
    result = refresh(source_app, values())
    rows = list(csv.DictReader(StringIO(export(source_app, values(), result)["social-download"]["data"]["content"])))
    assert rows[0]["title"] == "'=UNSAFE_FORMULA()"
    native = callback(app, client, "native-download.data", values("native", **{"native-export.n_clicks": 1}), "native-export.n_clicks")
    native_rows = csv.DictReader(StringIO(native["native-download"]["data"]["content"]))
    assert native_rows.fieldnames == list(NATIVE_COLUMNS) + ["record_id", "version_id", "labels", "retrievable", "archive_url"]
    assert {row["record_id"] for row in native_rows} == {"native-a", "native-b"}


def test_current_query_inherits_source_states_and_stales_after_label_change(source_app):
    app, client, service = source_app
    selection = values("native") | values() | {
        "social-labels.value": [GREEN_FALSE], "active-dataset.value": "social", "search-scope.value": "current",
        "research-question.value": "capture", "search-free.n_clicks": 1, "page-location.pathname": "/query",
    }
    result = callback(app, client, "research-results.children", selection, "search-free.n_clicks")
    assert service.search_calls[-1][1].labels == [GREEN_FALSE]
    links = {node["props"].get("href") for node in component_tree(result["research-results"]["children"])
             if node.get("type") == "A" and node["props"].get("href", "").startswith("/records/")}
    assert links == {f"/records/social-{index}" for index in (24, 25, 29)}
    assert "Historical source states (OR)" in json.dumps(result["research-results"])
    assert "Green messaging" in json.dumps(result["research-results"])
    stale = callback(app, client, "research-stale.children", selection | {
        "research-submission.data": result["research-submission"]["data"], "social-labels.value": [GREEN_TRUE],
    }, "social-labels.value")
    assert "earlier selection" in json.dumps(stale)
    assert not service.answer_calls


class DistributionAnswerService(SourceStateService):
    """Use the actual distribution answer method with synthetic current rows."""

    def __init__(self):
        super().__init__()
        self.version = "distribution-data-v1"
        self.source_reads = []
        self.page_calls = []
        self.page_after_hook = None
        self.db = SimpleNamespace(social_source_state_version=self.current_source_version)

    def health(self):
        return super().health() | {"data_version": self.version}

    def current_source_version(self, filters):
        self.source_reads.append(filters.model_copy(deep=True))
        return self.dashboard(filters, offset=0, limit=1)["social_historical_labels"]["source_state_version"]

    def answer(self, question, filters, visitor):
        self.answer_calls.append((question, filters.model_copy(deep=True), visitor))
        dashboard = self.dashboard(filters, offset=0, limit=1)
        distribution = dashboard["social_historical_labels"]
        data = {
            "status": "ok", "kind": "social_historical_labels", "method": "database", "group_by": "social_historical_labels",
            "filters": filters.model_dump(mode="json"), "distribution": distribution,
            "source_state_version": distribution["source_state_version"],
            "collections": [{"dataset": "social", "total": distribution["total"]}],
            "groups": [], "records": dashboard["page"]["rows"], "scope_notes": [],
            "data_version": self.version,
            "historical_source_state_guard": {
                "filters": filters.model_dump(mode="json"), "source_state_version": distribution["source_state_version"],
            },
        }
        return Service._social_label_statistics_answer(data, base_filters=filters)

    def page(self, filters, offset=0, limit=20, **kwargs):
        self.page_calls.append((filters.model_copy(deep=True), offset, limit))
        result = super().page(filters, offset, limit, **kwargs)
        if self.page_after_hook:
            self.page_after_hook()
        return result


@pytest.fixture
def distribution_app():
    service = DistributionAnswerService()
    app = create_app(service, SimpleNamespace(show_source_links=True, cookie_secret="distribution-answer-test", monthly_budget_usd=0))
    return app, app.server.test_client(), service


def distribution_request(application, selection=None):
    app, client, _service = application
    selection = values("native") | values() | (selection or {}) | {
        "active-dataset.value": "social", "search-scope.value": "current",
        "research-question.value": "Show historical source states by code", "answer-paid.n_clicks": 1,
        "page-location.pathname": "/query",
    }
    result = callback(app, client, "research-results.children", selection, "answer-paid.n_clicks")
    rendered = result["research-results"]["children"]
    components = {node["props"]["id"]: node["props"] for node in component_tree(rendered) if "id" in node["props"]}
    return rendered, components


def matching_page(application, components, offset=0, next_page=False):
    app, client, _service = application
    request = {
        "query-records-scope.data": components["query-records-scope"]["data"],
        "query-records-collection.value": "social", "query-records-offset.data": offset,
        "query-records-next.n_clicks": 1, "query-records-prev.n_clicks": 0, "query-records-reset.n_clicks": 0,
        "page-location.pathname": "/query",
    }
    return callback(app, client, "query-records-content.children", request,
                    "query-records-next.n_clicks" if next_page else "query-records-scope.data")


def test_generated_distribution_answer_renders_all_thirteen_codes_with_actual_counts(distribution_app):
    rendered, components = distribution_request(distribution_app)
    tables = [node for node in component_tree(rendered) if node.get("type") == "Table"]
    assert len(tables) == 1
    body = next(node for node in component_tree(tables[0]) if node.get("type") == "Tbody")
    rows = body["props"]["children"]
    assert len(rows) == 13
    cells = {row["props"]["children"][0]["props"]["children"]["props"]["children"]:
             [cell["props"]["children"] for cell in row["props"]["children"][1:]] for row in rows}
    assert cells["green_binary"] == ["Green messaging", "macro", "24", "3", "3"]
    assert cells["decreasing_emissions"] == ["Decreasing emissions", "subcode", "12", "15", "3"]
    assert cells["fossil_fuel_binary"] == ["Fossil fuel messaging", "macro", "0", "27", "3"]
    assert set(cells) == {item["key"] for item in social_label_metadata()}
    text = json.dumps(rendered)
    assert "30 selected posts" in text and "27 with a usable historical annotation" in text and "3 unknown" in text
    assert "It does not mean Source False" in text and "not reviewed themes, greenwashing or fact checks" in text
    visible_text = " ".join(node["props"]["children"] for node in component_tree(rendered)
                            if isinstance(node["props"].get("children"), str))
    assert "searchable" not in visible_text and "with unknown dates" not in visible_text
    assert "Answer service unavailable" not in text and "malformed" not in text
    summary = next(node["props"]["children"] for node in component_tree(rendered)
                   if node["props"].get("className") == "answer-text")
    assert summary == "30 selected posts · 27 with a usable historical annotation · 3 unknown."
    assert "Green messaging: True" not in text  # the CLI's thirteen-line prose is represented by the table.
    assert components["query-records-scope"]["data"]
    assert "PRIVATE_" not in text and "different-channel" not in text
    assert len(distribution_app[2].answer_calls) == 1 and not distribution_app[2].search_calls


def test_distribution_matching_records_preserve_compound_signed_scope_and_all_members(distribution_app):
    _rendered, components = distribution_request(distribution_app, {
        "social-accounts.value": ["Shared name"], "social-platforms.value": ["Twitter"],
        "social-sponsors.value": ["Company A"], "social-labels.value": [GREEN_TRUE],
        "social-dates.start_date": "2024-01-01", "social-dates.end_date": "2024-12-31",
        "social-unknown-dates.value": [],
    })
    service = distribution_app[2]
    from observatory.query_records import _serializer

    with distribution_app[0].server.test_request_context():
        saved = _serializer().loads(components["query-records-scope"]["data"])
    expected = Filters(dataset="social", accounts=["Shared name"], platforms=["Twitter"], sponsors=["Company A"],
                       labels=[GREEN_TRUE], date_from="2024-01-01", date_to="2024-12-31", include_unknown_dates=False)
    assert saved["filters"] == expected.model_dump(mode="json")
    assert saved["historical_source_state_guard"] == {
        "filters": expected.model_dump(mode="json"), "source_state_version": service.current_source_version(expected),
    }
    found = set()
    for offset, next_page in ((0, False), (0, True), (10, True)):
        page = matching_page(distribution_app, components, offset, next_page)
        found.update(node["props"]["href"].removeprefix("/records/") for node in component_tree(page)
                     if node.get("type") == "A" and node["props"].get("href", "").startswith("/records/"))
    assert found == {f"social-{index}" for index in range(24)}
    assert all(filters == expected for filters, _offset, _limit in service.page_calls)
    assert all(filters == expected for filters in service.source_reads)
    assert len(service.answer_calls) == 1 and not service.search_calls


@pytest.mark.parametrize("during_page", [False, True])
def test_distribution_record_guard_rejects_annotation_change_with_same_data_version(distribution_app, during_page):
    _rendered, components = distribution_request(distribution_app)
    service = distribution_app[2]

    def change_annotation():
        service.social[0]["social_historical_states"][0] = GREEN_FALSE

    if during_page:
        service.page_after_hook = change_annotation
    else:
        change_annotation()
    page = matching_page(distribution_app, components)
    assert "Historical source states changed" in json.dumps(page)
    assert not [node for node in component_tree(page) if node.get("type") == "A"]
    assert service.version == "distribution-data-v1" and len(service.answer_calls) == 1
