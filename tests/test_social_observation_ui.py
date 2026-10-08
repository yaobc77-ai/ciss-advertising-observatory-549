"""Observation source identities stay visible and cannot borrow another source's citation."""

import json
from types import SimpleNamespace

import pytest
from dash import html
from flask import Flask
from plotly.utils import PlotlyJSONEncoder

from observatory.app import (
    _coverage_notice,
    _evidence_cards,
    _media_answer_citation_indices,
)
from observatory.models import Citation, Evidence
from observatory.record_view import register_record_page
from observatory.social_source_ui import (
    decorate_source_observation_cards,
    original_text_citation_matches,
    source_observation_notice,
)


def source(source_id="junkipedia:1", **updates):
    text = "The supplied observation describes a proposed pilot."
    values = {
        "evidence_id": "same-evidence", "record_id": "same-counted-post", "version_id": "a" * 64,
        "dataset": "social", "title": "A supplied post", "text": text, "start": 0, "end": len(text),
        "source_observation_id": source_id, "source_version_id": "b" * 64, "source_body_hash": "c" * 64,
        "source_observation_count": 2, "source_conflicts": ["body", "sponsor"],
        "source_quality_codes": ["body_footer_only"],
        "url": "https://example.test/original", "archive_url": "https://example.test/archive",
    }
    values.update(updates)
    return Evidence(**values)


def citation(item, **updates):
    value = {key: val for key, val in item.model_dump().items() if key.startswith("source_")}
    value.update(evidence_id=item.evidence_id, quote=item.text)
    value.update(updates)
    return Citation(**value)


def descendants(component):
    yield component
    children = getattr(component, "children", None)
    for child in children if isinstance(children, (list, tuple)) else [children]:
        if child is not None:
            yield from descendants(child)


def visible(component):
    return "\n".join(node for node in descendants(component) if isinstance(node, str))


def decorated(items, citations=(), enabled=False):
    return _evidence_cards(items, enabled, citations)


def anchors(card):
    return {node.id for node in descendants(card) if isinstance(node, html.Div)
            and getattr(node, "id", "").startswith("answer-citation-")}


def test_saved_observation_conflicts_and_quality_are_visible_outside_technical_details():
    item = source()
    card = decorated([item], [citation(item)])[0]
    text = visible(card)
    assert "Saved source observation · junkipedia:1" in text
    assert text.count("Saved source observation · junkipedia:1") == 1
    assert "2 preserved source observations of the same counted post" in text
    assert "Unresolved source differences: post text, company affiliation" in text
    assert "not been adjudicated" in text and "Source quality:" in text
    assert "paid advertising status are not verified" in text
    details = next(node for node in descendants(card) if isinstance(node, html.Details)
                   and "Technical details" in visible(node))
    assert "b" * 64 in visible(details) and "c" * 64 in visible(details)
    assert "offsets are within this observation's saved text" in visible(details)
    assert "b" * 64 not in visible(html.Div(source_observation_notice(item)))


@pytest.mark.parametrize("change", [
    {"source_observation_id": "junkipedia:other"}, {"source_version_id": "d" * 64},
    {"source_body_hash": "e" * 64},
])
def test_each_observation_identity_must_match_even_if_evidence_id_and_quote_match(change):
    item = source()
    wrong = citation(item, **change)
    assert not original_text_citation_matches(wrong, item)
    card = decorated([item], [wrong, citation(item)])[0]
    assert anchors(card) == {"answer-citation-2"}
    assert "Citation [1]" not in visible(card)


def test_two_observations_with_same_evidence_id_cannot_borrow_one_anothers_quote():
    first, other = source(), source("junkipedia:2")
    cards = decorated([first, other], [citation(first), citation(other)])
    assert anchors(cards[0]) == {"answer-citation-1"}
    assert anchors(cards[1]) == {"answer-citation-2"}


@pytest.mark.parametrize("citation_value", [
    {"evidence_id": "same-evidence", "quote": "The supplied observation describes a proposed pilot."},
    {"evidence_id": "same-evidence", "quote": "The supplied observation describes a proposed pilot.",
     "source_observation_id": "junkipedia:1"},
])
def test_unbound_or_partial_citation_keeps_uncited_excerpt(citation_value):
    item = source()
    card = decorated([item], [citation_value])[0]
    assert anchors(card) == set()
    assert item.text in visible(card)


def test_native_cards_and_original_quote_matching_remain_unchanged():
    item = Evidence(evidence_id="native", record_id="record", version_id="version", dataset="native",
                    title="Native", text="Original native sentence.", start=0, end=25)
    cited = Citation(evidence_id=item.evidence_id, quote=item.text)
    cards = _evidence_cards([item], False, [cited])
    assert original_text_citation_matches(cited, item)
    assert decorate_source_observation_cards(cards, [item], [cited])[0] is cards[0]
    assert source_observation_notice(item) == []


