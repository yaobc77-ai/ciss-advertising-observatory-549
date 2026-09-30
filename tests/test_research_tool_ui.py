"""Public research tools expose results, not private provider bookkeeping."""

import json

from test_app import component_tree

from observatory.app import _research_steps, _tools_card


def rendered(component):
    return json.dumps(component, default=lambda item: item.to_plotly_json())


def test_public_trace_shows_tool_steps_and_cost_without_provider_or_arguments():
    result = {"cost_usd": 0.00042, "research_trace": {
        "model_calls": [{"response_id": "private-response", "reservation_id": "private-ledger"}],
        "tools": [{"tool": "record_statistics", "status": "ok", "arguments": {"private": "hidden"}}],
    }}
    text = rendered(_research_steps(result))
    assert "1 model call" in text and "$0.00042" in text
    assert "record_statistics" in text
    assert "private-response" not in text and "private-ledger" not in text and "hidden" not in text


def test_graph_card_is_explicit_about_partial_coverage_and_links_originating_record():
    result = {"structured_result": {"kind": "graph", "graph": {
        "nodes": [{"id": "a", "label": "Article"}, {"id": "b", "label": "ExxonMobil"}],
        "edges": [{"source": "a", "target": "b", "predicate": "source_lists_sponsor",
                   "label": "source lists sponsor", "provenance": {"record_id": "record-1"}}],
    }}}
    text = rendered(_tools_card(result, True))
    assert "source lists sponsor" in text and "ExxonMobil" in text
    assert "/records/record-1" in text and "not the complete graph" in text


def test_source_card_honors_source_link_toggle_and_unverified_capture_status():
    result = {"structured_result": {"kind": "sources", "source_artifacts": [
        {"label": "Original web reference", "properties": {"url": "https://example.org/article"}},
    ]}}
    assert "https://example.org/article" in rendered(_tools_card(result, True))
    hidden = rendered(_tools_card(result, False))
    assert "https://example.org/article" not in hidden
    assert "Historical annotations are unverified" in hidden and "not connected" in hidden


def test_text_interval_card_preserves_literal_text_and_reports_coverage():
    result = {"structured_result": {"kind": "record", "body": {
        "text": "Advertiser says <script>literal text</script>", "start": 40, "end": 90, "total_characters": 190,
    }}}
    card = _tools_card(result, True)
    nodes = list(component_tree(json.loads(rendered(card))))
    pre = next(item for item in nodes if item["type"] == "Pre")
    assert pre["props"]["children"] == result["structured_result"]["body"]["text"]
    assert "Characters 40" in rendered(card) and "completeness" in rendered(card)
