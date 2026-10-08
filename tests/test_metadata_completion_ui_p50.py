"""Fictional source-field UI checks; no model, database or customer fixtures."""

import json

from observatory.app import _tools_card


def text(node):
    return json.dumps(node, default=lambda value: value.to_plotly_json())


def test_partial_fields_show_known_value_and_completion_scope():
    rendered = text(_tools_card({
        "answer": "Publisher: Fable Journal. Date: not recorded.",
        "structured_result": {"kind": "original_metadata", "complete": False,
                              "known_fields": ["publisher"], "missing_fields": ["publication_date"],
                              "original_fields": {"publisher": {"status": "recorded", "value": "Fable Journal"},
                                                  "publication_date": {"status": "not_recorded", "value": None}}},
    }, False))
    assert "Fable Journal" in rendered and "1 of 2 requested fields available" in rendered
    assert "Partial answer" in rendered and "Unknown" in rendered


def test_provenance_review_value_is_not_shown_as_a_recorded_answer():
    rendered = text(_tools_card({
        "answer": "Awaiting source review",
        "structured_result": {"kind": "original_metadata", "complete": False,
                              "known_fields": [], "review_fields": ["publisher"],
                              "original_fields": {"publisher": {"status": "recorded", "value": "Unconfirmed Fictional Publisher",
                                                                  "provenance": {"status": "needs_review"}}}},
    }, False))
    assert "Awaiting source review" in rendered and "0 of 1 requested fields available" in rendered
    assert "Unconfirmed Fictional Publisher" not in rendered


def test_unusable_source_value_is_not_rendered_as_known():
    rendered = text(_tools_card({
        "answer": "Original source URL is unavailable.",
        "structured_result": {"kind": "original_metadata", "complete": False,
                              "known_fields": [], "missing_fields": ["original_url"],
                              "original_fields": {"original_url": {"status": "unsafe_url_withheld", "value": "Withheld Fictional Value"}}},
    }, False))
    assert "Withheld Fictional Value" not in rendered and "Unknown" in rendered
