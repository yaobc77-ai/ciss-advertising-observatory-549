"""Actual Query callbacks browse all saved statistics records without models."""

import copy
import json
from types import SimpleNamespace

import pytest
from test_app import SECRET, FakeService, callback, component_tree, research_values

from observatory.app import create_app
from observatory.models import Answer, Filters
from observatory.query_records import PAGE_SIZE


def row(index, *, dataset="native", publisher="The New York Times", sponsor="exxonmobil"):
    return {
        "record_id": f"{dataset}-{publisher}-{index:02d}", "version_id": f"version-{index}",
        "dataset": dataset, "publisher": publisher, "sponsor": sponsor, "platform": "Website",
        "keyword": "CCS", "labels": ["legacy"], "title": f"{publisher} article {index:02d}",
        "date": "2024-02-01", "url": f"https://example.test/{dataset}/{index}",
        "archive_url": "", "retrievable": True, "raw": {"private": SECRET}, "body": SECRET,
    }


class QueryService(FakeService):
    def __init__(self):
        super().__init__()
        self.rows = [row(i) for i in range(23)] + [row(i, publisher="Forbes", sponsor="bp") for i in range(10)]
        self.page_calls = []
        self.version = "source-v1"
        self.fail_page = False
        self.available = True
        self.page_after_hook = None

    def health(self):
        return {"status": "ok" if self.available else "unavailable", "data_version": self.version,
                "record_counts": {name: sum(r["dataset"] == name for r in self.rows) for name in ("native", "social")}}

    def _selected(self, filters):
        selected = []
        for item in self.rows:
            if filters.dataset not in ("all", item["dataset"]):
                continue
            if any(getattr(filters, plural) and item.get(field) not in getattr(filters, plural)
                   for plural, field in (("publishers", "publisher"), ("sponsors", "sponsor"),
                                         ("platforms", "platform"), ("keywords", "keyword"))):
                continue
            if filters.labels and not set(filters.labels) & set(item.get("labels", [])):
                continue
            if filters.record_ids and item["record_id"] not in filters.record_ids:
                continue
            published = item["date"]
            if not published:
                if not filters.include_unknown_dates:
                    continue
            elif ((filters.date_from and published < filters.date_from.isoformat()) or
                  (filters.date_to and published > filters.date_to.isoformat())):
                continue
            selected.append(copy.deepcopy(item))
        return selected

    def page(self, filters, offset=0, limit=20, **_kwargs):
        self.page_calls.append((filters.model_copy(deep=True), offset, limit))
        if self.fail_page:
            raise RuntimeError(SECRET)
        selected = self._selected(filters)
        offset = min(offset, max(0, (len(selected) - 1) // limit * limit))
        page = {"rows": selected[offset:offset + limit], "total": len(selected), "offset": offset}
        if self.page_after_hook:
            self.page_after_hook()
        return page

    def answer(self, question, filters, visitor):
        self.answer_calls.append((question, filters.model_copy(deep=True), visitor))
        return self.answer_result


@pytest.fixture
def query_ui():
    service = QueryService()
    app = create_app(service, SimpleNamespace(show_source_links=True, cookie_secret="query-pager-test", monthly_budget_usd=100))
    return app, app.server.test_client(), service


def statistics_result(service, filters=None, *, kind="count", denominator=None, stamp=True):
    filters = filters or Filters(publishers=["The New York Times"])
    denominator = denominator or Filters()
    collections, records = [], []
    datasets = ("native", "social") if filters.dataset == "all" else (filters.dataset,)
    for dataset in datasets:
        selected = service._selected(filters.model_copy(update={"dataset": dataset}))
        n = len(selected)
        item = {"dataset": dataset, "total": n, "retrievable": n, "unknown_dates": sum(not r["date"] for r in selected)}
        if kind == "share":
            d = len(service._selected(denominator.model_copy(update={"dataset": dataset})))
            item.update(numerator=n, denominator=d, percentage=100 * n / d if d else None,
                        percentage_status="defined" if d else "empty_selection")
        collections.append(item)
        records.extend(selected[:PAGE_SIZE])
    data = {"kind": kind, "method": "database", "filters": filters.model_dump(mode="json"),
            "group_by": None, "collections": collections, "groups": [], "records": records, "scope_notes": []}
    if stamp:
        data["data_version"] = service.version
    if kind == "share":
        data.update(denominator_filters=denominator.model_dump(mode="json"),
                    denominator_basis="current_selection_before_question_targets")
    return Answer(status="answered", answer_mode="statistics", answer="Saved statistics answer", structured_result=data)


def props(rendered, identifier):
    return next(node["props"] for node in component_tree(rendered) if node["props"].get("id") == identifier)


def submit(ui, answer):
    app, client, service = ui
    service.answer_result = answer
    response = callback(app, client, "research-results.children",
                        research_values() | {"answer-paid.n_clicks": 1}, "answer-paid.n_clicks")
    rendered = response["research-results"]["children"]
    return rendered, {
        "query-records-scope.data": props(rendered, "query-records-scope")["data"],
        "query-records-collection.value": props(rendered, "query-records-collection")["value"],
        "query-records-offset.data": 0, "query-records-prev.n_clicks": 0,
        "query-records-next.n_clicks": 0, "query-records-reset.n_clicks": 0,
        "page-location.pathname": "/query",
    }


def page(ui, values, changed="query-records-scope.data", expected_status=200):
    return callback(ui[0], ui[1], "query-records-content.children", values, changed, expected_status=expected_status)


def record_ids(rendered):
    return [node["props"]["href"].removeprefix("/records/") for node in component_tree(rendered)
            if node["type"] == "A" and node["props"].get("href", "").startswith("/records/")]


def test_all_matching_records_are_reachable_with_previous_next_and_first_page(query_ui):
    _, values = submit(query_ui, statistics_result(query_ui[2]))
    first = page(query_ui, values)
    assert len(record_ids(first)) == 10 and "1\u201310 of 23" in first["query-records-summary"]["children"]
    assert first["query-records-prev"]["disabled"] and not first["query-records-next"]["disabled"]
    values["query-records-next.n_clicks"] = 1
    second = page(query_ui, values, "query-records-next.n_clicks")
    assert second["query-records-offset"]["data"] == 10 and len(record_ids(second)) == 10
    assert "11\u201320 of 23" in second["query-records-summary"]["children"]
    assert not set(record_ids(first)) & set(record_ids(second))
    values["query-records-offset.data"] = 10
    last = page(query_ui, values, "query-records-next.n_clicks")
    assert last["query-records-offset"]["data"] == 20 and len(record_ids(last)) == 3
    assert last["query-records-next"]["disabled"] and not last["query-records-prev"]["disabled"]
    assert len(set(record_ids(first) + record_ids(second) + record_ids(last))) == 23
    values["query-records-offset.data"] = 20
    values["query-records-prev.n_clicks"] = 1
    previous = page(query_ui, values, "query-records-prev.n_clicks")
    assert record_ids(previous) == record_ids(second)
    values["query-records-reset.n_clicks"] = 1
    reset = page(query_ui, values, "query-records-reset.n_clicks")
    assert record_ids(reset) == record_ids(first) and reset["query-records-offset"]["data"] == 0
    assert len(query_ui[2].answer_calls) == 1 and query_ui[2].search_calls == []


def test_pager_preserves_full_saved_scope_and_record_ids_after_composer_changes(query_ui):
    service = query_ui[2]
    scope = Filters(dataset="native", publishers=["The New York Times"], sponsors=["exxonmobil"],
                    keywords=["CCS"], platforms=["Website"], labels=["legacy"],
                    record_ids=[r["record_id"] for r in service.rows[:21]],
                    date_from="2024-01-01", date_to="2024-12-31", include_unknown_dates=False)
    _, values = submit(query_ui, statistics_result(service, scope))
    values.update(research_values("social", "all"))  # Draft controls no longer describe the submitted answer.
    values.update({"native-publishers.value": ["Forbes"], "research-question.value": "different question",
                   "query-records-next.n_clicks": 1, "page-location.pathname": "/query"})
    service.answer = lambda *_args, **_kwargs: pytest.fail("paging must not regenerate an answer")
    service.search = lambda *_args, **_kwargs: pytest.fail("paging must not perform evidence search")
    response = page(query_ui, values, "query-records-next.n_clicks")
    assert service.page_calls[-1] == (scope, 10, 10)
    assert len(record_ids(response)) == 10
    assert all("New%20York" in identifier for identifier in record_ids(response))


def test_share_pages_numerator_not_denominator_and_keeps_statistics_unchanged(query_ui):
    service = query_ui[2]
    denominator = Filters()
    numerator = Filters(publishers=["The New York Times"])
    rendered, values = submit(query_ui, statistics_result(service, numerator, kind="share", denominator=denominator))
    before = json.dumps(rendered)
    assert "69.70%" in before and "Denominator" in before
    values["query-records-next.n_clicks"] = 1
    response = page(query_ui, values, "query-records-next.n_clicks")
    assert service.page_calls[-1][0] == numerator
    assert all("Forbes" not in identifier for identifier in record_ids(response))
    assert "research-results" not in response and json.dumps(rendered) == before
    assert "11\u201320 of 23" in response["query-records-summary"]["children"]


def test_company_publisher_list_pages_the_same_company_records(query_ui):
    service = query_ui[2]
    for item in service.rows[:7]:
        item["publisher"] = "Forbes"
    scope = Filters(sponsors=["exxonmobil"])
    answer = statistics_result(service, scope, kind="list_publishers")
    answer.structured_result.update(group_by="publishers", groups=[
        {"dataset": "native", "name": "The New York Times", "count": 16},
        {"dataset": "native", "name": "Forbes", "count": 7},
    ])
    rendered, values = submit(query_ui, answer)
    assert "All publishers and counts" in json.dumps(rendered)
    values["query-records-next.n_clicks"] = 1
    response = page(query_ui, values, "query-records-next.n_clicks")
    assert service.page_calls[-1][0] == scope and len(record_ids(response)) == 10
    assert "11\u201320 of 23" in response["query-records-summary"]["children"]


def test_empty_selection_has_explicit_state_and_both_page_buttons_disabled(query_ui):
    scope = Filters(publishers=["Missing outlet"])
    rendered, values = submit(query_ui, statistics_result(query_ui[2], scope))
    assert "No matching records" in json.dumps(rendered)
    response = page(query_ui, values)
    assert "0\u20130 of 0" in response["query-records-summary"]["children"] and not record_ids(response)
    assert all(response[name]["disabled"] for name in ("query-records-prev", "query-records-next", "query-records-reset"))


def test_service_error_is_sanitized_and_first_page_retries_without_models(query_ui):
    service = query_ui[2]
    rendered, values = submit(query_ui, statistics_result(service))
    original = json.dumps(rendered)
    service.fail_page = True
    values["query-records-next.n_clicks"] = 1
    failed = page(query_ui, values, "query-records-next.n_clicks")
    assert "temporarily unavailable" in json.dumps(failed) and SECRET not in json.dumps(failed)
    assert not failed["query-records-reset"]["disabled"] and not record_ids(failed)
    assert "research-results" not in failed and json.dumps(rendered) == original
    service.fail_page = False
    values["query-records-reset.n_clicks"] = 1
    recovered = page(query_ui, values, "query-records-reset.n_clicks")
    assert len(record_ids(recovered)) == 10 and len(service.answer_calls) == 1


@pytest.mark.parametrize("invalid", [None, "", "fabricated", {}, 1])
def test_missing_or_forged_scope_never_falls_back_to_unfiltered_records(query_ui, invalid):
    _, values = submit(query_ui, statistics_result(query_ui[2]))
    values["query-records-scope.data"] = invalid
    response = page(query_ui, values)
    assert "Submit the question again" in json.dumps(response) and not record_ids(response)
    assert query_ui[2].page_calls == []


def test_collection_switch_keeps_units_separate_and_resets_offset(query_ui):
    service = query_ui[2]
    service.rows.extend(row(i, dataset="social", publisher="Social platform") for i in range(12))
    rendered, values = submit(query_ui, statistics_result(service, Filters(dataset="all")))
    assert [option["value"] for option in props(rendered, "query-records-collection")["options"]] == ["native", "social"]
    values.update({"query-records-collection.value": "social", "query-records-offset.data": 20})
    response = page(query_ui, values, "query-records-collection.value")
    assert service.page_calls[-1][0] == Filters(dataset="social") and response["query-records-offset"]["data"] == 0
    assert "12 matching social ad records" in json.dumps(response) and len(record_ids(response)) == 10
    assert all(identifier.startswith("social-") for identifier in record_ids(response))
    values.update({"query-records-next.n_clicks": 1, "query-records-offset.data": 0})
    final = page(query_ui, values, "query-records-next.n_clicks")
    assert len(record_ids(final)) == 2 and final["query-records-next"]["disabled"]


def test_unloaded_collection_is_not_added_by_forged_selector(query_ui):
    _, values = submit(query_ui, statistics_result(query_ui[2]))
    values["query-records-collection.value"] = "social"
    response = page(query_ui, values, "query-records-collection.value")
    assert "saved record selection is unavailable" in json.dumps(response)
    assert query_ui[2].page_calls == []


def test_source_change_before_page_requires_resubmit_without_querying_rows(query_ui):
    service = query_ui[2]
    _, values = submit(query_ui, statistics_result(service))
    service.version = "source-v2"
    response = page(query_ui, values)
    assert "collection changed" in json.dumps(response) and "Submit the question again" in json.dumps(response)
    assert service.page_calls == [] and not record_ids(response)
    assert response["query-records-reset"]["disabled"]


def test_source_change_during_page_discards_rows(query_ui):
    service = query_ui[2]
    _, values = submit(query_ui, statistics_result(service))
    service.page_after_hook = lambda: setattr(service, "version", "source-v2")
    response = page(query_ui, values)
    assert "collection changed" in json.dumps(response) and not record_ids(response)
    assert len(service.page_calls) == 1


def test_matching_total_change_even_with_same_version_requires_resubmit(query_ui):
    service = query_ui[2]
    _, values = submit(query_ui, statistics_result(service))
    service.rows.pop(0)
    response = page(query_ui, values)
    assert "collection changed" in json.dumps(response) and not record_ids(response)


def test_trusted_answer_source_version_prevents_rendering_a_newer_collection(query_ui):
    app, client, service = query_ui
    answer = statistics_result(service)
    service.version = "source-v2"
    service.answer_result = answer
    response = callback(app, client, "research-results.children", research_values() | {"answer-paid.n_clicks": 1},
                        "answer-paid.n_clicks")
    assert "collection changed" in json.dumps(response)
    assert not any(node["props"].get("id") == "query-records-scope" for node in component_tree(response))
    assert not record_ids(response)


@pytest.mark.parametrize("missing_version", [None, "", "unavailable", False, 0])
def test_unstamped_answer_never_creates_pager_or_reads_records(query_ui, missing_version):
    app, client, service = query_ui
    answer = statistics_result(service, stamp=False)
    if missing_version is not None:
        answer.structured_result["data_version"] = missing_version
    # Equal counts cannot justify attaching current records to an unbound answer.
    service.version = "source-v2"
    service.answer_result = answer
    response = callback(app, client, "research-results.children", research_values() | {"answer-paid.n_clicks": 1},
                        "answer-paid.n_clicks")
    assert "saved record selection cannot be verified" in json.dumps(response)
    assert "Submit the question again" in json.dumps(response)
    assert not any(node["props"].get("id") == "query-records-scope" for node in component_tree(response))
    assert not record_ids(response) and service.page_calls == []


@pytest.mark.parametrize("clicks", [None, 0, -1, "1", True])
def test_nonclicks_do_not_read_records(query_ui, clicks):
    _, values = submit(query_ui, statistics_result(query_ui[2]))
    values["query-records-next.n_clicks"] = clicks
    page(query_ui, values, "query-records-next.n_clicks", expected_status=204)
    assert query_ui[2].page_calls == []


def test_other_route_does_not_read_records(query_ui):
    _, values = submit(query_ui, statistics_result(query_ui[2]))
    values["page-location.pathname"] = "/data"
    page(query_ui, values, expected_status=204)
    assert query_ui[2].page_calls == []


def test_strict_dash_validation_includes_dynamic_query_record_components(query_ui):
    app, client, _ = query_ui
    assert app.config.suppress_callback_exceptions is False
    validation = json.loads(json.dumps(app.validation_layout, default=lambda node: node.to_plotly_json()))
    ids = {node["props"].get("id") for node in component_tree(validation)}
    assert {"query-records-content", "query-records-prev", "query-records-next", "query-records-scope"} <= ids
    assert client.get("/_dash-layout").status_code == 200
