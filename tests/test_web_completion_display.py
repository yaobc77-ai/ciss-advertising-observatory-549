"""Independent fictional cases for collection completion and web provenance."""

import json

import pytest

from observatory.app import _render_answer_result


def rendered(result, links=True):
    cards = _render_answer_result(result, links, object())
    return [json.dumps(card, default=lambda item: item.to_plotly_json()) for card in cards]


def supplement():
    return {
        "status": "answered", "answer_mode": "web_supplement", "citations": [],
        "answer": "Web context only.\n\nWhy the collection could not answer: No matching stored source.",
        "external_research": {
            "status": "ok",
            "passages": [{"text": "The museum describes its restoration project. [W1]", "source_ids": ["W1"]}],
            "sources": [{"source_id": "W1", "title": "Museum restoration", "url": "https://example.test/museum"}],
            "collection_answer": {"status": "insufficient_evidence", "answer_mode": "rag",
                                  "answer": "No matching stored source.", "complete": False},
        },
    }


@pytest.mark.parametrize("links", [True, False])
def test_external_sources_do_not_look_like_a_completed_collection_answer(links):
    cards = rendered(supplement(), links)
    assert "Collection answer incomplete" in cards[0]
    assert "do not complete" in cards[0]
    assert "Additional web sources" in cards[1]
    assert "Museum restoration" in cards[1]
    assert "No matching stored source." in cards[1]
    assert ('"href": "https://example.test/museum"' in cards[1]) is links


def test_existing_serialized_supplements_also_expose_incomplete_state():
    result = supplement()
    result["external_research"].pop("collection_answer")
    assert "Collection answer incomplete" in rendered(result)[0]


def test_partial_tool_parts_are_preserved_between_warning_and_web_sources():
    result = supplement()
    result["structured_result"] = {
        "kind": "composite", "complete": False,
        "parts": [{"question_part": "Read the museum source", "answer": {
            "status": "answered", "answer_mode": "tools", "answer": "Stored interval.",
            "structured_result": {"kind": "record", "record_id": "museum-fiction",
                                  "body": {"start": 0, "end": 20, "total_characters": 20,
                                           "text": "Original museum text"}},
        }}],
        "pending_parts": [{"question_part": "Identify the restoration date"}],
    }
    result["external_research"]["collection_answer"]["answer_mode"] = "tools"
    cards = rendered(result)
    text = " ".join(cards)
    assert text.index("Collection answer incomplete") < text.index("Original museum text")
    assert text.index("Original museum text") < text.index("Additional web sources")
    assert "Identify the restoration date" in text


def test_completed_collection_answer_has_no_external_incomplete_warning():
    result = {"status": "answered", "answer_mode": "tools", "structured_result": {
        "kind": "record", "body": {"start": 0, "end": 20, "total_characters": 20,
                                    "text": "Original museum text"},
    }}
    text = " ".join(rendered(result))
    assert "Original museum text" in text
    assert "Collection answer incomplete" not in text


def test_failed_web_lookup_cannot_display_a_successful_web_answer():
    result = supplement()
    result["external_research"]["status"] = "no_sources"
    text = " ".join(rendered(result))
    assert "Collection answer incomplete" in text and "Web answer unavailable" in text
    assert "The museum describes" not in text
