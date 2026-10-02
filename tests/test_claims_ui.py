"""Published CLAIMS2 browsing uses current filters and original article evidence."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from dash import Dash, dcc, html
from flask import Flask
from test_app import SECRET, callback, component_tree

from observatory.app import _tools_card
from observatory.claims_ui import (
    claims_page_export,
    claims_panel,
    public_claims,
    register_claims_browser,
)
from observatory.record_view import register_record_page

BODY = "The company describes a proposed emissions project.\nAn independent researcher questions the projected savings."
QUOTE = BODY.split("\n")[0]
HASH = hashlib.sha256(BODY.encode()).hexdigest()


def assignment(identifier="native-00", nc="NC_1", sc="SC_1", review="human_supported"):
    return {
        "record_id": identifier, "version_id": "v-" + identifier, "body_hash": HASH,
        "dataset": "native", "title": "Article " + identifier, "publisher": "Outlet A",
        "sponsor": "exxonmobil", "date": "2024-01-01", "url": "https://example.test/source",
        "nc_id": nc, "sc_id": sc, "nc_definition": "A proposed emissions-reduction project is described.",
        "sc_definition": "An environmental benefit is claimed.", "review_state": review,
        "run_id": "saved-run-1", "taxonomy_version": "a" * 64, "review_version": "b" * 64,
        "start": 0, "end": len(QUOTE), "quote": QUOTE,
        "raw": {"secret": SECRET}, "reviewer": SECRET, "source_path": SECRET,
    }


class ClaimsService:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.available = True
        self.rows = [
            {"record_id": f"native-{i:02d}", "claims": [assignment(f"native-{i:02d}"),
                assignment(f"native-{i:02d}", "NC_2", None, "automatic_unverified")]}
            for i in range(13)
        ]

    def claims_matches(self, filters, *, nc_ids=None, sc_ids=None, taxonomy=None, review_state=None, offset=0, limit=20):
        self.calls.append((filters.model_copy(deep=True), {"nc_ids": nc_ids, "sc_ids": sc_ids,
                                                       "review_state": review_state, "offset": offset, "limit": limit}))
        if self.fail:
            raise RuntimeError(SECRET)
        rows = []
        for row in self.rows:
            if filters.record_ids and row["record_id"] not in filters.record_ids:
                continue
            claims = [item for item in row["claims"]
                      if (not nc_ids or item["nc_id"] in nc_ids)
                      and (not sc_ids or item["sc_id"] in sc_ids)
                      and (not review_state or item["review_state"] == review_state)]
            if claims:
                rows.append({"record_id": row["record_id"], "claims": claims})
        categories = {}
        for row in rows:
            for item in row["claims"]:
                group = categories.setdefault(item["nc_id"], {key: item[key] for key in (
                    "nc_id", "sc_id", "nc_definition", "sc_definition", "taxonomy_version")})
                group["record_count"] = group.get("record_count", 0) + 1
                group["assignment_count"] = group.get("assignment_count", 0) + 1
        scope = len(rows) if filters.record_ids else len(self.rows) + 7
        return {"available": self.available, "total_records": len(rows),
                "total_matches": sum(len(row["claims"]) for row in rows),
                "records": rows[offset:offset + limit], "claims_version": "c" * 64,
                "category_counts": list(categories.values()),
                "coverage_summary": {"scope_records": scope, "published_match_records": len(rows),
                                     "classification_completion_known": False},
                "coverage": SECRET, "error": SECRET}


@pytest.fixture
def claims_app():
    service = ClaimsService()
    app = Dash(__name__)
    app.layout = html.Div([dcc.Store(id="native-network-data"), dcc.Location(id="page-location"),
                          dcc.RadioItems(id="native-view", value="overview"),
                          dcc.RadioItems(id="active-dataset", value="native"), claims_panel()])
    register_claims_browser(app, service, links_enabled=True)
    return app, app.server.test_client(), service


def values(**filters):
    return {"native-network-data.data": {"filters": {"dataset": "native", **filters}},
            "page-location.pathname": "/data", "native-view.value": "overview", "active-dataset.value": "native",
            "claims2-category.value": "", "claims2-review.value": "all", "claims2-offset.data": 0,
            "claims2-selection.data": {"category": "", "review": "all"}}


def browse(application, inputs=None, changed="native-network-data.data"):
    return callback(application[0], application[1], "claims2-results.children", inputs or values(), changed)


def record_links(result):
    return {node["props"]["href"] for node in component_tree(result)
            if node.get("type") == "A" and node["props"].get("href", "").startswith("/records/")}


def test_published_browse_shows_articles_definitions_quotes_and_positive_only_coverage(claims_app):
    result = browse(claims_app)
    text = json.dumps(result)
    assert "13 matching records" in text and "26 assignments" in text
    assert "showing 1" in text
    assert record_links(result) == {f"/records/native-{i:02d}" for i in range(10)}
    for expected in [QUOTE, "NC_1", "SC_1", "saved-run-1", "a" * 64, "human-reviewed",
                     "Human-reviewed assignment", "Automated assignment", "No superclaim is mapped",
                     "not classified negatives", "not independent fact checks"]:
        assert expected.casefold() in text.casefold()
    assert result["claims2-controls"]["style"] == {}
    assert result["claims2-next"]["disabled"] is False
    assert SECRET not in text


@pytest.mark.parametrize("category,key", [("NC_2", "nc_ids"), ("SC_1", "sc_ids")])
def test_exact_category_filters_have_their_own_type(claims_app, category, key):
    inputs = values()
    inputs["claims2-category.value"] = category
    result = browse(claims_app, inputs, "claims2-apply.n_clicks")
    assert claims_app[2].calls[-1][1][key] == [category]
    assert "13 assignments" in json.dumps(result)
    assert result["claims2-selection"]["data"]["category"] == category


def test_review_filter_is_independent_of_historical_collection_label_filters(claims_app):
    inputs = values(publishers=["Outlet A"], sponsors=["exxonmobil"], labels=["green.historical"],
                    keywords=["CCS"], platforms=["Website"], date_from="2024-01-01",
                    date_to="2024-12-31", include_unknown_dates=False)
    inputs["claims2-review.value"] = "automatic_unverified"
    result = browse(claims_app, inputs, "claims2-apply.n_clicks")
    filters, options = claims_app[2].calls[-1]
    assert filters.model_dump(mode="json") == inputs["native-network-data.data"]["filters"] | {"record_ids": [], "date_presence": "any", "include_inferred_dates": False}
    assert options["review_state"] == "automatic_unverified"
    assert "13 assignments" in json.dumps(result)
    assert "Human-reviewed assignment" not in json.dumps(result)


@pytest.mark.parametrize("category", ["green.false_solutions", "NC_0", "NC_01", "SC_-1", "NC_1,SC_2", {"id": "NC_1"}])
def test_invalid_categories_cannot_be_used_as_claims_queries(claims_app, category):
    inputs = values()
    inputs["claims2-category.value"] = category
    result = browse(claims_app, inputs, "claims2-apply.n_clicks")
    if isinstance(category, dict):
        # Non-string client values are not silently treated as a requested ID.
        assert result["claims2-selection"]["data"] is None
    assert claims_app[2].calls == []
    assert record_links(result) == set()


@pytest.mark.parametrize("changed,value", [("page-location.pathname", "/query"), ("native-view.value", "relationships"),
                                           ("active-dataset.value", "social")])
def test_hidden_views_do_not_read_claims(claims_app, changed, value):
    inputs = values()
    inputs[changed] = value
    result = browse(claims_app, inputs, changed)
    assert claims_app[2].calls == []
    assert result["claims2-controls"]["style"] == {"display": "none"}


@pytest.mark.parametrize("available", [True, False])
def test_empty_or_unavailable_results_have_one_note_without_empty_controls(claims_app, available):
    claims_app[2].available = available
    claims_app[2].rows = []
    result = browse(claims_app)
    assert "not classified negatives" in result["claims2-status"]["children"]
    assert result["claims2-results"]["children"] == []
    assert result["claims2-controls"]["style"] == {"display": "none"}
    assert result["claims2-pager"]["style"] == {"display": "none"}
    assert SECRET not in json.dumps(result)


def test_filtered_zero_keeps_controls_so_users_can_clear_the_filter(claims_app):
    inputs = values()
    inputs["claims2-category.value"] = "NC_999"
    result = browse(claims_app, inputs, "claims2-apply.n_clicks")
    assert "No published CLAIMS2 assignments match" in result["claims2-status"]["children"]
    assert result["claims2-controls"]["style"] == {}
    cleared = browse(claims_app, inputs, "claims2-reset.n_clicks")
    assert cleared["claims2-category"]["value"] == ""
    assert "26 assignments" in json.dumps(cleared)


def test_pagination_reads_matching_records_without_duplicating_assignment_counts(claims_app):
    result = browse(claims_app, changed="claims2-next.n_clicks")
    assert result["claims2-offset"]["data"] == 10
    assert result["claims2-next"]["disabled"] is True
    assert record_links(result) == {f"/records/native-{i:02d}" for i in range(10, 13)}
    assert "26 assignments" in json.dumps(result)
    inputs = values()
    inputs["claims2-offset.data"] = 10
    assert browse(claims_app, inputs, "claims2-prev.n_clicks")["claims2-offset"]["data"] == 0
    inputs["claims2-offset.data"] = 10**12
    last = browse(claims_app, inputs, "claims2-next.n_clicks")
    assert last["claims2-offset"]["data"] == 10
    assert last["claims2-next"]["disabled"] is True


def test_changed_collection_scope_discards_old_category_and_pagination(claims_app):
    inputs = values(record_ids=["native-12"])
    inputs.update({"claims2-category.value": "NC_2", "claims2-review.value": "automatic_unverified",
                   "claims2-selection.data": {"category": "NC_2", "review": "automatic_unverified"},
                   "claims2-offset.data": 10})
    result = browse(claims_app, inputs)
    assert record_links(result) == {"/records/native-12"}
    assert "2 assignments" in json.dumps(result)
    assert result["claims2-selection"]["data"] == {"category": "", "review": "all"}
    assert result["claims2-offset"]["data"] == 0


def test_claims_errors_are_sanitized_without_removing_the_collection(claims_app):
    claims_app[2].fail = True
    result = browse(claims_app)
    assert "temporarily unavailable" in json.dumps(result)
    assert SECRET not in json.dumps(result)
    claims_app[2].fail = False
    assert record_links(browse(claims_app))


def test_public_projection_rejects_cross_record_claims_and_ignores_raw_fields():
    service = ClaimsService()
    from observatory.models import Filters

    result = service.claims_matches(Filters(), limit=1)
    assert SECRET not in json.dumps(public_claims(result))
    result["records"][0]["claims"][0]["record_id"] = "other-article"
    with pytest.raises(ValueError, match="ownership"):
        public_claims(result)


def test_matching_page_json_export_separates_page_evidence_from_complete_filtered_totals(claims_app):
    from observatory.models import Filters

    payload = claims_page_export(claims_app[2], Filters(publishers=["Outlet A"]),
                                 {"category": "", "review": "all"}, 10, links_enabled=True)
    assert payload["kind"] == "claims2_matching_page"
    assert payload["page"] == {"offset": 10, "limit": 10, "returned_records": 3, "returned_assignments": 6}
    assert payload["filtered_totals"] == {"records": 13, "assignments": 26}
    assert payload["coverage_summary"] == {"scope_records": 20, "published_match_records": 13,
                                            "classification_completion_known": False}
    assert {row["record_count"] for row in payload["category_counts"]} == {13}
    assert {row["record_id"] for row in payload["records"]} == {f"native-{i:02d}" for i in range(10, 13)}
    assert "one page" in payload["note"] and "whole filtered selection" in payload["note"]
    assert payload["filters"]["publishers"] == ["Outlet A"]
    assert payload["claims_version"] == "c" * 64
    assert SECRET not in json.dumps(payload)


def test_matching_page_export_applies_category_review_and_disables_source_urls(claims_app):
    from observatory.models import Filters

    selection = {"category": "NC_2", "review": "automatic_unverified"}
    payload = claims_page_export(claims_app[2], Filters(), selection, 0, links_enabled=False)
    assert payload["assignment_filters"] == selection
    assert payload["filtered_totals"] == {"records": 13, "assignments": 13}
    assert all(claim["nc_id"] == "NC_2" and claim["review_state"] == "automatic_unverified"
               for row in payload["records"] for claim in row["claims"])
    assert all(row["url"] == "" for row in payload["records"])
    assert "https://example.test/source" not in json.dumps(payload)


def test_http_page_export_rereads_service_instead_of_trusting_client_result(claims_app):
    inputs = values(record_ids=["native-12"])
    inputs["claims2-export.n_clicks"] = 1
    result = callback(claims_app[0], claims_app[1], "claims2-download.data", inputs, "claims2-export.n_clicks")
    downloaded = result["claims2-download"]["data"]
    assert downloaded["filename"] == "claims2-matching-page.json"
    payload = json.loads(downloaded["content"])
    assert payload["filtered_totals"]["records"] == 1
    assert payload["records"][0]["record_id"] == "native-12"
    assert claims_app[2].calls[-1][0].record_ids == ["native-12"]
    assert "file contains this page of evidence only" in result["claims2-export-status"]["children"]


def test_filter_change_clears_export_notice_without_querying_or_downloading(claims_app):
    result = callback(claims_app[0], claims_app[1], "claims2-download.data", values(), "native-network-data.data")
    assert "claims2-download" not in result
    assert result["claims2-export-status"]["children"] == ""
    assert claims_app[2].calls == []


@pytest.mark.parametrize("mode", ["error", "pending", "invalid_selection"])
def test_export_failures_are_sanitized_without_any_download(claims_app, mode):
    inputs = values()
    if mode == "error":
        claims_app[2].fail = True
    elif mode == "pending":
        claims_app[2].available = False
    else:
        inputs["claims2-selection.data"] = {"records": [{"secret": SECRET}]}
    result = callback(claims_app[0], claims_app[1], "claims2-download.data", inputs, "claims2-export.n_clicks")
    assert "claims2-download" not in result
    assert "unavailable for export" in result["claims2-export-status"]["children"]
    assert SECRET not in json.dumps(result)


def tool_card_text(data, links=True):
    card = _tools_card({"structured_result": {"kind": "claims", **data},
                        "answer": "Stored assignments are shown below."}, links)
    return json.dumps(card, default=lambda item: item.to_plotly_json())


def test_query_tools_route_renders_published_definitions_and_quotes_without_raw_payloads():
    from observatory.models import Filters

    data = ClaimsService().claims_matches(Filters(), limit=1)
    text = tool_card_text(data)
    for expected in ["Published CLAIMS2 assignments", QUOTE, "NC_1", "SC_1", "saved-run-1",
                     "Human-reviewed assignment", "not classified negatives", "/records/native-00"]:
        assert expected in text
    assert SECRET not in text
    assert "https://example.test/source" not in tool_card_text(data, links=False)


def test_query_tool_empty_results_are_not_greenwashing_negatives():
    text = tool_card_text({"available": True, "total_records": 0, "total_matches": 0, "records": []})
    assert "No published matching assignments" in text and "not classified negatives" in text
    assert "Assignment provenance" not in text


def test_query_tool_malformed_evidence_is_suppressed():
    from observatory.models import Filters

    data = ClaimsService().claims_matches(Filters(), limit=1)
    data["records"][0]["claims"][0]["record_id"] = "another-record"
    text = tool_card_text(data)
    assert "results are unavailable" in text
    assert "Assignment provenance" not in text and SECRET not in text


@pytest.fixture
def record_app():
    service = ClaimsService()
    source = {"record_id": "native-00", "version_id": "v-native-00", "dataset": "native", "body": BODY,
              "body_hash": HASH, "title": "<script>alert('unsafe title')</script>", "publisher": "Outlet A",
              "sponsor": "exxonmobil", "date": "2024-01-01", "keyword": "CCS", "url": "https://example.test/source",
              "archive_status": "No verified archived copy linked", "archive_note": "", "attachments": [],
              "body_label": "Stored article text", "body_note": "Original extraction.", "quality_notes": [],
              "body_characters": len(BODY), "retrievable": True}
    details = SimpleNamespace(get=lambda identifier: source if identifier == source["record_id"] else None)
    app = Flask(__name__)
    register_record_page(app, details, service=service)
    return app.test_client(), service, source


def test_record_page_binds_each_quote_to_current_body_and_shows_run_definitions(record_app):
    client, service, source = record_app
    response = client.get("/records/native-00")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    for expected in ["CLAIMS2 evidence", QUOTE, "NC_1", "SC_1", "saved-run-1", "Assignment provenance",
                     "Human-reviewed assignment", "Automated assignment", "No superclaim is mapped", "not classified negatives"]:
        assert expected in text
    assert "<script>alert" not in text and "&lt;script&gt;" in text
    assert SECRET not in text
    assert service.calls[-1][0].record_ids == [source["record_id"]]
    assert service.calls[-1][1]["limit"] == 1


@pytest.mark.parametrize("change", ["version_id", "body_hash", "quote", "dataset", "start"])
def test_stale_or_wrong_record_claims_are_not_shown_beside_article(record_app, change):
    client, service, _source = record_app
    item = service.rows[0]["claims"][0]
    item[change] = 1 if change == "start" else "different"
    text = client.get("/records/native-00").get_data(as_text=True)
    assert "could not be bound to this article version" in text
    assert "Assignment provenance" not in text
    assert "Stored article text" in text and BODY in text


@pytest.mark.parametrize("mode", ["empty", "schema_pending", "error"])
def test_record_details_remain_readable_when_claims_are_pending_or_fail(record_app, mode):
    client, service, _source = record_app
    if mode == "error":
        service.fail = True
    elif mode == "empty":
        service.rows = []
    else:
        service.available = False
    text = client.get("/records/native-00").get_data(as_text=True)
    assert "CLAIMS2 evidence" in text and "Stored article text" in text
    assert "Assignment provenance" not in text
    assert SECRET not in text
    assert client.get("/records/missing").status_code == 404
