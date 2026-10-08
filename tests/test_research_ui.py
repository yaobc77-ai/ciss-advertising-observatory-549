"""Research-facing UI contracts, exercised through Dash/Flask without APIs."""

import csv
import json
from datetime import date
from io import StringIO
from types import SimpleNamespace

import pytest
from test_app import (
    FakeService,
    callback,
    component_tree,
    defaults,
    research_values,
)

from observatory.app import create_app


@pytest.fixture
def research_ui():
    service = FakeService()
    settings = SimpleNamespace(
        show_source_links=True, cookie_secret="research-test", monthly_budget_usd=100
    )
    app = create_app(service, settings)
    return app, app.server.test_client(), service


def update_collection(application, values=None, dataset="native"):
    app, client, _ = application
    return callback(
        app,
        client,
        f"{dataset}-grid.rowData",
        defaults(dataset) | (values or {}),
        f"{dataset}-sponsors.value",
    )


def test_full_numeric_heatmap_and_cross_tab_do_not_drop_ninth_sponsor(research_ui):
    _, _, service = research_ui
    template = service.rows[0]
    sponsors = ["exxonmobil", "cera", *[f"sponsor-{i}" for i in range(9)]]
    service.rows = [
        dict(
            template,
            record_id=f"record-{i}",
            version_id=f"version-{i}",
            sponsor=sponsor,
            publisher=f"Outlet {i % 2}",
        )
        for i, sponsor in enumerate(sponsors)
    ]
    result = update_collection(research_ui)
    figure = result["native-relationships-chart"]["figure"]
    heatmap = figure["data"][0]
    assert len(heatmap["y"]) == 11
    assert set(heatmap["y"]) == set(sponsors)
    # Click coordinates retain source values; readable aliases are labels only.
    labels = dict(zip(figure["layout"]["yaxis"]["tickvals"], figure["layout"]["yaxis"]["ticktext"], strict=True))
    assert labels["exxonmobil"] == "ExxonMobil"
    assert labels["cera"] == "CERAWeek"
    assert heatmap["text"][heatmap["y"].index("cera")] == ["CERAWeek"] * len(heatmap["x"])
    for row, sponsor in zip(heatmap["customdata"], heatmap["y"], strict=True):
        assert row == [{"kind": "edge", "sponsor": sponsor, "publisher": outlet} for outlet in heatmap["x"]]
    assert heatmap["texttemplate"] == "%{z}"
    assert heatmap["showscale"]
    assert heatmap["colorbar"]["title"]["text"] == "Records"
    assert sum(map(sum, heatmap["z"])) == 11
    assert any(0 in row for row in heatmap["z"])
    table = result["native-matrix"]["rowData"]
    assert len(table) == 12  # all sponsors plus the totals row
    assert table[-1]["is_total"] and table[-1]["total"] == 11
    cera = next(row for row in table if row["sponsor"] == "cera")
    assert cera["sponsor_display"] == "CERAWeek"
    assert cera["entity_type"] == "conference/event"
    assert "client confirmation" in cera["note"]
    assert sum(row["total"] for row in table[:-1]) == table[-1]["total"]


def test_cross_tab_download_uses_same_filtered_rows_as_table(research_ui):
    app, client, service = research_ui
    service.rows[0]["sponsor"] = "exxonmobil"
    selected = {"native-sponsors.value": ["exxonmobil"]}
    viewed = update_collection(research_ui, selected)
    downloaded = callback(
        app,
        client,
        "native-matrix-download.data",
        defaults("native") | selected | {"native-matrix-export.n_clicks": 1},
        "native-matrix-export.n_clicks",
    )
    payload = downloaded["native-matrix-download"]["data"]
    csv_rows = list(csv.DictReader(StringIO(payload["content"])))
    assert payload["filename"] == "native-sponsor-outlet-cross-tab.csv"
    assert len(csv_rows) == 2
    assert csv_rows[0]["Sponsor / organization"] == "ExxonMobil"
    assert csv_rows[0]["Source sponsor value"] == "exxonmobil"
    assert csv_rows[0]["Outlet A"] == "1"
    assert int(csv_rows[-1]["Total"]) == viewed["native-matrix"]["rowData"][-1]["total"]
    assert service.browse_calls[-1].sponsors == ["exxonmobil"]


