"""The actual Query UI distinguishes SQL results from generated explanations."""

import json
from types import SimpleNamespace

import pytest
from test_app import SECRET, FakeService, callback, component_tree, research_values

from observatory.app import create_app
from observatory.models import Answer


@pytest.fixture
def statistics_ui():
    service = FakeService()
    settings = SimpleNamespace(show_source_links=True, cookie_secret="statistics-test", monthly_budget_usd=100)
    app = create_app(service, settings)
    return app, app.server.test_client(), service


def structured_answer(service, group_by=None, groups=None, **updates):
    data = {
        "intent": "outlet_count" if group_by is None else "company_publishers",
        "data_version": service.health()["data_version"],
        "filters": {"dataset": "native", "publishers": ["The New York Times"], "include_unknown_dates": False},
        "group_by": group_by,
        "collections": [{"dataset": "native", "total": 28, "retrievable": 20, "unknown_dates": 0}],
        "groups": groups or [],
        "records": service.rows[:1],
        "scope_notes": ["Stored publication metadata determines outlet membership."],
    }
    data.update(updates)
    return Answer(status="answered", answer="There are 28 native ad records from the New York Times in this selection.",
                  answer_mode="statistics", structured_result=data)


def submit(ui, answer):
    app, client, service = ui
    original = service.answer

    def response(question, filters, visitor):
        original(question, filters, visitor)
        return answer

    service.answer = response
    return callback(app, client, "research-results.children",
                    research_values() | {"research-question.value": "How many native ads are from the New York Times?",
                                         "answer-paid.n_clicks": 1},
                    "answer-paid.n_clicks")


def test_outlet_count_is_labeled_as_database_statistics_and_shows_effective_scope(statistics_ui):
    response = submit(statistics_ui, structured_answer(statistics_ui[2]))
    rendered = json.dumps(response)
    assert "Collection statistics" in rendered
    assert "no model charge" in rendered
    assert "Generated answer" not in rendered
    assert "Insufficient evidence" not in rendered
    assert "28 native ad records" in rendered
    assert "28 matching" in rendered
    assert "20 searchable" in rendered
    assert "Last submitted search" in rendered
    assert "Publishers: The New York Times" in rendered
    assert "Unknown dates excluded" in rendered
    assert "not a census of all advertising" in rendered
    assert SECRET not in rendered
    assert len(statistics_ui[2].answer_calls) == 1
    assert statistics_ui[2].search_calls == []


def test_complete_publisher_table_does_not_stop_at_tenth_group(statistics_ui):
    groups = [{"dataset": "native", "name": f"Publisher {index:02d}", "display_name": f"Publisher {index:02d}", "count": index + 1}
              for index in range(14)]
    answer = structured_answer(statistics_ui[2], group_by="publishers", groups=groups,
                               collections=[{"dataset": "native", "total": 105, "retrievable": 80, "unknown_dates": 2}])
    response = submit(statistics_ui, answer)
    nodes = list(component_tree(response))
    table = next(node for node in nodes if node["type"] == "Table")
    rows = [node for node in component_tree(table) if node["type"] == "Tr"]
    assert len(rows) == 15  # fourteen complete categories plus the column header
    assert all(group["name"] in json.dumps(table) for group in groups)
    assert "All publishers and counts" in json.dumps(table)
    assert "Native ad records" in json.dumps(table)
    assert "105 matching" in json.dumps(response)
    assert "2 with unknown dates" in json.dumps(response)
    counts = [node["props"]["children"] for node in component_tree(table)
              if node["type"] == "Td" and node["props"].get("className") == "count-value"]
    assert counts == [str(group["count"]) for group in groups]


def test_source_sponsor_display_does_not_merge_company_event_and_alias_rows(statistics_ui):
    groups = [
        {"dataset": "native", "name": "exxonmobil", "display_name": "ExxonMobil", "count": 5},
        {"dataset": "native", "name": "cera", "display_name": "CERAWeek", "count": 2},
        {"dataset": "native", "name": "Williams", "display_name": "Williams", "count": 1},
        {"dataset": "native", "name": "Williams Companies", "display_name": "Williams Companies", "count": 1},
    ]
    answer = structured_answer(statistics_ui[2], group_by="sponsors", groups=groups)
    response = submit(statistics_ui, answer)
    table = next(node for node in component_tree(response) if node["type"] == "Table")
    assert "All source-listed sponsors / organizations and counts" in json.dumps(table)
    rows = [node for node in component_tree(table) if node["type"] == "Tr"]
    assert len(rows) == 5
    assert all(group["display_name"] in json.dumps(table) for group in groups)


def test_matching_record_inspection_is_collapsed_and_has_working_detail_links(statistics_ui):
    response = submit(statistics_ui, structured_answer(statistics_ui[2]))
    details = next(node for node in component_tree(response)
                   if node["type"] == "Details" and node["props"].get("className") == "statistics-records")
    assert not details["props"].get("open", False)
    assert "showing 1 of 28" in json.dumps(details)
    links = [node["props"] for node in component_tree(details) if node["type"] in ("A", "Link")]
    assert any(link.get("href") == "/records/native-a" and link.get("children") == "A capture proposal" for link in links)
    assert not any(link.get("href") == "/data" for link in links)
    controls = {node["props"].get("id") for node in component_tree(details)}
    assert {"query-records-prev", "query-records-next", "query-records-reset", "query-records-scope"} <= controls
    assert SECRET not in json.dumps(details)


