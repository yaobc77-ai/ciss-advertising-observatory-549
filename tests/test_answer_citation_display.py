"""Check that answer citation numbers identify the visible source quotes."""

from dash import html

from observatory.app import (
    _evidence_cards,
    _evidence_coverage,
    _external_research_card,
    _grounded_answer_content,
    _partial_collection_answer,
)
from observatory.models import Citation, Evidence


def evidence(evidence_id, text):
    return Evidence(
        evidence_id=evidence_id,
        record_id=f"record-{evidence_id}",
        version_id=f"version-{evidence_id}",
        dataset="native",
        title=f"Article {evidence_id}",
        text=text,
        start=0,
        end=len(text),
        url="https://example.test/original",
        archive_url="https://archive.example.test/snapshot",
    )


def descendants(component):
    yield component
    children = getattr(component, "children", None)
    if children is not None:
        for child in children if isinstance(children, (tuple, list)) else [children]:
            yield from descendants(child)


def citation_labels(card):
    return [
        component.children
        for component in descendants(card)
        if isinstance(component, html.Span)
        and isinstance(component.children, str)
        and component.children.startswith("Citation [")
    ]


def quotes(card):
    return [
        component.children
        for component in descendants(card)
        if isinstance(component, html.Blockquote)
    ]


def test_citation_order_is_independent_of_retrieval_rank():
    items = [
        evidence("first", "First article supports a later claim."),
        evidence("uncited", "A retrieved article that was not cited."),
        evidence("third", "Third article supports the first claim."),
    ]
    cards = _evidence_cards(items, True, [
        Citation(evidence_id="third", quote="supports the first claim"),
        Citation(evidence_id="first", quote="supports a later claim"),
    ])

    assert [citation_labels(card) for card in cards] == [
        ["Citation [2]"], [], ["Citation [1]"],
    ]
    assert [quotes(card) for card in cards] == [
        ["supports a later claim"], [items[1].text], ["supports the first claim"],
    ]
    for rank, card in enumerate(cards, start=1):
        assert f"Retrieval rank {rank}" in list(descendants(card))
    assert "Article third" in list(descendants(cards[2]))


def test_all_quotes_from_one_evidence_keep_their_answer_numbers():
    first = evidence("one", "A pilot is planned. Its outcome is uncertain.")
    other = evidence("other", "Another sponsor describes a different project.")
    cards = _evidence_cards([first, other], True, [
        Citation(evidence_id="one", quote="A pilot is planned."),
        Citation(evidence_id="other", quote="a different project"),
        Citation(evidence_id="one", quote="Its outcome is uncertain."),
    ])

    assert citation_labels(cards[0]) == ["Citation [1]", "Citation [3]"]
    assert quotes(cards[0]) == ["A pilot is planned.", "Its outcome is uncertain."]
    assert citation_labels(cards[1]) == ["Citation [2]"]


def test_repeated_quote_preserves_each_citation_number():
    quote = "The advertisement describes a planned pilot."
    cards = _evidence_cards([evidence("one", quote)], True, [
        Citation(evidence_id="one", quote=quote),
        Citation(evidence_id="one", quote=quote),
    ])

    assert citation_labels(cards[0]) == ["Citation [1]", "Citation [2]"]
    assert quotes(cards[0]) == [quote, quote]


def test_invalid_citations_do_not_renumber_a_later_valid_citation():
    quote = "The outcome is uncertain."
    cards = _evidence_cards([evidence("one", quote)], True, [
        Citation(evidence_id="missing-source", quote=quote),
        {"quote": quote},
        Citation(evidence_id="one", quote="The project is proven."),
        Citation(evidence_id="one", quote=" "),
        {"evidence_id": "one", "quote": 123},
        Citation(evidence_id="one", quote=quote),
    ])

    assert citation_labels(cards[0]) == ["Citation [6]"]
    assert quotes(cards[0]) == [quote]


def test_uncited_or_mismatched_evidence_retains_the_retrieved_excerpt():
    text = "The article describes a planned project. " * 30
    item = evidence("one", text)
    for citations in [[], [Citation(evidence_id="one", quote="Not in this source.")]]:
        cards = _evidence_cards([item], True, citations)
        assert citation_labels(cards[0]) == []
        assert quotes(cards[0]) == [text[:520] + "…"]


def test_a_valid_cited_quote_is_displayed_in_full():
    # The 60-word limit does not guarantee a quote is at most 520 characters.
    quote = " ".join(["a-long-technical-expression"] * 30)
    cards = _evidence_cards([evidence("one", quote)], True, [
        Citation(evidence_id="one", quote=quote),
    ])

    assert len(quote) > 520
    assert quotes(cards[0]) == [quote]
    assert citation_labels(cards[0]) == ["Citation [1]"]


def test_numbered_citations_respect_disabled_source_links():
    item = evidence("one", "The sponsor proposes a pilot.")
    cards = _evidence_cards([item], False, [
        Citation(evidence_id="one", quote="proposes a pilot"),
    ])
    links = [
        component.href
        for component in descendants(cards[0])
        if isinstance(component, html.A)
    ]

    assert item.url not in links and item.archive_url not in links
    assert links == ["/records/record-one"]
    assert "Source links are disabled" in list(descendants(cards[0]))
    assert citation_labels(cards[0]) == ["Citation [1]"]


