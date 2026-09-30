"""Client questions resolve to complete counts and scoped record drilldowns."""

import json

import pytest
from dash import Dash, dcc, html
from test_app import SECRET, callback, component_tree

from observatory.analytics import UNKNOWN
from observatory.research_explorer import (
    ALL_RECORDS,
    explorer_panel,
    register_research_explorer,
)
from observatory.service import summarize


class ExplorerService:
    def __init__(self):
        self.statistics_calls = []
        self.page_calls = []
        self.fail_counts = False
        self.fail_records = False
        self.rows = [
            {"record_id": f"e-{i}", "version_id": f"v-{i}", "dataset": "native", "sponsor": "exxonmobil",
             "publisher": f"Outlet {i:02d}", "date": "2024-06-01", "title": f"Exxon article {i}",
             "keyword": "CCS", "platform": "Website", "labels": ["legacy"], "retrievable": False,
             "url": "https://example.test/source", "archive_url": "", "internal_path": SECRET}
            for i in range(14)
        ]
        self.rows += [
            {**self.rows[0], "record_id": f"wp-{i}", "sponsor": sponsor, "publisher": "The Washington Post",
             "title": f"Washington article {i}", "date": None if i == 0 else "2024-06-01"}
            for i, sponsor in enumerate(["exxonmobil", "bp", "api", "cera", "", "Williams", "Williams Companies"])
        ]

    def facets(self, dataset):
        assert dataset == "native"
        return {field + "s": sorted({row[field] or UNKNOWN for row in self.rows}) for field in ("sponsor", "publisher")}

    def selected(self, filters):
        result = []
        for row in self.rows:
            if filters.dataset != "native":
                continue
            if any(getattr(filters, plural) and (row.get(field) or UNKNOWN) not in getattr(filters, plural)
                   for field, plural in [("sponsor", "sponsors"), ("publisher", "publishers"),
                                         ("keyword", "keywords"), ("platform", "platforms")]):
                continue
            if filters.labels and not set(filters.labels).intersection(row["labels"]):
                continue
            if filters.record_ids and row["record_id"] not in filters.record_ids:
                continue
            if not row["date"] and not filters.include_unknown_dates:
                continue
            if row["date"] and ((filters.date_from and row["date"] < filters.date_from.isoformat())
                                or (filters.date_to and row["date"] > filters.date_to.isoformat())):
                continue
            result.append(row)
        return result

    def statistics(self, filters):
        self.statistics_calls.append(filters.model_copy(deep=True))
        if self.fail_counts:
            raise RuntimeError(SECRET)
        return summarize(self.selected(filters))

    def page(self, filters, offset=0, limit=20):
        self.page_calls.append((filters.model_copy(deep=True), offset, limit))
        if self.fail_records:
            raise RuntimeError(SECRET)
        rows = self.selected(filters)
        offset = min(offset, max(0, (len(rows) - 1) // limit * limit))
        return {"rows": rows[offset:offset + limit], "total": len(rows), "offset": offset}


@pytest.fixture
def explorer_app():
    app = Dash(__name__)
    service = ExplorerService()
    app.layout = html.Div([dcc.Store(id="native-network-data"), explorer_panel()])
    register_research_explorer(app, service, links_enabled=True)
    return app, app.server.test_client(), service


def values(direction="sponsor", anchor=None, **filters):
    return {
        "native-network-data.data": {"relationships": [{"sponsor": "FAKE", "publisher": "FAKE", "count": 999}],
                                       "filters": {"dataset": "native", **filters}},
        "research-direction.value": direction, "research-anchor.value": anchor,
        "research-counterpart.value": ALL_RECORDS, "research-record-offset.data": 0,
    }


def explore(explorer_app, inputs, changed="native-network-data.data"):
    app, client, _ = explorer_app
    return callback(app, client, "research-count-chart.figure", inputs, changed)


def test_company_default_returns_every_outlet_with_number_and_records(explorer_app):
    response = explore(explorer_app, values())
    figure = response["research-count-chart"]["figure"]
    assert response["research-anchor"]["value"] == "exxonmobil"
    assert len(figure["data"][0]["customdata"]) == 15
    assert sum(figure["data"][0]["x"]) == 15
    assert "15 native ad records across 15 source-listed news outlets" in response["research-count-scope"]["children"]
    assert response["research-count-heading"]["children"] == "Where does ExxonMobil appear?"
    assert response["research-next"]["disabled"] is False
    assert SECRET not in json.dumps(response)
    assert "FAKE" not in json.dumps(response)
    assert explorer_app[2].statistics_calls[-1].sponsors == ["exxonmobil"]


def test_direction_switch_defaults_to_washington_post_and_keeps_source_categories(explorer_app):
    response = explore(explorer_app, values("publisher", "exxonmobil"), "research-direction.value")
    assert response["research-anchor"]["value"] == "The Washington Post"
    figure = response["research-count-chart"]["figure"]
    assert set(figure["data"][0]["customdata"]) == {"exxonmobil", "bp", "api", "cera", UNKNOWN, "Williams", "Williams Companies"}
    assert sum(figure["data"][0]["x"]) == 7
    assert "CERAWeek" in json.dumps(response["research-count-table"])
    assert "7 native ad records across 7 source-listed sponsors" in response["research-count-scope"]["children"]
    assert "1 records have an unknown date" in response["research-count-scope"]["children"]


def test_full_current_filter_intersection_is_kept_for_counts_and_records(explorer_app):
    inputs = values("publisher", "The Washington Post", sponsors=["exxonmobil", "api"],
                    publishers=["The Washington Post"], keywords=["CCS"], platforms=["Website"], labels=["legacy"],
                    record_ids=["wp-0", "wp-2"], date_from="2024-01-01", date_to="2024-12-31",
                    include_unknown_dates=False)
    response = explore(explorer_app, inputs)
    assert response["research-count-chart"]["figure"]["data"][0]["customdata"] == ["api"]
    counts = explorer_app[2].statistics_calls[-1]
    records = explorer_app[2].page_calls[-1][0]
    assert counts == records
    assert counts.sponsors == ["exxonmobil", "api"]
    assert counts.publishers == ["The Washington Post"]
    assert counts.keywords == ["CCS"]
    assert counts.platforms == ["Website"]
    assert counts.labels == ["legacy"]
    assert counts.record_ids == ["wp-0", "wp-2"]
    assert not counts.include_unknown_dates
    assert counts.date_from.isoformat() == "2024-01-01"


def test_anchor_outside_current_filter_is_rejected_without_querying_counts(explorer_app):
    response = explore(explorer_app, values(anchor="exxonmobil", sponsors=["api"]), "research-anchor.value")
    assert response["research-anchor"]["value"] is None
    assert explorer_app[2].statistics_calls == []
    assert explorer_app[2].page_calls == []


def test_new_scope_replaces_incompatible_anchor_and_clears_record_selection(explorer_app):
    inputs = values(anchor="exxonmobil", sponsors=["api"])
    inputs["research-counterpart.value"] = "Outlet 00"
    inputs["research-record-offset.data"] = 20
    response = explore(explorer_app, inputs)
    assert response["research-anchor"]["value"] == "api"
    assert response["research-counterpart"]["value"] == ALL_RECORDS
    assert response["research-record-offset"]["data"] == 0
    assert explorer_app[2].page_calls[-1][0].sponsors == ["api"]


@pytest.mark.parametrize("changed", ["research-counterpart.value", "research-count-chart.clickData"])
def test_selecting_a_count_opens_only_its_source_records(explorer_app, changed):
    inputs = values(anchor="exxonmobil", keywords=["CCS"])
    inputs["research-counterpart.value"] = "The Washington Post"
    inputs["research-count-chart.clickData"] = {"points": [{"customdata": "The Washington Post"}]}
    response = explore(explorer_app, inputs, changed)
    filters = explorer_app[2].page_calls[-1][0]
    assert filters.sponsors == ["exxonmobil"]
    assert filters.publishers == ["The Washington Post"]
    assert filters.keywords == ["CCS"]
    anchors = [node["props"] for node in component_tree(response["research-records"]) if node.get("type") == "A"]
    assert any(anchor.get("href") == "/records/wp-0" for anchor in anchors)
    assert response["research-counterpart"]["value"] == "The Washington Post"
    assert response["research-next"]["disabled"] is True


def test_pagination_pages_all_matching_articles_under_anchor(explorer_app):
    inputs = values(anchor="exxonmobil", keywords=["CCS"])
    first = explore(explorer_app, inputs, "research-anchor.value")
    assert "15 matching records" in json.dumps(first["research-records"])
    following = explore(explorer_app, inputs, "research-next.n_clicks")
    assert following["research-record-offset"]["data"] == 10
    assert following["research-prev"]["disabled"] is False
    assert following["research-next"]["disabled"] is True
    assert len([node for node in component_tree(following["research-records"]) if node.get("type") == "Article"]) == 5
    inputs["research-record-offset.data"] = 10
    previous = explore(explorer_app, inputs, "research-prev.n_clicks")
    assert previous["research-record-offset"]["data"] == 0
    assert explorer_app[2].page_calls[-1][0].keywords == ["CCS"]


@pytest.mark.parametrize("clicked", [None, {"points": []}, {"points": [None]},
                                     {"points": [{"customdata": ["The Washington Post"]}]},
                                     {"points": [{"customdata": "FAKE"}]}])
def test_malformed_or_fabricated_click_cannot_widen_or_fabricate_counts(explorer_app, clicked):
    inputs = values(anchor="exxonmobil", publishers=["The Washington Post"])
    inputs["research-count-chart.clickData"] = clicked
    response = explore(explorer_app, inputs, "research-count-chart.clickData")
    assert response["research-counterpart"]["value"] == ALL_RECORDS
    assert explorer_app[2].page_calls[-1][0].publishers == ["The Washington Post"]
    assert explorer_app[2].page_calls[-1][0].sponsors == ["exxonmobil"]


@pytest.mark.parametrize("filters", [{"dataset": "social"}, {"date_from": "bad-date"}])
def test_invalid_scope_is_safe_without_data_queries(explorer_app, filters):
    response = explore(explorer_app, values(**filters))
    assert "unavailable" in response["research-count-scope"]["children"]
    assert explorer_app[2].statistics_calls == []
    assert explorer_app[2].page_calls == []


def test_unknown_source_category_can_be_explored_without_aliasing(explorer_app):
    response = explore(explorer_app, values(anchor=UNKNOWN), "research-anchor.value")
    assert response["research-count-chart"]["figure"]["data"][0]["customdata"] == ["The Washington Post"]
    assert explorer_app[2].page_calls[-1][0].sponsors == [UNKNOWN]


def test_empty_date_scope_is_explicit_and_does_not_broaden(explorer_app):
    response = explore(explorer_app, values(anchor="exxonmobil", date_from="2030-01-01", include_unknown_dates=False))
    assert "0 native ad records across 0 source-listed news outlets" in response["research-count-scope"]["children"]
    assert "No matching articles" in json.dumps(response["research-records"])
    assert response["research-next"]["disabled"] is True


def test_count_failure_is_sanitized(explorer_app):
    explorer_app[2].fail_counts = True
    response = explore(explorer_app, values(anchor="exxonmobil"))
    assert "Counts are temporarily unavailable" in response["research-count-scope"]["children"]
    assert SECRET not in json.dumps(response)


def test_record_failure_keeps_usable_complete_counts(explorer_app):
    explorer_app[2].fail_records = True
    response = explore(explorer_app, values(anchor="exxonmobil"))
    assert len(response["research-count-chart"]["figure"]["data"][0]["customdata"]) == 15
    assert "Article details are temporarily unavailable" in json.dumps(response["research-records"])
    assert SECRET not in json.dumps(response)
