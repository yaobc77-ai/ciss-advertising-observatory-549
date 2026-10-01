"""Check that answer citation numbers identify the visible source quotes."""

from dash import html

from observatory.app import _evidence_cards
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
