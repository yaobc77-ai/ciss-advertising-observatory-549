"""Social account exploration through real Dash HTTP callbacks, with local fixtures."""

import csv
import hashlib
import json
from io import StringIO
from types import SimpleNamespace

import pytest
from test_app import FakeService, callback, component_tree

from observatory.app import FILTER_NAMES, create_app
from observatory.models import Evidence
from observatory.social_annotations import (
    SCHEME,
    STATUS,
    social_label_metadata,
    social_state_id,
    social_state_options,
)

UNKNOWN = "(Unknown)"


class SocialService(FakeService):
    def __init__(self, admitted=True):
        super().__init__()
        self.admitted = admitted
        self.social = []
        for account, platform, company, date, count in [
            ("Shared name", "Twitter", "Company A", "2024-01-10", 24),
            ("Shared name", "LinkedIn", "Company B", "2023-01-10", 2),
            ("Other account", "Twitter", "Company A", "2024-06-01", 1),
            ("Other account", "Twitter", "Company A", None, 1),
            ("", "Twitter", "", None, 1),
            ("Third account", "", "Company C", "2024-07-01", 1),
        ]:
            for _ in range(count):
                number = len(self.social)
                self.social.append({
                    "record_id": f"social-{number}", "version_id": f"sv-{number}",
                    "dataset": "social", "account": account, "platform": platform,
                    "sponsor": company, "date": date, "publisher": "", "keyword": "capture",
                    "title": f"Capture post {number}", "url": f"https://example.test/{number}",
                    "archive_url": "", "labels": [], "retrievable": True,
                    "raw": {"channel_id": f"different-channel-{number}"},
                    "social_historical_states": [social_state_id(item["key"], "unknown") for item in social_label_metadata()],
                    "social_historical_scheme": SCHEME, "social_historical_status": STATUS,
                })

    def health(self):
        return {"status": "ok", "record_counts": {"native": 2, "social": len(self.social)},
                "countable_record_counts": {"native": 2, "social": len(self.social) if self.admitted else 0}}

    def facets(self, dataset):
        if dataset != "social":
            return super().facets(dataset) | {"accounts": []}
        facets = {
            name: sorted({row.get(name[:-1] if name != "keywords" else "keyword") or UNKNOWN
                          for row in self.social}) if name not in {"labels", "publishers"} else []
            for name in FILTER_NAMES
        }
        facets["labels"] = [option["value"] for option in social_state_options()]
        return facets

    def dashboard(self, filters, offset=0, limit=20, sort_by="date", descending=True):
        dashboard = super().dashboard(filters, offset, limit, sort_by, descending)
        if filters.dataset == "social":
            rows = self._selected(filters)
            version = hashlib.sha256(json.dumps([
                (row["record_id"], row["version_id"], row["social_historical_states"]) for row in rows
            ], sort_keys=True).encode()).hexdigest()
            unknown = sum(any(value.endswith(":unknown") for value in row["social_historical_states"]) for row in rows)
            dashboard["social_historical_labels"] = {
                "scheme": SCHEME, "status": STATUS, "total": len(rows),
                "valid_annotation_records": len(rows) - unknown, "unknown_annotation_records": unknown,
                "source_state_version": version,
                "items": [{**item, **{
                    state: sum(social_state_id(item["key"], state) in row["social_historical_states"] for row in rows)
                    for state in ("source_true", "source_false", "unknown")
                }} for item in social_label_metadata()],
            }
        return dashboard

    def _selected(self, filters):
        if filters.dataset == "native":
            return super()._selected(filters)
        candidates = self.social if self.admitted else []
        if filters.dataset == "all":
            candidates = self.rows + candidates
        selected = []
        for row in candidates:
            if any(getattr(filters, field) and (row.get(column) or UNKNOWN) not in getattr(filters, field)
                   for field, column in (("accounts", "account"), ("platforms", "platform"),
                                         ("sponsors", "sponsor"), ("keywords", "keyword"))):
                continue
            if filters.labels and not set(filters.labels).intersection(row.get("social_historical_states", [])):
                continue
            date = row.get("date")
            if not date and not filters.include_unknown_dates:
                continue
            if date and ((filters.date_from and date < filters.date_from.isoformat())
                         or (filters.date_to and date > filters.date_to.isoformat())):
                continue
            selected.append(row)
        return selected

    def search(self, question, filters, limit=5):
        self.search_calls.append((question, filters, limit))
        return [Evidence(evidence_id=f"E{index}", record_id=row["record_id"],
                         version_id=row["version_id"], dataset=row["dataset"], title=row["title"],
                         sponsor=row["sponsor"], text="Capture source text", start=0, end=19)
                for index, row in enumerate(self._selected(filters)[:limit], 1)]


