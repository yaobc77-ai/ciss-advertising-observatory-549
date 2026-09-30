"""Exercise the actual Dash layout and callback HTTP protocol without external services."""

import csv
import json
from io import StringIO
from types import SimpleNamespace

import pytest

from observatory.app import create_app
from observatory.models import Answer, Citation, Evidence

SECRET = "private-disclosure-postgresql-secret-internal-path"
ORIGINAL = "https://example.test/original"
ARCHIVE = "https://archive.example.test/capture"


class FakeService:
    def __init__(self):
        self.browse_calls = []
        self.search_calls = []
        self.answer_calls = []
        self.fail_browse = False
        self.fail_answer = False
        self.answer_status = "answered"
        self.rows = [
            {
                "record_id": "native-a",
                "version_id": "version-a",
                "dataset": "native",
                "publisher": "Outlet A",
                "sponsor": "Sponsor A",
                "title": "A capture proposal",
                "date": "2024-01-01",
                "keyword": "CCS",
                "url": ORIGINAL,
                "archive_url": ARCHIVE,
                "retrievable": True,
                "labels": ["historical_solution"],
                "raw": {"secret": SECRET},
                "disclosure": SECRET,
                "internal_path": SECRET,
                "api_key": SECRET,
            },
            {
                "record_id": "native-b",
                "version_id": "version-b",
                "dataset": "native",
                "publisher": "Outlet B",
                "sponsor": "Sponsor B",
                "title": "=FORMULA()",
                "date": None,
                "keyword": "gas",
                "url": "javascript:alert(1)",
                "archive_url": "file:///private/capture.pdf",
                "retrievable": False,
                "labels": [],
            },
        ]

    def health(self):
        return {
            "status": "ok",
            "record_counts": {"native": 3, "social": 0},
            "data_version": "test",
            "dsn": SECRET,
        }

    def facets(self, dataset):
        return {
            "publishers": ["Outlet A", "Outlet B", "(Unknown)"],
            "sponsors": ["Sponsor A", "Sponsor B"],
            "platforms": [],
            "keywords": ["CCS", "gas"],
            "labels": ["historical_solution"],
        }

    def _selected(self, filters):
        if filters.dataset == "social":
            return []
        return [
            r
            for r in self.rows
            if (not filters.publishers or r["publisher"] in filters.publishers)
            and (not filters.sponsors or r["sponsor"] in filters.sponsors)
            and (filters.include_unknown_dates or r["date"] is not None)
        ]

    def browse(self, filters):
        self.browse_calls.append(filters)
        if self.fail_browse:
            raise RuntimeError(SECRET)
        return self._selected(filters)

    def page(self, filters, offset=0, limit=20, sort_by="date", descending=True):
        rows = self.browse(filters)
        offset = min(offset, max(0, (len(rows) - 1) // limit * limit))
        return {"rows": rows[offset:offset + limit], "total": len(rows), "offset": offset}

    def dashboard(self, filters, offset=0, limit=20, sort_by="date", descending=True):
        from observatory.analytics import (
            historical_label_distribution,
            sponsor_publisher_matrix,
            yearly_timeline,
        )
        from observatory.service import summarize

        rows = self.browse(filters)
        offset = min(offset, max(0, (len(rows) - 1) // limit * limit))
        return {"page": {"rows": rows[offset:offset + limit], "total": len(rows), "offset": offset},
                "stats": summarize(rows), "matrix": sponsor_publisher_matrix(rows),
                "labels": historical_label_distribution(rows), "timeline": yearly_timeline(rows)}

    def statistics(self, filters):
        rows = self._selected(filters)

        def groups(field):
            names = {r[field] for r in rows}
            return [
                {
                    "name": n,
                    "count": sum(r[field] == n for r in rows),
                    "percent": 100 * sum(r[field] == n for r in rows) / len(rows),
                }
                for n in names
            ]

        return {
            "total": len(rows),
            "retrievable": sum(r["retrievable"] for r in rows),
            "unknown_dates": sum(r["date"] is None for r in rows),
            "publishers": groups("publisher"),
            "sponsors": groups("sponsor"),
            "platforms": [],
            "timeline": [{"month": "2024-01", "count": 1}] if rows else [],
            "relationships": [
                {"sponsor": r["sponsor"], "publisher": r["publisher"], "count": 1}
                for r in rows
            ],
        }

    def evidence(self):
        return Evidence(
            evidence_id="E1",
            record_id="native-a",
            version_id="version-a",
            dataset="native",
            title="A capture proposal",
            publisher="Outlet A",
            sponsor="Sponsor A",
            url=ORIGINAL,
            archive_url=ARCHIVE,
            text="The sponsor proposes a carbon capture project.",
            start=0,
            end=45,
        )

    def search(self, question, filters, limit=5):
        self.search_calls.append((question, filters, limit))
        return [self.evidence()] if filters.dataset != "social" else []

    def answer(self, question, filters, visitor):
        self.answer_calls.append((question, filters, visitor))
        if self.fail_answer:
            raise RuntimeError(SECRET)
        return Answer(
            status=self.answer_status,
            answer="The sponsor describes a proposed project."
            if self.answer_status == "answered"
            else SECRET,
            evidence=[self.evidence()],
            citations=[
                Citation(evidence_id="E1", quote="proposes a carbon capture project")
            ],
        )


@pytest.fixture
def application():
    service = FakeService()
    settings = SimpleNamespace(
        show_source_links=True,
        cookie_secret="test-cookie-secret",
        monthly_budget_usd=100,
    )
    app = create_app(service, settings)
    return app, app.server.test_client(), service


def component_tree(node):
    if isinstance(node, dict):
        if "props" in node:
            yield node
        for value in node.values():
            yield from component_tree(value)
    elif isinstance(node, list):
        for value in node:
            yield from component_tree(value)


def test_health_reports_release_and_features_without_serializing_configuration(application, monkeypatch):
    _app, client, _service = application
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "a" * 40)
    monkeypatch.setenv("OBS_DATABASE_URL", SECRET)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json["application"]["commit"] == "a" * 40
    assert response.json["application"]["features"] == {
        "collection_graph": True,
        "graph_breakdowns": True,
        "research_agent": False,
    }
    assert response.json["application"]["version"]
    assert SECRET not in response.get_data(as_text=True)


def test_health_rejects_non_commit_environment_values_and_preserves_failure_status(application, monkeypatch):
    _app, client, service = application
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", SECRET)
    monkeypatch.setattr(service, "health", lambda: {"status": "unavailable"})
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json["application"]["commit"] is None
    assert SECRET not in response.get_data(as_text=True)


def defaults(dataset):
    values = {
        f"{dataset}-{name}.value": []
        for name in ("publishers", "sponsors", "platforms", "keywords", "labels")
    }
    values.update(
        {
            f"{dataset}-dates.start_date": None,
            f"{dataset}-dates.end_date": None,
            f"{dataset}-unknown-dates.value": ["include"],
            f"{dataset}-metric.value": "count",
            "page-location.pathname": "/data",
        }
    )
    return values


def callback(app, client, output_id, values, changed, expected_status=200):
    key, spec = next(
        (key, spec) for key, spec in app.callback_map.items() if output_id in key
    )
    outs = spec["output"]
    outputs = (
        [{"id": out.component_id, "property": out.component_property} for out in outs]
        if isinstance(outs, list)
        else {"id": outs.component_id, "property": outs.component_property}
    )
    body = {
        "output": key,
        "outputs": outputs,
        "changedPropIds": [changed],
        "inputs": [
            {
                "id": x["id"],
                "property": x["property"],
                "value": values.get(x["id"] + "." + x["property"]),
            }
            for x in spec["inputs"]
        ],
        "state": [
            {
                "id": x["id"],
                "property": x["property"],
                "value": values.get(x["id"] + "." + x["property"]),
            }
            for x in spec["state"]
        ],
    }
    response = client.post("/_dash-update-component", json=body)
    assert response.status_code == expected_status, response.get_data(as_text=True)
    return response.json["response"] if expected_status == 200 else {}


def research_values(dataset="native", scope="current"):
    return (
        defaults("native")
        | defaults("social")
        | {
            "research-question.value": "carbon capture",
            "search-scope.value": scope,
            "active-dataset.value": dataset,
            "search-free.n_clicks": 1,
            "answer-paid.n_clicks": 0,
            "page-location.pathname": "/query",
        }
    )


def test_layout_has_both_panels_original_fields_and_record_archive_access(application):
    app, client, _service = application
    assert client.get("/").status_code == 200
    response = client.get("/_dash-layout")
    assert response.status_code == 200
    components = {
        x["props"]["id"]: x["props"]
        for x in component_tree(response.json)
        if "id" in x["props"]
    }
    assert {f"{dataset}-publishers" for dataset in ("native", "social")} <= set(
        components
    )
    assert [col["field"] for col in components["native-grid"]["columnDefs"]] == [
        "title",
        "sponsor",
        "publisher",
        "date",
        "url",
        "archive_status",
    ]
    assert components["native-grid"]["columnDefs"][0]["cellRenderer"] == "RecordTitle"
    assert components["research-question"]["maxLength"] == 2000
    assert SECRET not in response.get_data(as_text=True)
    assert client.get("/assets/observatory.css").status_code == 200
    assert client.get("/assets/source_links.js").status_code == 200
    changed = callback(
        app,
        client,
        "native-panel.style",
        {"active-dataset.value": "social"},
        "active-dataset.value",
    )
    assert changed["native-panel"]["style"] == {"display": "none"}
    assert changed["social-panel"]["style"] == {}
    assert changed["native-filter-panel"]["style"] == {"display": "none"}
    assert changed["social-filter-panel"]["style"] == {"display": "none"}
    assert changed["shared-filters"]["style"] == {"display": "none"}
    assert len(changed) == 6  # Switching tabs must never reset any filter values.


def test_filter_chart_and_export_use_same_records(application):
    app, client, service = application
    values = defaults("native") | {
        "native-sponsors.value": ["Sponsor A"],
        "native-metric.value": "percent",
    }
    result = callback(
        app, client, "native-grid.rowData", values, "native-sponsors.value"
    )
    rows = result["native-grid"]["rowData"]
    assert [r["record_id"] for r in rows] == ["native-a"]
    assert result["native-sponsors-chart"]["figure"]["data"][0]["x"] == [100.0]
    assert SECRET not in json.dumps(result)
    exported = callback(
        app,
        client,
        "native-download.data",
        values | {"native-export.n_clicks": 1},
        "native-export.n_clicks",
    )
    csv_rows = list(
        csv.DictReader(StringIO(exported["native-download"]["data"]["content"]))
    )
    assert [r["record_id"] for r in csv_rows] == [r["record_id"] for r in rows]
    assert service.browse_calls[-1] == service.browse_calls[-2]
    assert SECRET not in exported["native-download"]["data"]["content"]


@pytest.mark.parametrize("dataset", ["native", "social"])
@pytest.mark.parametrize("metric", ["count", "percent"])
@pytest.mark.parametrize(
    "dates",
    [("2024-01-01", None, "2024-02-01"), (None, None, None)],
    ids=["mixed-dates", "unknown-dates"],
)
def test_collection_uses_one_snapshot_for_rows_and_statistics(
    application, monkeypatch, dataset, metric, dates
):
    app, client, service = application
    source_rows = [dict(service.rows[0]), dict(service.rows[1]), dict(service.rows[0])]
    for index, row in enumerate(source_rows):
        row.update(
            record_id=f"{dataset}-{index}",
            dataset=dataset,
            date=dates[index],
            platform="Platform B" if index == 1 else "Platform A",
        )

    def browse_once(filters):
        service.browse_calls.append(filters)
        assert len(service.browse_calls) == 1, "A second read could see a newer import"
        return source_rows

    def reject_second_read(_filters):
        raise AssertionError("Statistics must use the already-read records")

    monkeypatch.setattr(service, "browse", browse_once)
    monkeypatch.setattr(service, "statistics", reject_second_read)
    monkeypatch.setattr(
        service,
        "health",
        lambda: {"record_counts": {dataset: len(source_rows)}},
    )
    result = callback(
        app,
        client,
        f"{dataset}-grid.rowData",
        defaults(dataset) | {f"{dataset}-metric.value": metric},
        f"{dataset}-metric.value",
    )

    rows = result[f"{dataset}-grid"]["rowData"]
    assert [row["record_id"] for row in rows] == [
        row["record_id"] for row in source_rows
    ]
    assert len(service.browse_calls) == 1
    assert service.browse_calls[0].dataset == dataset
    assert [row["date"] for row in source_rows] == list(dates)
    assert [row["date"] for row in rows] == [date or "(Unknown)" for date in dates]
    cards = result[f"{dataset}-summary"]["children"]
    summary = {
        card["props"]["children"][0]["props"]["children"]: card["props"]["children"][1][
            "props"
        ]["children"]
        for card in cards
    }
    assert summary == {
        "Selected records": "3",
        "Searchable records": "2",
        "Unknown dates": str(dates.count(None)),
    }
    for chart, names in (
        (
            "primary",
            ("Outlet A", "Outlet B")
            if dataset == "native"
            else ("Platform A", "Platform B"),
        ),
        ("sponsors", ("Sponsor A", "Sponsor B")),
    ):
        bars = result[f"{dataset}-{chart}-chart"]["figure"]["data"][0]
        displayed = dict(zip(bars["y"], bars["x"], strict=True))
        expected = [2, 1] if metric == "count" else [200 / 3, 100 / 3]
        assert displayed == pytest.approx(dict(zip(names, expected, strict=True)))
        assert sum(item[0] for item in bars["customdata"]) == len(rows)
        assert sum(item[1] for item in bars["customdata"]) == pytest.approx(100)
    timeline = result[f"{dataset}-timeline-chart"]["figure"]
    if all(date is None for date in dates):
        assert timeline["data"][0]["x"] == ["Unknown"]
        assert timeline["data"][0]["y"] == [3]
    else:
        assert timeline["data"][0]["x"] == ["2024", "Unknown"]
        assert timeline["data"][0]["y"] == [2, 1]
    assert timeline["data"][0]["type"] == "bar"
    assert result[f"{dataset}-record-count"]["children"].startswith(
        "3 eligible records"
    )
    assert result[f"{dataset}-status"]["children"] is None
    assert SECRET not in json.dumps(result)


def test_unknown_values_and_csv_formula_handling(application):
    app, client, _service = application
    result = callback(
        app,
        client,
        "native-grid.rowData",
        defaults("native"),
        "native-unknown-dates.value",
    )
    second = result["native-grid"]["rowData"][1]
    assert second["date"] == "(Unknown)"
    assert second["url"] == second["archive_url"] == ""
    exported = callback(
        app,
        client,
        "native-download.data",
        defaults("native") | {"native-export.n_clicks": 1},
        "native-export.n_clicks",
    )
    rows = list(
        csv.DictReader(StringIO(exported["native-download"]["data"]["content"]))
    )
    assert rows[1]["title"] == "'=FORMULA()"
    assert "javascript:" not in json.dumps(result)


def test_social_absence_is_explicit_and_has_no_fake_rows(application):
    app, client, _service = application
    result = callback(
        app, client, "social-grid.rowData", defaults("social"), "social-platforms.value"
    )
    assert result["social-grid"]["rowData"] == []
    assert "not connected" in json.dumps(result)


def test_free_search_never_calls_paid_answer_and_uses_active_filters(application):
    app, client, service = application
    values = research_values() | {
        "native-sponsors.value": ["Sponsor A"],
        "social-sponsors.value": ["Unrelated social sponsor"],
    }
    result = callback(
        app, client, "research-results.children", values, "search-free.n_clicks"
    )
    assert service.answer_calls == []
    assert service.search_calls[-1][1].sponsors == ["Sponsor A"]
    assert service.search_calls[-1][1].dataset == "native"
    serialized = json.dumps(result)
    assert "Last submitted search" in serialized
    assert "Sponsors: Sponsor A" in serialized
    assert "Unrelated social sponsor" not in serialized
    assert (
        "version-a" in serialized
        and "native-a" in serialized
        and ORIGINAL in serialized
    )


def test_all_scope_does_not_silently_carry_collection_filters(application):
    app, client, service = application
    values = research_values(scope="all") | {
        "native-sponsors.value": ["Sponsor A"],
        "social-platforms.value": ["Example platform"],
    }
    result = callback(
        app, client, "research-results.children", values, "search-free.n_clicks"
    )
    filters = service.search_calls[-1][1]
    assert filters.dataset == "all"
    assert filters.sponsors == filters.platforms == filters.publishers == []
    assert "all eligible records" in json.dumps(result)
    assert "Example platform" not in json.dumps(result)


def test_source_switch_covers_table_export_and_evidence():
    service = FakeService()
    app = create_app(
        service, SimpleNamespace(show_source_links=False, cookie_secret="test")
    )
    client = app.server.test_client()
    grid = callback(
        app, client, "native-grid.rowData", defaults("native"), "native-sponsors.value"
    )
    export = callback(
        app,
        client,
        "native-download.data",
        defaults("native") | {"native-export.n_clicks": 1},
        "native-export.n_clicks",
    )
    evidence = callback(
        app,
        client,
        "research-results.children",
        research_values(),
        "search-free.n_clicks",
    )
    for output in (grid, export, evidence):
        text = json.dumps(output)
        assert ORIGINAL not in text and ARCHIVE not in text
        assert SECRET not in text


def test_paid_answer_uses_server_session_and_preserves_evidence(application):
    app, client, service = application
    values = research_values() | {"answer-paid.n_clicks": 1}
    first = callback(
        app, client, "research-results.children", values, "answer-paid.n_clicks"
    )
    callback(
        app,
        client,
        "research-results.children",
        values | {"answer-paid.n_clicks": 2},
        "answer-paid.n_clicks",
    )
    assert len(service.answer_calls) == 2
    assert service.answer_calls[0][2] == service.answer_calls[1][2]
    assert service.answer_calls[0][2] not in json.dumps(first)
    assert "proposes a carbon capture project" in json.dumps(first)


@pytest.mark.parametrize("status", ["limited", "service_unavailable"])
def test_paid_service_status_hides_internal_messages(application, status):
    app, client, service = application
    service.answer_status = status
    result = callback(
        app,
        client,
        "research-results.children",
        research_values() | {"answer-paid.n_clicks": 1},
        "answer-paid.n_clicks",
    )
    assert SECRET not in json.dumps(result)
    assert "keyword search" in json.dumps(result)


def test_paid_failure_falls_back_to_keyword_evidence(application):
    app, client, service = application
    service.fail_answer = True
    result = callback(
        app,
        client,
        "research-results.children",
        research_values() | {"answer-paid.n_clicks": 1},
        "answer-paid.n_clicks",
    )
    assert len(service.search_calls) == 1
    assert "native-a" in json.dumps(result)
    assert SECRET not in json.dumps(result)


def test_invalid_dates_and_oversized_questions_do_not_call_services(application):
    app, client, service = application
    invalid = defaults("native") | {
        "native-dates.start_date": "2025-01-01",
        "native-dates.end_date": "2024-01-01",
    }
    result = callback(
        app, client, "native-grid.rowData", invalid, "native-dates.start_date"
    )
    assert "Check the filters" in json.dumps(result)
    assert service.browse_calls == []
    callback(
        app,
        client,
        "research-results.children",
        research_values() | {"research-question.value": "x" * 2001},
        "search-free.n_clicks",
    )
    assert service.search_calls == service.answer_calls == []


def test_collection_exception_is_sanitized(application):
    app, client, service = application
    service.fail_browse = True
    result = callback(
        app,
        client,
        "native-grid.rowData",
        defaults("native"),
        "native-publishers.value",
    )
    assert SECRET not in json.dumps(result)
    assert "temporarily unavailable" in json.dumps(result)


def test_three_route_views_have_unique_ids_and_persisted_shared_controls(application):
    app, client, service = application
    nodes = list(component_tree(client.get("/_dash-layout").json))
    ids = [node["props"]["id"] for node in nodes if "id" in node["props"]]
    assert len(ids) == len(set(ids))
    components = {
        node["props"]["id"]: node["props"] for node in nodes if "id" in node["props"]
    }
    for route in ("query", "data", "wireframe"):
        assert f"{route}-page" in components
        assert components[f"nav-{route}"]["href"] == f"/{route}"
        assert client.get(f"/{route}").status_code == 200
    for identity in (
        "native-sponsors",
        "social-sponsors",
        "active-dataset",
        "research-question",
        "search-scope",
        "native-dates",
    ):
        assert components[identity]["persistence"] is True
        assert components[identity]["persistence_type"] == "session"
    assert service.answer_calls == service.search_calls == []
    wireframe = json.dumps(components["wireframe-page"])
    assert "wire-screen" in wireframe and "wire-flow" in wireframe
    assert "awaiting dataset" in wireframe
    assert "PostgreSQL" in wireframe
    assert SECRET not in wireframe


@pytest.mark.parametrize(
    "pathname, page",
    [
        ("/", "query"),
        ("/query", "query"),
        ("/query/", "query"),
        ("/data", "data"),
        ("/wireframe", "wireframe"),
        ("/missing", "not-found"),
    ],
)
def test_routes_switch_visible_page_without_touching_controls_or_results(
    application, pathname, page
):
    app, client, service = application
    result = callback(
        app,
        client,
        "query-page.hidden",
        {"page-location.pathname": pathname},
        "page-location.pathname",
    )
    for target in ("query", "data", "wireframe", "not-found"):
        assert result[f"{target}-page"]["hidden"] == (target != page)
    assert result["collection-workspace"]["hidden"] == (page not in ("query", "data"))
    assert result["observatory-app"]["className"] == f"view-{page}"
    assert result["shared-filters"]["hidden"] == (page not in {"query", "data"})
    for control in ("query-composer", "query-options", "query-cost"):
        assert result[control]["hidden"] == (page != "query")
    assert all(
        "children" not in data for key, data in result.items() if key.endswith("-page")
    )
    assert not {
        "research-results",
        "research-submission",
        "research-question",
        "active-dataset",
        "native-sponsors",
    } & set(result)
    assert service.search_calls == service.answer_calls == []


@pytest.mark.parametrize("pathname", ["/data", "/wireframe", "/missing", None])
@pytest.mark.parametrize("button", ["search-free", "answer-paid"])
def test_hidden_query_actions_cannot_call_service(application, pathname, button):
    app, client, service = application
    values = research_values() | {
        "page-location.pathname": pathname,
        f"{button}.n_clicks": 1,
    }
    callback(
        app,
        client,
        "research-results.children",
        values,
        f"{button}.n_clicks",
        expected_status=204,
    )
    assert service.search_calls == service.answer_calls == []


@pytest.mark.parametrize(
    "trigger, clicks",
    [
        ("page-location.pathname", 1),
        ("answer-paid.n_clicks", 0),
        ("answer-paid.n_clicks", None),
        ("answer-paid.n_clicks", -1),
        ("answer-paid.n_clicks", "1"),
        ("answer-paid.n_clicks", True),
        ("active-dataset.value", 1),
    ],
)
def test_query_requires_explicit_positive_click_on_a_known_action(
    application, trigger, clicks
):
    app, client, service = application
    callback(
        app,
        client,
        "research-results.children",
        research_values() | {"answer-paid.n_clicks": clicks},
        trigger,
        expected_status=204,
    )
    assert service.search_calls == service.answer_calls == []


def test_results_become_stale_after_scope_change_and_clear_on_resubmit(application):
    app, client, service = application
    values = research_values() | {"native-sponsors.value": ["Sponsor A"]}
    result = callback(
        app, client, "research-results.children", values, "search-free.n_clicks"
    )
    snapshot = result["research-submission"]["data"]
    assert snapshot["filters"]["sponsors"] == ["Sponsor A"]
    before = callback(
        app,
        client,
        "research-stale.children",
        values | {"research-submission.data": snapshot},
        "research-submission.data",
    )
    assert before["research-stale"]["children"] is None
    changed = values | {
        "native-sponsors.value": ["Sponsor B"],
        "research-submission.data": snapshot,
    }
    stale = callback(
        app, client, "research-stale.children", changed, "native-sponsors.value"
    )
    assert "earlier selection" in json.dumps(stale)
    assert len(service.search_calls) == 1 and not service.answer_calls
    refreshed = callback(
        app, client, "research-results.children", changed, "search-free.n_clicks"
    )
    cleared = callback(
        app,
        client,
        "research-stale.children",
        changed
        | {"research-submission.data": refreshed["research-submission"]["data"]},
        "research-submission.data",
    )
    assert cleared["research-stale"]["children"] is None


def test_unrelated_collection_filters_do_not_mark_query_stale(application):
    app, client, _service = application
    values = research_values()
    submitted = callback(
        app, client, "research-results.children", values, "search-free.n_clicks"
    )["research-submission"]["data"]
    result = callback(
        app,
        client,
        "research-stale.children",
        values
        | {
            "research-submission.data": submitted,
            "social-sponsors.value": ["Not in active collection"],
        },
        "social-sponsors.value",
    )
    assert result["research-stale"]["children"] is None


def test_all_scope_summary_explains_filter_bypass_and_ignores_filter_changes(
    application,
):
    app, client, service = application
    values = research_values(scope="all")
    submitted = callback(
        app, client, "research-results.children", values, "search-free.n_clicks"
    )["research-submission"]["data"]
    changed = values | {
        "research-submission.data": submitted,
        "native-sponsors.value": ["Sponsor B"],
    }
    stale = callback(
        app, client, "research-stale.children", changed, "native-sponsors.value"
    )
    assert stale["research-stale"]["children"] is None
    scope = callback(
        app, client, "current-scope.children", changed, "native-sponsors.value"
    )
    assert "filters are bypassed" in json.dumps(scope)
    assert len(service.search_calls) == 1 and not service.answer_calls


def test_default_scope_is_quiet_but_active_filters_remain_visible(application):
    app, client, service = application
    values = research_values()
    default_scope = callback(
        app, client, "current-scope.children", values, "active-dataset.value"
    )
    assert default_scope["current-scope"]["children"] is None
    filtered_scope = callback(
        app,
        client,
        "current-scope.children",
        values | {"native-sponsors.value": ["Sponsor B"]},
        "native-sponsors.value",
    )
    assert "Sponsor B" in json.dumps(filtered_scope)
    assert service.answer_calls == service.search_calls == []


def test_route_navigation_never_dispatches_or_clears_existing_paid_answer(application):
    app, client, service = application
    values = research_values() | {"answer-paid.n_clicks": 1}
    answer = callback(
        app, client, "research-results.children", values, "answer-paid.n_clicks"
    )
    for path in ("/data", "/wireframe", "/query"):
        result = callback(
            app,
            client,
            "query-page.hidden",
            {"page-location.pathname": path},
            "page-location.pathname",
        )
        assert "research-results" not in result
        assert "research-submission" not in result
    assert len(service.answer_calls) == 1
    assert "proposes a carbon capture project" in json.dumps(answer)
    research_spec = next(
        spec
        for key, spec in app.callback_map.items()
        if "research-results.children" in key
    )
    assert {item["id"] for item in research_spec["inputs"]} == {
        "search-free",
        "answer-paid",
    }
    assert "page-location" in {item["id"] for item in research_spec["state"]}


@pytest.mark.parametrize("pathname", ["/query", "/wireframe", None])
def test_export_is_available_only_on_data_page(application, pathname):
    app, client, service = application
    callback(
        app,
        client,
        "native-download.data",
        defaults("native")
        | {"native-export.n_clicks": 1, "page-location.pathname": pathname},
        "native-export.n_clicks",
        expected_status=204,
    )
    assert not service.browse_calls