def test_timeline_and_label_coverage_show_unknown_records(research_ui):
    result = update_collection(research_ui)
    timeline = result["native-timeline-chart"]["figure"]["data"][0]
    assert timeline["type"] == "bar"
    assert dict(zip(timeline["x"], timeline["y"], strict=True)) == {
        "2024": 1,
        "Unknown": 1,
    }
    assert timeline["text"] == ["1", "1"]
    label_chart = result["native-labels-chart"]["figure"]["data"][0]
    assert label_chart["x"] == [1]
    assert "Historical solution" in label_chart["y"]
    assert (
        "1 of 2 selected records have no historical label"
        in result["native-labels-chart-note"]["children"]
    )


def test_toolbox_groups_secondary_controls_outside_main_navigation(research_ui):
    _, client, _ = research_ui
    components = list(component_tree(client.get("/_dash-layout").json))
    nav = next(item for item in components if item["type"] == "Nav")
    nav_links = [item for item in component_tree(nav) if item["type"] == "Link"]
    assert [item["props"]["children"] for item in nav_links] == ["Query", "Data"]
    toolbox = next(item for item in components if item["props"].get("id") == "toolbox")
    assert toolbox["type"] == "Details"
    assert not toolbox["props"].get("open", False)
    tool_nodes = list(component_tree(toolbox))
    tool_ids = {item["props"].get("id") for item in tool_nodes}
    assert {
        "search-free",
        "search-scope",
        "open-collection-filters",
        "query-cost",
        "nav-wireframe",
    } <= tool_ids
    assert "answer-paid" not in tool_ids
    assert "shared-filters" not in tool_ids
    filters_link = next(item for item in tool_nodes if item["props"].get("id") == "open-collection-filters")
    assert filters_link["props"]["href"] == "#collection-filters"
    collection_filters = next(item for item in components if item["props"].get("id") == "collection-filters")
    assert collection_filters["type"] == "Details"
    filter_ids = {item["props"].get("id") for item in component_tree(collection_filters)}
    assert {"shared-filters", "native-filter-panel", "social-filter-panel"} <= filter_ids
    tool_links = [item for item in tool_nodes if item["type"] == "Link"]
    assert any(item["props"].get("href") == "/wireframe" for item in tool_links)
    assert all(item["props"].get("href") != "/wireframe" for item in nav_links)


def test_empty_social_collection_hides_empty_content_and_filters(research_ui):
    app, client, _ = research_ui
    result = update_collection(research_ui, dataset="social")
    assert result["social-content"]["style"] == {"display": "none"}
    assert "No admitted company social posts" in json.dumps(
        result["social-status"]["children"]
    )
    selected = callback(
        app,
        client,
        "social-panel.style",
        {"active-dataset.value": "social"},
        "active-dataset.value",
    )
    assert selected["social-filter-panel"]["style"] == {"display": "none"}
    assert selected["shared-filters"]["style"] == {"display": "none"}


def test_evidence_identifiers_are_collapsed_but_date_and_terms_remain_visible(
    research_ui,
):
    app, client, service = research_ui
    evidence = service.evidence().model_copy(
        update={
            "evidence_id": "e" * 64,
            "record_id": "r" * 64,
            "version_id": "v" * 64,
            "published_at": date(2024, 4, 3),
            "matched_terms": ["carbon", "capture"],
            "score": 0.125,
        }
    )
    service.evidence = lambda: evidence
    response = callback(
        app,
        client,
        "research-results.children",
        research_values(),
        "search-free.n_clicks",
    )
    nodes = list(component_tree(response["research-results"]["children"]))
    card = next(node for node in nodes if node["type"] == "Article")
    details = next(
        node
        for node in component_tree(card)
        if node["type"] == "Details" and "Technical details" in json.dumps(node)
    )
    assert not details["props"].get("open", False)
    for token in ("e" * 64, "r" * 64, "v" * 64):
        assert token in json.dumps(details)
    public_parts = [
        item
        for item in card["props"]["children"]
        if item is not details and item.get("type") != "A"
    ]
    public_text = json.dumps(public_parts)
    assert "e" * 64 not in public_text and "v" * 64 not in public_text
    assert "2024-04-03" in public_text
    assert "Matched search terms: carbon, capture" in public_text