@pytest.fixture
def social_app():
    service = SocialService()
    app = create_app(service, SimpleNamespace(show_source_links=True, cookie_secret="ui-test", monthly_budget_usd=0))
    return app, app.server.test_client(), service


def values(dataset="social", **overrides):
    result = {f"{dataset}-{field}.value": [] for field in FILTER_NAMES}
    result.update({f"{dataset}-dates.start_date": None, f"{dataset}-dates.end_date": None,
                   f"{dataset}-unknown-dates.value": ["include"], f"{dataset}-metric.value": "count",
                   f"{dataset}-page-size.value": 20, f"{dataset}-sort.value": "date:desc",
                   f"{dataset}-page-offset.data": 0, "page-location.pathname": "/data",
                   "active-dataset.value": dataset})
    return result | overrides


def refresh(application, selection, changed="social-accounts.value"):
    app, client, _service = application
    return callback(app, client, "social-grid.rowData", selection, changed)


def bars(result, name):
    traces = result[name]["figure"]["data"]
    return dict(zip(traces[0]["y"], traces[0]["x"], strict=True)) if traces else {}


def click_values(selection, result, account):
    snapshot = result["social-network-data"]["data"]
    return selection | {
        "social-network-data.data": snapshot,
        "social-primary-chart.clickData": {"points": [{"curveNumber": 0,
                                                      "pointNumber": snapshot["accounts"].index(account), "y": account}]},
    }


def test_layout_names_sources_and_keeps_native_account_control_hidden(social_app):
    _app, client, _service = social_app
    layout = client.get("/_dash-layout").json
    components = {node["props"]["id"]: node["props"] for node in component_tree(layout) if "id" in node["props"]}
    for dataset, hidden in (("native", True), ("social", False)):
        field = next(node["props"] for node in component_tree(layout)
                     if node.get("type") == "Div" and node["props"].get("className") == "filter-field"
                     and any(child["props"].get("id") == f"{dataset}-accounts"
                             for child in component_tree(node["props"].get("children"))))
        assert field["style"] == ({"display": "none"} if hidden else {})
        assert components[f"{dataset}-accounts"]["multi"] is True
    assert UNKNOWN in [option["value"] for option in components["social-accounts"]["options"]]
    columns = {column["field"]: column for column in components["social-grid"]["columnDefs"]}
    assert columns["sponsor"]["headerName"] == "Company affiliation"
    assert "paid sponsorship is not verified" in columns["sponsor"]["headerTooltip"]
    text = json.dumps(layout)
    assert "channel.name" in text and "unique channel IDs" in text
    assert "Historical source state (OR)" in text and "Unknown annotation does not mean Source False" in text


@pytest.mark.parametrize("metric", ["count", "percent"])
def test_account_and_platform_charts_share_full_selection(social_app, metric):
    result = refresh(social_app, values(**{"social-metric.value": metric}))
    denominator = 1 if metric == "count" else 30 / 100
    assert bars(result, "social-primary-chart") == pytest.approx(
        {"Shared name": 26 / denominator, "Other account": 2 / denominator,
         "Third account": 1 / denominator, UNKNOWN: 1 / denominator})
    assert bars(result, "social-platforms-chart") == pytest.approx(
        {"Twitter": 27 / denominator, "LinkedIn": 2 / denominator, UNKNOWN: 1 / denominator})
    assert len(result["social-grid"]["rowData"]) == 20
    assert result["social-record-count"]["children"].startswith("30 unique posts")
    assert "have no historical label" not in result["social-labels-chart-note"]["children"]