def test_dual_collection_totals_keep_native_and_social_units_visible(statistics_ui):
    answer = structured_answer(statistics_ui[2],
                               filters={"dataset": "all"}, records=[],
                               collections=[{"dataset": "native", "total": 263, "retrievable": 226, "unknown_dates": 22},
                                            {"dataset": "social", "total": 0, "retrievable": 0, "unknown_dates": 0}])
    response = submit(statistics_ui, answer)
    rendered = json.dumps(response)
    assert "Native ad records: 263 matching" in rendered
    assert "Company posts: 0 matching" in rendered  # not verified paid ads
    assert "Native articles and social posts are separate units" in rendered
    browser = next(node for node in component_tree(response)
                   if node["type"] == "Details" and node["props"].get("className") == "statistics-records")
    collection = next(node for node in component_tree(browser) if node["props"].get("id") == "query-records-collection")
    assert [option["value"] for option in collection["props"]["options"]] == ["native"]


def test_share_displays_actual_percentage_and_explicit_parent_scope(statistics_ui):
    answer = structured_answer(
        statistics_ui[2], kind="share", denominator_filters={"dataset": "native", "sponsors": ["Sponsor A"]},
        collections=[{"dataset": "native", "total": 2, "retrievable": 1, "unknown_dates": 0,
                      "numerator": 2, "denominator": 9, "percentage": 200 / 9}],
    )
    rendered = json.dumps(submit(statistics_ui, answer))
    assert "22.22%" in rendered
    assert "Matching records" in rendered and "Current selection" in rendered
    assert "Denominator" in rendered
    assert "Sponsors: Sponsor A" in rendered
    assert "Publishers: The New York Times" in rendered
    assert SECRET not in rendered


def test_empty_share_is_undefined_and_collections_remain_separate(statistics_ui):
    answer = structured_answer(
        statistics_ui[2], kind="share", denominator_filters={"dataset": "all"}, records=[],
        collections=[{"dataset": "native", "total": 1, "retrievable": 1, "unknown_dates": 0,
                      "numerator": 1, "denominator": 4, "percentage": 25.0},
                     {"dataset": "social", "total": 0, "retrievable": 0, "unknown_dates": 0,
                      "numerator": 0, "denominator": 0, "percentage": None}],
    )
    rendered = json.dumps(submit(statistics_ui, answer))
    assert "25.00%" in rendered
    assert "Undefined" in rendered and "empty selection" in rendered
    assert "Native ad records" in rendered and "Company posts" in rendered
    assert "0.00%" not in rendered


def test_clarification_is_explicit_and_never_labeled_as_generated_answer(statistics_ui):
    response = submit(statistics_ui, Answer(status="insufficient_evidence", answer_mode="clarification",
                                           answer="Please choose a company or news outlet before comparing counts."))
    rendered = json.dumps(response)
    assert "Question needs clarification" in rendered
    assert "Clarify this question" in rendered
    assert "no model charge" in rendered
    assert "Please choose a company or news outlet" in rendered
    assert "Generated answer" not in rendered
    assert "Insufficient evidence" not in rendered
    assert statistics_ui[2].search_calls == []


def test_statistics_failure_is_sanitized_without_generated_answer_badge(statistics_ui):
    response = submit(statistics_ui, Answer(status="service_unavailable", answer_mode="statistics", answer=SECRET))
    rendered = json.dumps(response)
    assert "Collection statistics" in rendered
    assert "no model charge" in rendered
    assert "Answer service unavailable" in rendered
    assert "Generated answer" not in rendered
    assert SECRET not in rendered


def test_model_limit_is_distinguished_from_an_outage_without_exposing_details(statistics_ui):
    limited = submit(statistics_ui, Answer(status="limited", answer_mode="tools", answer=SECRET))
    rendered = json.dumps(limited)
    assert "Paid answers temporarily limited" in rendered
    assert "request limit or project API budget was reached" in rendered
    assert "keyword search" in rendered
    assert SECRET not in rendered
    outage = submit(statistics_ui, Answer(status="service_unavailable", answer_mode="tools", answer=SECRET))
    assert "request limit or project API budget was reached" not in json.dumps(outage)
    assert SECRET not in json.dumps(outage)


@pytest.mark.parametrize("identifier,question", [
    ("example-outlet-count", "How many native ads are from the New York Times?"),
    ("example-company-outlets", "Which publishers is ExxonMobil working with?"),
    ("example-outlet-sponsors", "Which fossil fuel companies has the Washington Post worked with?"),
])
def test_example_buttons_fill_question_without_submitting_or_answering(statistics_ui, identifier, question):
    app, client, service = statistics_ui
    response = callback(app, client, "research-question.value", {f"{identifier}.n_clicks": 1}, f"{identifier}.n_clicks")
    assert response == {"research-question": {"value": question}}
    assert service.answer_calls == []
    assert service.search_calls == []
    assert service.browse_calls == []


@pytest.mark.parametrize("clicks", [None, 0, -1, "1"])
def test_example_non_click_does_not_change_question_or_make_calls(statistics_ui, clicks):
    app, client, service = statistics_ui
    callback(app, client, "research-question.value", {"example-outlet-count.n_clicks": clicks},
             "example-outlet-count.n_clicks", expected_status=204)
    assert service.answer_calls == []
    assert service.search_calls == []