def test_record_titles_link_details_and_advanced_filters_explain_search_terms(research_ui):
    _, client, _ = research_ui
    layout = client.get("/_dash-layout").json
    components = {
        item["props"]["id"]: item["props"]
        for item in component_tree(layout)
        if "id" in item["props"]
    }
    columns = {item["field"]: item for item in components["native-grid"]["columnDefs"]}
    assert columns["title"]["cellRenderer"] == "RecordTitle"
    assert columns["title"]["headerName"] == "Article"
    assert "record_id" not in columns and "keyword" not in columns
    assert columns["archive_status"]["cellRenderer"] == "ArchiveLink"
    assert "company, trade group or event" in columns["sponsor"]["headerTooltip"]
    assert "CERAWeek is an event" in json.dumps(components["native-overview"])
    advanced = next(
        item for item in component_tree(components["native-filter-panel"])
        if item["type"] == "Details" and item["props"].get("className") == "advanced-filters"
    )
    assert not advanced["props"].get("open", False)
    assert any(item["props"].get("id") == "native-keywords" for item in component_tree(advanced))
    term_label = next(item for item in component_tree(advanced) if item["props"].get("htmlFor") == "native-keywords")
    assert term_label["props"]["children"] == "Collection search term"
    assert "they are not sponsor identities or article themes" in json.dumps(advanced)
    # The title renderer and record detail still receive identity and source terms.
    rows = update_collection(research_ui)["native-grid"]["rowData"]
    assert {row["record_id"]: row["keyword"] for row in rows} == {"native-a": "CCS", "native-b": "gas"}


def test_record_detail_page_renders_text_pdf_and_escaped_source_content():
    record = {
        "record_id": "public-record",
        "version_id": "version-one",
        "title": "<script>alert('source')</script>",
        "sponsor": "cera",
        "publisher": "Example outlet",
        "date": "2022-01-02",
        "keyword": "carbon",
        "body": "Captured source <script>alert('body')</script>",
        "body_label": "Stored source text",
        "body_note": "Unchanged captured source text.",
        "body_hash": "a" * 64,
        "body_characters": 51,
        "retrievable": True,
        "archive_status": "Local PDF snapshot available",
        "archive_note": "Reviewed local source.",
        "url": "https://example.test/article",
        "archive_url": "",
        "attachments": [
            {
                "label": "Reviewed PDF",
                "url": "/records/public-record/attachments/one",
                "download_url": "/records/public-record/attachments/one?download=1",
                "identity_note": "Verified source identity",
                "limitations": "Snapshot only",
                "sha256": "b" * 64,
            }
        ],
    }
    details = SimpleNamespace(
        get=lambda value: record if value == "public-record" else None
    )
    app = create_app(
        FakeService(),
        SimpleNamespace(
            show_source_links=True, cookie_secret="test", monthly_budget_usd=100
        ),
        record_details=details,
    )
    client = app.server.test_client()
    response = client.get("/records/public-record")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "CERAWeek" in text and "conference/event" in text
    assert "Collection search term:" in text
    assert "Stored source text" in text and "Captured source &lt;script&gt;" in text
    assert "<script>alert" not in text
    assert '<iframe class="snapshot-preview"' in text
    assert 'src="/records/public-record/attachments/one"' in text
    assert "<details><summary>Technical details</summary>" in text
    assert client.get("/records/missing").status_code == 404