@pytest.mark.parametrize("accounts,platforms,companies,start,end,unknown,expected", [
    (["Shared name"], ["Twitter"], ["Company A"], "2024-01-01", "2024-12-31", [], 24),
    (["Shared name"], ["LinkedIn"], [], None, None, ["include"], 2),
    (["Shared name", "Other account"], ["Twitter"], [], None, None, [], 25),
    ([UNKNOWN], ["Twitter"], [UNKNOWN], None, None, ["include"], 1),
    (["Third account"], [UNKNOWN], [], None, None, ["include"], 1),
    (["Shared name"], ["Twitter"], ["Company B"], None, None, ["include"], 0),
])
def test_combined_scope_pagination_and_export(social_app, accounts, platforms, companies, start, end, unknown, expected):
    app, client, service = social_app
    selection = values(**{"social-accounts.value": accounts, "social-platforms.value": platforms,
                          "social-sponsors.value": companies, "social-dates.start_date": start,
                          "social-dates.end_date": end, "social-unknown-dates.value": unknown})
    result = refresh(social_app, selection)
    assert result["social-record-count"]["children"].startswith(f"{expected} unique posts")
    expected_ids = {row["record_id"] for row in service._selected(service.browse_calls[-1])}
    first_ids = {row["record_id"] for row in result["social-grid"]["rowData"]}
    assert first_ids <= expected_ids
    snapshot = result["social-network-data"]["data"]
    second = refresh(social_app, selection | {"social-page-next.n_clicks": 1, "social-network-data.data": snapshot}, "social-page-next.n_clicks")
    second_ids = {row["record_id"] for row in second["social-grid"]["rowData"]}
    assert first_ids | second_ids == expected_ids
    exported = callback(app, client, "social-download.data", selection | {"social-export.n_clicks": 1, "social-network-data.data": snapshot}, "social-export.n_clicks")
    rows = list(csv.DictReader(StringIO(exported["social-download"]["data"]["content"])))
    assert {row["record_id"] for row in rows} == expected_ids
    assert service.browse_calls[-1].accounts == accounts


@pytest.mark.parametrize("account", ["Shared name", UNKNOWN])
def test_bar_click_narrows_accounts_and_switches_to_members(social_app, account):
    app, client, service = social_app
    selection = values(**{"social-accounts.value": ["Shared name", UNKNOWN],
                          "social-platforms.value": ["Twitter"], "social-sponsors.value": ["Company A", UNKNOWN],
                          "social-dates.start_date": "2024-01-01", "social-dates.end_date": "2024-12-31"})
    result = refresh(social_app, selection)
    selected = callback(app, client, "social-view.value", click_values(selection, result, account), "social-primary-chart.clickData")
    assert selected == {"social-accounts": {"value": [account]}, "social-view": {"value": "records"}}
    rows = refresh(social_app, selection | {"social-accounts.value": selected["social-accounts"]["value"]})["social-grid"]["rowData"]
    assert rows and {row["account"] for row in rows} == {account}
    assert service.browse_calls[-1].platforms == ["Twitter"]
    assert service.browse_calls[-1].sponsors == ["Company A", UNKNOWN]
    assert service.browse_calls[-1].date_from.isoformat() == "2024-01-01"
    tab = callback(app, client, "social-overview.hidden", {"social-view.value": "records"}, "social-view.value")
    assert tab["social-records"]["hidden"] is False


@pytest.mark.parametrize("mutation", ["scope", "name", "index", "curve", "boolean-curve", "multiple", "empty"])
def test_stale_or_invalid_account_click_does_not_change_scope(social_app, mutation):
    app, client, service = social_app
    selection = values()
    result = refresh(social_app, selection)
    request = click_values(selection, result, "Shared name")
    point = request["social-primary-chart.clickData"]["points"][0]
    if mutation == "scope":
        request["social-platforms.value"] = ["LinkedIn"]
    elif mutation == "name":
        point["y"] = "Fabricated account"
    elif mutation == "index":
        point["pointNumber"] = -1
    elif mutation == "curve":
        point["curveNumber"] = 1
    elif mutation == "boolean-curve":
        point["curveNumber"] = False
    elif mutation == "multiple":
        request["social-primary-chart.clickData"]["points"].append(dict(point))
    else:
        request["social-primary-chart.clickData"] = None
    before = len(service.browse_calls)
    callback(app, client, "social-view.value", request, "social-primary-chart.clickData", expected_status=204)
    assert len(service.browse_calls) == before