def test_summary_and_company_sections_link_to_numbered_source_quotes():
    result = {
        "summary": [{"text": "Both advertisements describe emissions projects.", "citation_indices": [1, 2]}],
        "sections": [{"title": "ExxonMobil", "citation_indices": [1]},
                     {"title": "Shell", "citation_indices": [2]}],
        "cited_claims": [{"text": "ExxonMobil describes a proposed project.", "citation_indices": [1]},
                         {"text": "Shell describes an operating facility.", "citation_indices": [2]}],
        "citations": [{"evidence_id": "one", "quote": "a proposed project"},
                      {"evidence_id": "two", "quote": "an operating facility"}],
    }
    content = _grounded_answer_content(result, "Legacy flat answer.")
    # Traverse the returned list as a container, like the Query answer card.
    visible = list(descendants(html.Div(content)))
    assert "Summary" in visible and "ExxonMobil" in visible and "Shell" in visible
    assert "Legacy flat answer." not in visible
    links = [item.href for item in visible if isinstance(item, html.A)]
    assert links == ["#answer-citation-1", "#answer-citation-2",
                     "#answer-citation-1", "#answer-citation-2"]
    cards = _evidence_cards([evidence("one", "ExxonMobil describes a proposed project."),
                             evidence("two", "Shell describes an operating facility.")], True, result["citations"])
    assert {item.id for item in descendants(html.Div(cards))
            if isinstance(item, html.Div) and getattr(item, "id", "").startswith("answer-citation-")} == {
        "answer-citation-1", "answer-citation-2",
    }


def test_legacy_answers_keep_their_text_and_invalid_reference_numbers_are_not_links():
    assert "Legacy source-grounded answer." in list(descendants(html.Div(
        _grounded_answer_content({}, "Legacy source-grounded answer."),
    )))
    result = {"summary": [{"text": "A description.", "citation_indices": [1, 0, 2, True]}],
              "citations": [{"evidence_id": "one", "quote": "A description."}]}
    links = [item.href for item in descendants(html.Div(_grounded_answer_content(result, "")))
             if isinstance(item, html.A)]
    assert links == ["#answer-citation-1"]


def test_web_passages_remain_explicitly_outside_collection_and_use_safe_source_links():
    result = {"external_research": {
        "status": "ok", "source_kind": "external_web",
        "passages": [{"text": "A provider summary of an external announcement. [W1]", "source_ids": ["W1"]},
                     {"text": "A malformed uncited passage.", "source_ids": ["W2"]}],
        "sources": [{"source_id": "W1", "url": "https://example.test/announcement", "title": "Announcement"},
                    {"source_id": "W2", "url": "javascript:alert(1)", "title": "Unsafe"}],
    }}
    card = _external_research_card(result, True)
    visible = list(descendants(card))
    assert "Outside the advertising collection" in visible
    assert "Additional web sources" in visible
    assert "A malformed uncited passage." not in visible
    links = [item.href for item in visible if isinstance(item, html.A)]
    assert links == ["https://example.test/announcement", "https://example.test/announcement"]
    paragraphs = [item for item in visible if isinstance(item, html.P)]
    cited = next(item for item in paragraphs if "provider summary" in str(item.children))
    assert len([item for item in descendants(cited) if isinstance(item, html.A)]) == 1
    assert "Not independently verified" in visible
    hidden = _external_research_card(result, False)
    assert "https://example.test/announcement" not in [item.href for item in descendants(hidden) if isinstance(item, html.A)]


def test_web_lookup_failure_has_no_provider_details_or_fabricated_evidence():
    result = {"external_research": {"status": "unavailable", "summary": "private exception trace"}}
    card = _external_research_card(result, True)
    assert "private exception trace" not in list(descendants(card))
    assert not any(isinstance(item, html.A) for item in descendants(card))
    assert _external_research_card({}, True) is None


def test_comparison_coverage_exposes_the_missing_side_without_asserting_absence():
    result = {"structured_result": {"kind": "evidence_coverage", "groups": [
        {"label": "ExxonMobil", "passages": 3}, {"label": "Shell", "passages": 0},
    ]}}
    card = _evidence_coverage(result)
    visible = list(descendants(card))
    assert "ExxonMobil" in visible and "Shell" in visible
    assert "3" in visible and "0" in visible
    assert "No matching stored passage retrieved" in visible
    assert card.open is True
    assert "No matching stored passages retrieved for: Shell." in visible
    assert any(isinstance(item, str) and "does not prove" in item for item in visible)
    assert _evidence_coverage({}) is None


def test_complete_retrieval_coverage_is_a_collapsed_disclosure():
    card = _evidence_coverage({"structured_result": {"kind": "evidence_coverage", "groups": [
        {"label": "ExxonMobil", "passages": 3}, {"label": "Shell", "passages": 2},
    ]}})
    assert isinstance(card, html.Details)
    assert card.open is False
    assert "Retrieval coverage" in list(descendants(card))


def test_one_missing_comparison_side_is_partial_even_with_a_cited_summary():
    result = {"status": "insufficient_evidence", "summary": [{"text": "Only one side is supported.", "citation_indices": [1]}],
              "structured_result": {"kind": "evidence_coverage", "groups": [
                  {"label": "ExxonMobil", "passages": 2}, {"label": "Shell", "passages": 0},
              ]}}
    assert _partial_collection_answer(result) is True
    assert _partial_collection_answer({**result, "status": "answered"}) is False
    assert _partial_collection_answer({**result, "summary": []}) is False
    assert _partial_collection_answer({"status": "insufficient_evidence", "summary": result["summary"]}) is False
