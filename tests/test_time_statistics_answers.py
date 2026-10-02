"""Complete time results are rendered from database counts, with no model calls."""

import json
from copy import deepcopy

import pytest
from test_app import FakeService

from observatory.app import _statistics_card
from observatory.models import Filters
from observatory.service import Service


def result_data(kind="list_years"):
    filters = Filters(dataset="native", include_inferred_dates=False)
    return {
        "kind": kind, "method": "database", "filters": filters.model_dump(mode="json"),
        "group_by": "years", "collections": [
            {"dataset": "native", "total": 9, "retrievable": 8, "unknown_dates": 1},
        ], "groups": [{"dataset": "native", "name": name, "count": count}
                      for name, count in (("2019", 2), ("2020", 3), ("2021", 3))],
        "records": [], "scope_notes": ["Counts describe the selected collection."],
        "date_inference": {"used": False, "note": "Original publication dates only."},
    }


def text(component):
    return json.dumps(component, default=lambda item: item.to_plotly_json())


def test_year_answer_reconciles_totals_and_ui_labels_years_and_unknowns():
    data = result_data()
    result = Service._tool_statistics_answer(data, base_filters=Filters())
    assert result.status == "answered" and result.answer_mode == "statistics"
    assert "9 native ad records" in result.answer and "1 native ad records have no date" in result.answer
    card = text(_statistics_card(result.model_dump(mode="json"), True, FakeService()))
    assert "All years and counts" in card and '"children": "Year"' in card
    assert all(year in card for year in ("2019", "2020", "2021"))
    assert "1 with unknown dates" in card and "Original publication dates only." in card


def test_highest_year_answer_preserves_every_tie_and_unknowns():
    data = result_data("top_years")
    data["groups"] = data["groups"][1:]
    result = Service._tool_statistics_answer(data, base_filters=Filters())
    assert "2020, 2021 tie" in result.answer and "3 records per year" in result.answer
    assert "1 native ad records have no date" in result.answer
    card = text(_statistics_card(result.model_dump(mode="json"), True, FakeService()))
    assert "Years with the highest count (all ties)" in card


def period_data():
    data = result_data("compare_periods")
    data.update(group_by=None, groups=[], periods=[])
    for label, bounds, count in [
        ("Before 2020", {"date_to": "2019-12-31"}, 2),
        ("2020 onward", {"date_from": "2020-01-01"}, 6),
    ]:
        scope = Filters.model_validate(data["filters"] | bounds | {"include_unknown_dates": False})
        data["periods"].append({"label": label, "filters": scope.model_dump(mode="json"),
                                "collections": [{"dataset": "native", "total": count}]})
    return data


def test_both_time_periods_difference_and_missing_dates_reach_answer_and_table():
    data = period_data()
    result = Service._tool_statistics_answer(data, base_filters=Filters())
    assert "2 in Before 2020; 6 in 2020 onward" in result.answer
    assert "2020 onward has 4 more records" in result.answer
    assert "1 native ad records have no date" in result.answer
    card = text(_statistics_card(result.model_dump(mode="json"), True, FakeService()))
    assert "Counts for each requested period" in card
    assert "Before 2020" in card and "2020 onward" in card
    assert "2019-12-31" in card and "2020-01-01" in card


def test_equal_periods_are_reported_as_equal():
    data = period_data()
    data["periods"][1]["collections"][0]["total"] = 2
    result = Service._tool_statistics_answer(data, base_filters=Filters())
    assert "equal counts" in result.answer


@pytest.mark.parametrize("invalid", ["missing_period", "missing_collection", "negative_count", "broader_scope"])
def test_partial_or_invalid_period_answers_cannot_be_marked_complete(invalid):
    data = period_data()
    base = Filters()
    if invalid == "missing_period":
        data["periods"].pop()
    elif invalid == "missing_collection":
        data["periods"][1]["collections"] = []
    elif invalid == "negative_count":
        data["periods"][1]["collections"][0]["total"] = -1
    else:
        base = Filters(publishers=["A source outlet"])
    with pytest.raises(ValueError):
        Service._tool_statistics_answer(data, base_filters=base)


def test_year_distribution_cannot_omit_records_silently():
    data = result_data()
    data["groups"].pop()
    with pytest.raises(ValueError, match="reconcile"):
        Service._tool_statistics_answer(data, base_filters=Filters())


def test_highest_years_cannot_have_different_counts():
    data = deepcopy(result_data("top_years"))
    with pytest.raises(ValueError, match="ties"):
        Service._tool_statistics_answer(data, base_filters=Filters())