def test_account_filter_resets_pagination_and_clear_resets_every_control(social_app):
    app, client, service = social_app
    result = refresh(social_app, values(**{"social-page-offset.data": 20, "social-accounts.value": ["Other account"]}))
    assert result["social-page-offset"]["data"] == 0
    cleared = callback(app, client, "social-dates.start_date", {"social-clear-filters.n_clicks": 1}, "social-clear-filters.n_clicks")
    for name in FILTER_NAMES:
        assert cleared[f"social-{name}"]["value"] == []
    assert cleared["social-dates"] == {"start_date": None, "end_date": None}
    assert cleared["social-unknown-dates"]["value"] == ["include"]
    assert service.browse_calls[-1].accounts == ["Other account"]


def test_invalid_social_dates_preserve_scope_without_querying(social_app):
    _app, _client, service = social_app
    result = refresh(social_app, values(**{"social-accounts.value": ["Shared name"],
                                          "social-dates.start_date": "2025-01-01",
                                          "social-dates.end_date": "2024-01-01"}))
    assert "Check the filters" in json.dumps(result["social-status"])
    assert result["social-content"]["style"] == {"display": "none"}
    assert not service.browse_calls


def test_current_query_uses_accounts_and_stales_when_account_changes(social_app):
    app, client, service = social_app
    selection = values("native") | values() | {"social-accounts.value": ["Other account"],
                                                "social-platforms.value": ["Twitter"],
                                                "social-sponsors.value": ["Company A"],
                                                "active-dataset.value": "social", "search-scope.value": "current",
                                                "research-question.value": "capture", "search-free.n_clicks": 1,
                                                "page-location.pathname": "/query"}
    result = callback(app, client, "research-results.children", selection, "search-free.n_clicks")
    scope = result["research-submission"]["data"]
    assert scope["filters"]["accounts"] == ["Other account"]
    assert service.search_calls[-1][1].accounts == ["Other account"]
    assert "social-26" in json.dumps(result) and "social-0" not in json.dumps(result)
    assert "Company affiliation" in json.dumps(result)
    stale = callback(app, client, "research-stale.children", selection | {
        "research-submission.data": scope, "social-accounts.value": ["Shared name"]}, "social-accounts.value")
    assert "earlier selection" in json.dumps(stale)
    inactive = callback(app, client, "research-stale.children", selection | {
        "research-submission.data": scope, "native-sponsors.value": ["Sponsor B"]}, "native-sponsors.value")
    assert inactive["research-stale"]["children"] is None
    assert not service.answer_calls


def test_all_query_explicitly_bypasses_account_filters(social_app):
    app, client, service = social_app
    selection = values("native") | values() | {"social-accounts.value": ["Shared name"],
                                                "active-dataset.value": "social", "search-scope.value": "all",
                                                "research-question.value": "capture", "search-free.n_clicks": 1,
                                                "page-location.pathname": "/query"}
    result = callback(app, client, "research-results.children", selection, "search-free.n_clicks")
    assert service.search_calls[-1][1].dataset == "all"
    assert service.search_calls[-1][1].accounts == []
    scope = callback(app, client, "current-scope.children", selection, "social-accounts.value")
    assert "filters are bypassed" in json.dumps(scope)
    stale = callback(app, client, "research-stale.children", selection | {
        "research-submission.data": result["research-submission"]["data"], "social-accounts.value": [UNKNOWN]}, "social-accounts.value")
    assert stale["research-stale"]["children"] is None


def test_stored_unadmitted_social_rows_show_only_availability_notice():
    service = SocialService(admitted=False)
    app = create_app(service, SimpleNamespace(show_source_links=True, cookie_secret="ui-test", monthly_budget_usd=0))
    client = app.server.test_client()
    assert "Social \\u00b7 no admitted records" in json.dumps(client.get("/_dash-layout").json)
    hidden = callback(app, client, "native-panel.style", {"active-dataset.value": "social"}, "active-dataset.value")
    assert hidden["shared-filters"]["style"] == {"display": "none"}
    assert hidden["social-filter-panel"]["style"] == {"display": "none"}
    result = refresh((app, client, service), values())
    assert result["social-grid"]["rowData"] == []
    assert result["social-content"]["style"] == {"display": "none"}
    assert "No admitted company social posts" in json.dumps(result["social-status"])
    assert bars(result, "social-primary-chart") == {}
