"""A changed selection must replace its relationship and exact record membership.

Exercise Dash's real callback endpoint, carrying the previous response back as
browser state. Counts alone would miss a stale article anchor or pie bucket.
"""

import csv
import json
import re
from io import StringIO
from urllib.parse import unquote

import pytest
from test_app import callback, component_tree
from test_collection_graph_ui import explore, values
from test_collection_graph_ui import map_app as map_app

from observatory.collection_graph_breakdown import selection_breakdown
from observatory.collection_graph_ui import ALL_BUCKETS
from observatory.knowledge_map import build_collection_map


def _text(value):
    """Read visible component children, excluding audit props and attributes."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    if isinstance(value, dict):
        return _text(value.get("props", {}).get("children", ""))
    return str(value) if value is not None else ""


def _node(graph, kind, source_value):
    return next(item for item in graph["nodes"]
                if item["type"] == kind
                and item["properties"]["source_value"] == source_value)


def _selection(item, kind="node"):
    return {"kind": kind, "id": item["id"]}


def _summary_edge(graph, sponsor, outlet):
    return next(item for item in graph["summary_edges"]
                if item["source"] == sponsor["id"] and item["target"] == outlet["id"])


def _expected_records(graph, *, sponsor=None, outlet=None):
    return {row["record_id"] for row in graph["records"]
            if (sponsor is None or row["sponsor"] == sponsor)
            and (outlet is None or row["publisher"] == outlet)}


def _advance(map_app, browser, changed, value):
    browser[changed] = value
    response = explore(map_app, browser, changed)
    for component, props in response.items():
        for prop, result in props.items():
            browser[f"{component}.{prop}"] = result
    return response


def _choose(map_app, browser, item, kind="node", method="tap"):
    if method == "keyboard":
        return _advance(map_app, browser, "collection-graph-find.value",
                        json.dumps(_selection(item, kind)))
    prop = "tapNodeData" if kind == "node" else "tapEdgeData"
    # Labels and counts are deliberately untrusted; only the server ID applies.
    return _advance(map_app, browser, f"collection-graph-canvas.{prop}",
                    {"id": item["id"], "label": "stale client label", "record_count": 9999})


def _bucket(graph, selected, label):
    return next(item for item in selection_breakdown(graph, selected)["categories"]
                if item["label"] == label)


def _article_record_ids(graph, response):
    elements = response["collection-graph-canvas"]["elements"]
    article_nodes = {item["data"]["id"] for item in elements
                     if item["data"].get("type") == "Article"}
    record_ids = {row["record_id"] for row in graph["records"]
                  if row["article_node_id"] in article_nodes}
    expected_edges = {edge["id"] for edge in graph["article_edges"]
                      if edge["source"] in article_nodes}
    actual_edges = {item["data"]["id"] for item in elements
                    if item["data"].get("kind") == "edge"}
    assert actual_edges == expected_edges
    assert all(item["data"]["record_count"] == 1 for item in elements
               if item["data"].get("kind") == "edge")
    return record_ids


def _page_record_ids(response):
    return {unquote(item["props"]["href"].removeprefix("/records/"))
            for item in component_tree(response["collection-graph-inspector"])
            if item.get("type") == "A"
            and item["props"].get("href", "").startswith("/records/")}


def _all_page_record_ids(map_app, browser, response):
    result = _page_record_ids(response)
    for _ in range(4):
        if response["collection-graph-next"]["disabled"]:
            return result
        response = _advance(map_app, browser, "collection-graph-next.n_clicks",
                            (browser.get("collection-graph-next.n_clicks") or 0) + 1)
        page = _page_record_ids(response)
        assert page and not result.intersection(page)
        result.update(page)
    pytest.fail("The fixture selection did not finish its record pages")


def _total_text(response):
    totals = [item for item in component_tree(response["collection-graph-selection-heading"])
              if item["props"].get("className") == "collection-graph-total"]
    assert len(totals) == 1
    return _text(totals[0])


def _assert_total(response, count):
    assert re.search(rf"\b{count}\s+(?:supporting|matching)\s+records?\b", _total_text(response))
    assert "stale client label" not in _text(response["collection-graph-selection-heading"]["children"])


def _relation_rows(response, predicate):
    relations = response["collection-graph-relations"]
    return [item for item in component_tree(relations)
            if item["props"].get("data-predicate") == predicate]


def test_article_click_retargets_anchor_and_includes_outlet_records_outside_old_company(map_app):
    graph = map_app[2].graph
    sponsor = _node(graph, "SponsorCandidate", "exxonmobil")
    outlet = _node(graph, "Outlet", "The Washington Post")
    browser = values(**{"collection-graph-view.value": "articles"})
    company = _choose(map_app, browser, sponsor)
    assert _article_record_ids(graph, company) == _expected_records(graph, sponsor="exxonmobil")

    switched = _choose(map_app, browser, outlet)
    assert switched["collection-graph-selection"]["data"] == _selection(outlet)
    assert switched["collection-graph-anchor"]["data"] == _selection(outlet)
    assert switched["collection-graph-bucket"]["value"] == ALL_BUCKETS
    expected = _expected_records(graph, outlet="The Washington Post")
    assert len(expected) == 12
    assert _article_record_ids(graph, switched) == expected
    _assert_total(switched, 12)
    assert _all_page_record_ids(map_app, browser, switched) == expected


def test_reclicking_company_after_outlet_bucket_restores_all_company_articles(map_app):
    graph = map_app[2].graph
    sponsor = _node(graph, "SponsorCandidate", "exxonmobil")
    browser = values(**{"collection-graph-view.value": "articles"})
    _choose(map_app, browser, sponsor)
    category = _bucket(graph, _selection(sponsor), "The Washington Post")
    narrowed = _advance(map_app, browser, "collection-graph-bucket.value", category["id"])
    assert _article_record_ids(graph, narrowed) == _expected_records(
        graph, sponsor="exxonmobil", outlet="The Washington Post")

    restored = _choose(map_app, browser, sponsor)
    assert restored["collection-graph-bucket"]["value"] == ALL_BUCKETS
    assert restored["collection-graph-bucket-selection"]["data"] == ALL_BUCKETS
    assert restored["collection-graph-anchor"]["data"] == _selection(sponsor)
    expected = _expected_records(graph, sponsor="exxonmobil")
    assert len(expected) == 20
    assert _article_record_ids(graph, restored) == expected
    _assert_total(restored, 20)
    assert _all_page_record_ids(map_app, browser, restored) == expected


@pytest.mark.parametrize("trigger", ["donut", "list"])
def test_entity_bucket_names_both_endpoints_and_separates_selected_count_from_parent_pie(map_app, trigger):
    graph = map_app[2].graph
    sponsor = _node(graph, "SponsorCandidate", "exxonmobil")
    browser = values()
    _choose(map_app, browser, sponsor)
    category = _bucket(graph, _selection(sponsor), "The Washington Post")
    if trigger == "donut":
        response = _advance(map_app, browser, "collection-graph-breakdown.clickData",
                            {"points": [{"customdata": category["id"], "value": 9999}]})
    else:
        response = _advance(map_app, browser, "collection-graph-bucket.value", category["id"])

    title = next(item for item in component_tree(response["collection-graph-selection-heading"])
                 if item.get("type") == "H3")
    assert "ExxonMobil" in _text(title) and "The Washington Post" in _text(title)
    _assert_total(response, 10)
    assert sum(response["collection-graph-breakdown"]["figure"]["data"][0]["values"]) == 20
    note = _text(response["collection-graph-breakdown-note"]["children"]).lower()
    assert "20" in note and any(term in note for term in ("chart", "ring", "distribution"))
    rows = _relation_rows(response, "derived_source_association")
    assert rows
    visible = _text(rows)
    assert "Co-listed in source records" in visible
    assert "ExxonMobil" in visible and "The Washington Post" in visible
    assert all(re.search(rf"\b{count}\b", visible) for count in (10, 20, 12))
    expected = _expected_records(graph, sponsor="exxonmobil", outlet="The Washington Post")
    assert _all_page_record_ids(map_app, browser, response) == expected


@pytest.mark.parametrize("target", ["article", "source_lists_sponsor", "published_in"])
def test_article_and_source_field_edges_show_named_original_relationships_and_single_record(map_app, target):
    graph = map_app[2].graph
    article = next(item for item in graph["nodes"]
                   if item["type"] == "Article" and item["properties"]["record_id"] == "record-0")
    browser = values(**{"collection-graph-view.value": "articles"})
    if target == "article":
        response = _choose(map_app, browser, article)
    else:
        edge = next(item for item in graph["article_edges"]
                    if item["source"] == article["id"] and item["predicate"] == target)
        response = _choose(map_app, browser, edge, "edge")

    _assert_total(response, 1)
    assert _page_record_ids(response) == {"record-0"}
    assert response["collection-graph-next"]["disabled"] is True
    for predicate, label, source_name in [
        ("source_lists_sponsor", "Source lists sponsor", "ExxonMobil"),
        ("published_in", "Published in", "The Washington Post"),
    ]:
        rows = _relation_rows(response, predicate)
        assert rows
        visible = _text(rows)
        assert label in visible and source_name in visible
        assert re.search(r"\b1\s+(?:supporting\s+)?records?\b", visible)


@pytest.mark.parametrize("view", ["entities", "articles"])
@pytest.mark.parametrize("method", ["tap", "keyboard"])
def test_continuous_outlet_company_and_relation_selection_never_keeps_previous_bucket_records(map_app, view, method):
    graph = map_app[2].graph
    sponsor = _node(graph, "SponsorCandidate", "exxonmobil")
    api = _node(graph, "SponsorCandidate", "api")
    outlet = _node(graph, "Outlet", "The Washington Post")
    edge = _summary_edge(graph, sponsor, outlet)
    browser = values(**{"collection-graph-view.value": view})
    _choose(map_app, browser, sponsor, method=method)
    category = _bucket(graph, _selection(sponsor), "The Washington Post")
    _advance(map_app, browser, "collection-graph-bucket.value", category["id"])

    changes = [
        (outlet, "node", _expected_records(graph, outlet="The Washington Post")),
        (api, "node", _expected_records(graph, sponsor="api")),
        (edge, "edge", _expected_records(graph, sponsor="exxonmobil", outlet="The Washington Post")),
        (sponsor, "node", _expected_records(graph, sponsor="exxonmobil")),
    ]
    for item, kind, expected in changes:
        response = _choose(map_app, browser, item, kind, method)
        assert response["collection-graph-selection"]["data"] == _selection(item, kind)
        assert response["collection-graph-bucket"]["value"] == ALL_BUCKETS
        assert response["collection-graph-bucket-selection"]["data"] == ALL_BUCKETS
        assert response["collection-graph-offset"]["data"] == 0
        _assert_total(response, len(expected))
        if view == "articles":
            assert response["collection-graph-anchor"]["data"] == _selection(item, kind)
            assert _article_record_ids(graph, response) == expected
        assert _all_page_record_ids(map_app, browser, response) == expected

        # Narrow each new parent before the next change, using a category that
        # cannot legitimately persist as the next selection's membership.
        detail = selection_breakdown(graph, _selection(item, kind))
        category = min(detail["categories"], key=lambda row: row["count"])
        narrowed = _advance(map_app, browser, "collection-graph-bucket.value", category["id"])
        assert _page_record_ids(narrowed).issubset(set(category["record_ids"]))


def _export_counts(map_app, browser, changed="collection-graph-counts-export.n_clicks"):
    app, client, _ = map_app
    browser["collection-graph-counts-export.n_clicks"] = 1
    response = callback(app, client, "collection-graph-counts-download.data", browser, changed)
    for component, props in response.items():
        for prop, result in props.items():
            browser[f"{component}.{prop}"] = result
    return response


def test_bucket_csv_exports_selected_relationship_only_with_parent_denominator(map_app):
    graph = map_app[2].graph
    sponsor = _node(graph, "SponsorCandidate", "exxonmobil")
    browser = values()
    _choose(map_app, browser, sponsor)
    category = _bucket(graph, _selection(sponsor), "The Washington Post")
    _advance(map_app, browser, "collection-graph-bucket.value", category["id"])
    exported = _export_counts(map_app, browser)
    content = exported["collection-graph-counts-download"]["data"]["content"]
    rows = list(csv.DictReader(StringIO(content.lstrip("\ufeff"))))
    assert len(rows) == 1
    row = rows[0]
    assert "ExxonMobil" in row["Selection"] and "The Washington Post" in row["Selection"]
    assert row["Source category"] == row["Exact source value"] == "The Washington Post"
    assert row["Records"] == "10" and row["Denominator"] == "20"
    assert float(row["Share"]) == .5
    status = exported["collection-graph-counts-status"]["children"]
    assert "1 categories for 10 supporting records" in status
    assert "parent denominator of 20" in status


@pytest.mark.parametrize("change", ["selection", "bucket"])
def test_new_selection_or_bucket_clears_previous_download_status_without_exporting_again(map_app, change):
    graph = map_app[2].graph
    sponsor = _node(graph, "SponsorCandidate", "exxonmobil")
    browser = values()
    _choose(map_app, browser, sponsor)
    exported = _export_counts(map_app, browser)
    assert exported["collection-graph-counts-status"]["children"]
    if change == "selection":
        _choose(map_app, browser, _node(graph, "Outlet", "The Washington Post"))
    else:
        category = _bucket(graph, _selection(sponsor), "The Washington Post")
        _advance(map_app, browser, "collection-graph-bucket.value", category["id"])
    calls = len(map_app[2].calls)
    changed = "collection-graph-selection.data" if change == "selection" else "collection-graph-bucket-selection.data"
    cleared = _export_counts(map_app, browser, changed)
    assert cleared["collection-graph-counts-status"]["children"] == ""
    assert "collection-graph-counts-download" not in cleared
    assert len(map_app[2].calls) == calls


def test_missing_sponsor_bucket_never_highlights_outlets_other_known_sponsor_relationships(map_app):
    graph = build_collection_map([
        {"dataset": "native", "record_id": "known", "version_id": "v-known",
         "sponsor": "api", "publisher": "Outlet A"},
        {"dataset": "native", "record_id": "missing", "version_id": "v-missing",
         "sponsor": "", "publisher": "Outlet A"},
    ])
    map_app[2].graph = graph
    outlet = _node(graph, "Outlet", "Outlet A")
    browser = values()
    _choose(map_app, browser, outlet)
    category = next(item for item in selection_breakdown(graph, _selection(outlet))["categories"]
                    if item["missing"])
    response = _advance(map_app, browser, "collection-graph-bucket.value", category["id"])
    assert _page_record_ids(response) == {"missing"}
    assert not _relation_rows(response, "derived_source_association")
    visible = _text(response["collection-graph-relations"]["children"]).lower()
    assert "no source-listed sponsor" in visible and "no relationship is asserted" in visible
    highlighted = {match.group(1) for rule in response["collection-graph-canvas"]["stylesheet"]
                   if rule["style"].get("opacity") == .95
                   and (match := re.fullmatch(r'edge\[id = "([^"]+)"\]', rule["selector"]))}
    assert not highlighted.intersection(item["id"] for item in graph["summary_edges"])


@pytest.mark.parametrize("missing_field", ["sponsor", "publisher"])
def test_article_with_missing_source_field_reports_absence_without_inventing_unknown_entity(map_app, missing_field):
    row = {"dataset": "native", "record_id": "missing", "version_id": "v-missing",
           "sponsor": "api", "publisher": "Outlet A"}
    row[missing_field] = ""
    graph = build_collection_map([row])
    map_app[2].graph = graph
    article = next(item for item in graph["nodes"] if item["type"] == "Article")
    browser = values(**{"collection-graph-view.value": "articles"})
    response = _choose(map_app, browser, article)
    absent_predicate = "source_lists_sponsor" if missing_field == "sponsor" else "published_in"
    present_predicate = "published_in" if missing_field == "sponsor" else "source_lists_sponsor"
    assert not _relation_rows(response, absent_predicate)
    assert len(_relation_rows(response, present_predicate)) == 1
    field_label = "sponsor" if missing_field == "sponsor" else "outlet"
    visible = _text(response["collection-graph-relations"]["children"]).lower()
    assert f"no source-listed {field_label}" in visible and "no relationship is asserted" in visible
    absent_kind = "SponsorCandidate" if missing_field == "sponsor" else "Outlet"
    assert all(item["data"].get("type") != absent_kind
               for item in response["collection-graph-canvas"]["elements"])
    assert _page_record_ids(response) == {"missing"}


@pytest.mark.parametrize("year,count", [("2021", 9), ("Unknown date", 1)])
def test_date_bucket_named_relationship_uses_selected_records_instead_of_full_edge_count(map_app, year, count):
    graph = map_app[2].graph
    sponsor = _node(graph, "SponsorCandidate", "exxonmobil")
    outlet = _node(graph, "Outlet", "The Washington Post")
    edge = _summary_edge(graph, sponsor, outlet)
    browser = values(**{"collection-graph-view.value": "articles"})
    _choose(map_app, browser, edge, "edge")
    category = _bucket(graph, _selection(edge, "edge"), year)
    response = _advance(map_app, browser, "collection-graph-bucket.value", category["id"])
    _assert_total(response, count)
    rows = _relation_rows(response, "derived_source_association")
    assert len(rows) == 1
    totals = [item for item in component_tree(rows)
              if item.get("type") == "P" and "supporting records" in _text(item)]
    assert len(totals) == 1 and _text(totals[0]) == f"{count} supporting records"
    visible = _text(rows)
    assert f"{count} of 20 records" in visible and f"{count} of 12 records" in visible
    assert sum(response["collection-graph-breakdown"]["figure"]["data"][0]["values"]) == 10
    expected = _expected_records(graph, sponsor="exxonmobil", outlet="The Washington Post")
    expected = {rid for rid in expected if (rid == "record-0") == (year == "Unknown date")}
    assert _article_record_ids(graph, response) == expected
    assert _all_page_record_ids(map_app, browser, response) == expected


@pytest.mark.parametrize("target", ["article", "source_lists_sponsor", "published_in"])
def test_article_selection_survives_entities_view_and_highlights_only_its_witnessed_pair(map_app, target):
    graph = map_app[2].graph
    article = next(item for item in graph["nodes"]
                   if item["type"] == "Article" and item["properties"]["record_id"] == "record-0")
    sponsor = _node(graph, "SponsorCandidate", "exxonmobil")
    outlet = _node(graph, "Outlet", "The Washington Post")
    witnessed = _summary_edge(graph, sponsor, outlet)
    browser = values(**{"collection-graph-view.value": "articles"})
    if target == "article":
        selected_item, kind = article, "node"
    else:
        selected_item = next(item for item in graph["article_edges"]
                             if item["source"] == article["id"] and item["predicate"] == target)
        kind = "edge"
    _choose(map_app, browser, selected_item, kind)

    response = _advance(map_app, browser, "collection-graph-view.value", "entities")
    assert response["collection-graph-selection"]["data"] == _selection(selected_item, kind)
    _assert_total(response, 1)
    assert _page_record_ids(response) == {"record-0"}
    assert not any(item["data"].get("type") == "Article"
                   for item in response["collection-graph-canvas"]["elements"])
    stylesheet = response["collection-graph-canvas"]["stylesheet"]
    highlighted_edges = {match.group(1) for rule in stylesheet
                         if rule["style"].get("opacity") == .95
                         and (match := re.fullmatch(r'edge\[id = "([^"]+)"\]', rule["selector"]))}
    highlighted_nodes = {match.group(1) for rule in stylesheet
                         if rule["style"].get("opacity") == 1
                         and (match := re.fullmatch(r'node\[id = "([^"]+)"\]', rule["selector"]))}
    assert highlighted_edges == {witnessed["id"]}
    assert highlighted_nodes == {sponsor["id"], outlet["id"]}
    # The aggregate edge contains 10 records, but the retained detail refers
    # only to this article and its two source-field relationships.
    assert not _relation_rows(response, "derived_source_association")
    for predicate in ("source_lists_sponsor", "published_in"):
        rows = _relation_rows(response, predicate)
        assert len(rows) == 1 and "1 supporting records" in _text(rows)


@pytest.mark.parametrize("target", ["article", "source_lists_sponsor", "published_in"])
def test_article_focus_remains_a_valid_dropdown_option_after_entities_switch_and_next_find_callback(map_app, target):
    graph = map_app[2].graph
    article = next(item for item in graph["nodes"]
                   if item["type"] == "Article" and item["properties"]["record_id"] == "record-0")
    browser = values(**{"collection-graph-view.value": "articles"})
    if target == "article":
        selected_item, kind = article, "node"
    else:
        selected_item = next(item for item in graph["article_edges"]
                             if item["source"] == article["id"] and item["predicate"] == target)
        kind = "edge"
    _choose(map_app, browser, selected_item, kind)

    switched = _advance(map_app, browser, "collection-graph-view.value", "entities")
    find_value = switched["collection-graph-find"]["value"]
    assert json.loads(find_value) == _selection(selected_item, kind)
    assert find_value in {option["value"] for option in switched["collection-graph-find"]["options"]}
    # React validates a controlled dropdown against its new options before its
    # next value callback; retaining only the selection Store is insufficient.
    repeated = _advance(map_app, browser, "collection-graph-find.value", find_value)
    assert repeated["collection-graph-selection"]["data"] == _selection(selected_item, kind)
    assert repeated["collection-graph-find"]["value"] == find_value
    assert find_value in {option["value"] for option in repeated["collection-graph-find"]["options"]}
    _assert_total(repeated, 1)
    assert _all_page_record_ids(map_app, browser, repeated) == {"record-0"}
    for predicate in ("source_lists_sponsor", "published_in"):
        rows = _relation_rows(repeated, predicate)
        assert len(rows) == 1 and "1 supporting records" in _text(rows)


def test_summary_edge_remains_a_valid_dropdown_option_after_articles_switch_and_next_find_callback(map_app):
    graph = map_app[2].graph
    sponsor = _node(graph, "SponsorCandidate", "exxonmobil")
    outlet = _node(graph, "Outlet", "The Washington Post")
    edge = _summary_edge(graph, sponsor, outlet)
    browser = values()
    _choose(map_app, browser, edge, "edge")

    switched = _advance(map_app, browser, "collection-graph-view.value", "articles")
    find_value = switched["collection-graph-find"]["value"]
    assert json.loads(find_value) == _selection(edge, "edge")
    assert find_value in {option["value"] for option in switched["collection-graph-find"]["options"]}
    expected = _expected_records(graph, sponsor="exxonmobil", outlet="The Washington Post")
    assert len(expected) == 10 and _article_record_ids(graph, switched) == expected

    repeated = _advance(map_app, browser, "collection-graph-find.value", find_value)
    assert repeated["collection-graph-selection"]["data"] == _selection(edge, "edge")
    assert repeated["collection-graph-anchor"]["data"] == _selection(edge, "edge")
    assert repeated["collection-graph-find"]["value"] == find_value
    assert find_value in {option["value"] for option in repeated["collection-graph-find"]["options"]}
    _assert_total(repeated, 10)
    assert _article_record_ids(graph, repeated) == expected
    assert _all_page_record_ids(map_app, browser, repeated) == expected