def test_observation_identity_cannot_be_attached_to_native_source_card():
    social = source()
    native = social.model_dump()
    native.update(dataset="native", source_observation_id=None, source_version_id=None, source_body_hash=None,
                  source_observation_count=1, source_conflicts=[], source_quality_codes=[])
    assert not original_text_citation_matches(citation(social), native)
    card = decorated([native], [citation(social)])[0]
    assert anchors(card) == set()


def test_disabled_source_links_and_caller_cards_are_preserved():
    item = source()
    cited = citation(item)
    originals = _evidence_cards([item], False, [cited])
    before = json.dumps(originals[0].to_plotly_json(), cls=PlotlyJSONEncoder)
    card = decorate_source_observation_cards(originals, [item], [cited])[0]
    links = [node.href for node in descendants(card) if isinstance(node, html.A)]
    assert links == ["/records/same-counted-post"]
    assert item.url not in links and item.archive_url not in links
    assert json.dumps(originals[0].to_plotly_json(), cls=PlotlyJSONEncoder) == before


def test_invalid_observation_metadata_is_not_printed_as_trusted_identity():
    item = source().model_dump()
    item["source_observation_id"] = "C:/PRIVATE-PATH"
    notice = visible(html.Div(source_observation_notice(item)))
    assert "incomplete or invalid" in notice and "PRIVATE-PATH" not in notice
    assert not original_text_citation_matches({"evidence_id": item["evidence_id"], "quote": item["text"]}, item)


def test_global_literal_phrase_notice_describes_saved_text_matching():
    notice = _coverage_notice({"status": "available", "operator": "literal_phrase"})
    text = visible(notice)
    assert "Saved text phrase match" in text
    assert "Matched the literal phrase in saved social text" in text
    assert "rank is not a confidence score" in text
    assert "OR matching" not in text and "English word stems" not in text


@pytest.mark.parametrize("change", [
    {"source_observation_id": "junkipedia:other"}, {"source_version_id": "d" * 64},
    {"source_body_hash": "e" * 64},
])
def test_global_mixed_media_anchor_rejects_another_source_observation(change):
    item = source()
    media = {"evidence_id": "image-one", "media_type": "image", "origin": "ocr",
             "evidence_text": "A supplied image transcription.", "quote_from_original_body": False}
    wrong, valid = citation(item, **change), citation(item)
    result = {
        "evidence": [item], "media_evidence": [media],
        "citations": [wrong, valid, {"evidence_id": media["evidence_id"], "evidence_type": "image",
                                     "origin": "ocr", "quote": media["evidence_text"]}],
    }
    assert _media_answer_citation_indices(result) == {2, 3}
    assert anchors(_evidence_cards([item], False, result["citations"])[0]) == {"answer-citation-2"}


def detail_html(*, enabled=False, legacy_status="paused_body_disagreement", conflicts=True, retrieval_permission=None):
    from test_social_admission_detail import detail

    row = detail()
    row["retrievable"] = enabled if retrieval_permission is None else retrieval_permission
    row["social_admission"]["retrieval_status"] = legacy_status
    if enabled:
        row["source_observation_retrieval"] = {"status": "enabled_source_observations", "observations": 2,
                                              "semantic_completeness_verified": False}
        row["body_note"] = "Each preserved source observation is searchable separately; the text below is one saved observation."
    if not conflicts:
        row["social_admission"]["conflicting_fields"] = []
        row["social_admission"]["variants"] = row["social_admission"]["variants"][:1]
        row["social_admission"]["member_count"] = 1
    app = Flask(__name__)
    register_record_page(app, SimpleNamespace(get=lambda identifier: row))
    response = app.test_client().get("/records/" + row["record_id"])
    assert response.status_code == 200
    return response.get_data(as_text=True)


@pytest.mark.parametrize("legacy_status", ["paused_body_disagreement", "paused_text_quality", "paused_sentence_source_validation"])
@pytest.mark.parametrize("conflicts", [True, False])
def test_actual_detail_template_prioritizes_enabled_observations_over_legacy_pause(legacy_status, conflicts):
    text = detail_html(enabled=True, legacy_status=legacy_status, conflicts=conflicts)
    assert "Retrieval is enabled for every preserved source observation" in text
    assert "RAG retrieval is paused" not in text and "differing text is paused" not in text
    assert "Source observation 1 · junkipedia:101" in text
    assert "images, audio and referenced post contents remain unverified" in text
    if conflicts:
        assert "Unresolved source differences" in text and "not been adjudicated" in text
        assert "The second observation &lt;script&gt;trap()&lt;/script&gt;." in text
    assert "PRIVATE-PATH-TRAP" not in text


@pytest.mark.parametrize("legacy_status", ["paused_body_disagreement", "paused_text_quality", "paused_sentence_source_validation"])
def test_actual_detail_template_retains_legacy_pause_without_current_source_permission(legacy_status):
    text = detail_html(legacy_status=legacy_status)
    assert "RAG retrieval is paused" in text
    assert "Retrieval is enabled for every preserved source observation" not in text


def test_actual_detail_enabled_marker_requires_current_retrieval_permission():
    text = detail_html(enabled=True, retrieval_permission=False)
    assert "RAG retrieval is paused" in text
    assert "Retrieval is enabled for every preserved source observation" not in text
