"""Source-independent scope checks; no customer questions or references."""

import pytest

from observatory.question_policy import (
    literal_scope_preserved,
    statistics_request_preserves_question,
)

CONTEXT = {"sponsors": [{"value": "Linden Harbor Guild"}]}
BASE = {"dataset": "native"}
SPONSOR_FILTERS = {"sponsors": ["Linden Harbor Guild"]}


@pytest.mark.parametrize("period", [
    "last year", "this year", "previous month", "past 2 weeks", "next quarter",
    "yesterday", "year to date", "YTD", "去年", "今年", "上个月", "本季度",
    "今天", "过去2年", "last calendar year", "past two years", "上一年", "近两年",
])
def test_relative_calendar_cannot_silently_become_unfiltered(period):
    question = f"Count native ads for Linden Harbor Guild {period}"
    arguments = {"filters": SPONSOR_FILTERS, "group_by": "total"}
    assert not statistics_request_preserves_question(question, CONTEXT, arguments, BASE)
    assert not literal_scope_preserved(question, CONTEXT, SPONSOR_FILTERS, BASE)


def test_model_supplied_relative_endpoints_do_not_create_a_trusted_calendar():
    arguments = {"filters": {**SPONSOR_FILTERS,
                            "date_from": "2050-01-01", "date_to": "2050-12-31"}}
    assert not statistics_request_preserves_question(
        "Count native ads for Linden Harbor Guild last year", CONTEXT, arguments, BASE)


def test_existing_ui_dates_do_not_define_the_meaning_of_last_year():
    base = {**BASE, "date_from": "2048-01-01", "date_to": "2052-12-31"}
    assert not statistics_request_preserves_question(
        "Count native ads for Linden Harbor Guild last year", CONTEXT,
        {"filters": SPONSOR_FILTERS}, base)


def test_explicit_year_scope_remains_supported():
    arguments = {"filters": {**SPONSOR_FILTERS,
                            "date_from": "2051-01-01", "date_to": "2051-12-31"}}
    assert statistics_request_preserves_question(
        "Count native ads for Linden Harbor Guild in 2051", CONTEXT, arguments, BASE)


def test_relative_named_entity_is_data_rather_than_calendar_scope():
    context = {"sponsors": [{"value": "Last Year Cooperative"}]}
    filters = {"sponsors": ["Last Year Cooperative"]}
    question = "Count native ads for Last Year Cooperative"
    assert literal_scope_preserved(question, context, filters, BASE)
    assert statistics_request_preserves_question(question, context, {"filters": filters}, BASE)


def test_plain_counts_still_work_without_dates():
    assert statistics_request_preserves_question(
        "Count native ads for Linden Harbor Guild", CONTEXT,
        {"filters": SPONSOR_FILTERS}, BASE)
