"""Saved Query members reject source-state changes with unchanged counts/health."""

from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from test_query_records import (
    QueryService,
    page,
    record_ids,
    row,
    statistics_result,
    submit,
)

from observatory.app import create_app
from observatory.models import Filters
from observatory.social_annotations import social_state_id

CHOSEN = social_state_id("renewable_energy", "source_true")


class SourceStateQueryService(QueryService):
    def __init__(self):
        super().__init__()
        self.source_version = "a" * 64
        self.source_reads = []
        self.db = SimpleNamespace(social_source_state_version=self.source_state_version)
        self.rows = [row(i, dataset="social") | {"account": "Alpha", "platform": "Twitter",
                                               "labels": [CHOSEN]} for i in range(23)]

    def source_state_version(self, filters):
        self.source_reads.append(filters.model_copy(deep=True))
        return self.source_version


@pytest.fixture
def state_ui():
    service = SourceStateQueryService()
    app = create_app(service, SimpleNamespace(show_source_links=True, cookie_secret="source-state-query-test",
                                             monthly_budget_usd=100))
    return app, app.server.test_client(), service


def source_answer(service, *, include_guard=True):
    filters = Filters(dataset="social", labels=[CHOSEN], accounts=["Alpha"], platforms=["Twitter"])
    answer = statistics_result(service, filters)
    if include_guard:
        answer.structured_result["historical_source_state_guard"] = {
            "filters": filters.model_dump(mode="json"), "source_state_version": service.source_version,
        }
    return answer


def test_signed_full_scope_pages_all_23_source_state_members_without_models(state_ui):
    service = state_ui[2]
    _, values = submit(state_ui, source_answer(service))
    found = []
    for offset in (0, 10, 20):
        if offset:
            values["query-records-next.n_clicks"] += 1
        response = page(state_ui, values, "query-records-next.n_clicks" if offset else "query-records-scope.data")
        found.extend(unquote(value) for value in record_ids(response["query-records-content"]["children"]))
        values["query-records-offset.data"] = response["query-records-offset"]["data"]
    assert found == [item["record_id"] for item in service.rows]
    assert len(set(found)) == 23
    assert all(filters.dataset == "social" and filters.labels == [CHOSEN] and filters.accounts == ["Alpha"]
               and filters.platforms == ["Twitter"] for filters, _, _ in service.page_calls)
    assert len(service.answer_calls) == 1  # only submitted mock answer, no paging model calls
    assert all(filters.labels == [CHOSEN] for filters in service.source_reads)


@pytest.mark.parametrize("during_read", [False, True])
def test_same_health_and_count_cannot_hide_state_membership_change(state_ui, during_read):
    service = state_ui[2]
    _, values = submit(state_ui, source_answer(service))
    # A changed annotation can swap members without changing a record body,
    # health.data_version or selected count. The dedicated guard must see it.
    if during_read:
        service.page_after_hook = lambda: setattr(service, "source_version", "b" * 64)
    else:
        service.source_version = "b" * 64
    response = page(state_ui, values)
    assert record_ids(response["query-records-content"]["children"]) == []
    assert "Historical source states changed" in str(response)
    assert service.version == "source-v1" and len(service.rows) == 23
    assert len(service.page_calls) == int(during_read)


@pytest.mark.parametrize("mutation", ["absent", "distribution_absent", "malformed_hash", "outside_scope", "unknown_keys"])
def test_unverifiable_source_state_answers_do_not_issue_a_record_token(state_ui, mutation):
    service = state_ui[2]
    answer = source_answer(service, include_guard=mutation not in {"absent", "distribution_absent"})
    if mutation == "distribution_absent":
        answer.structured_result["kind"] = "social_historical_labels"
        answer.structured_result["filters"]["labels"] = []
    elif mutation != "absent":
        guard = answer.structured_result["historical_source_state_guard"]
        if mutation == "malformed_hash":
            guard["source_state_version"] = "unavailable"
        elif mutation == "outside_scope":
            guard["filters"]["labels"] = [social_state_id("renewable_energy", "source_false")]
        else:
            guard["private"] = "should never be signed"
    app = state_ui[0]
    with app.server.test_request_context():
        from observatory.query_records import statistics_records_panel

        panel = statistics_records_panel(deepcopy(answer.structured_result), service, True)
    assert "query-records-scope" not in str(panel.to_plotly_json())
    assert not service.page_calls


def test_current_new_answer_can_page_after_an_old_guard_expires(state_ui):
    service = state_ui[2]
    service.source_version = "b" * 64
    _, values = submit(state_ui, source_answer(service))
    response = page(state_ui, values)
    assert [unquote(value) for value in record_ids(response["query-records-content"]["children"])] == [
        item["record_id"] for item in service.rows[:10]]
