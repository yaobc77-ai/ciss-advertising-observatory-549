"""Typed media answer presentation with real Dash components, without providers."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from dash import html

from observatory.app import (
    _evidence_cards,
    _evidence_coverage,
    _media_evidence_cards,
    _media_location,
    _partial_collection_answer,
    _render_answer_result,
    create_app,
)
from observatory.models import Answer, MediaAnswerEvidence


def descendants(component):
    yield component
    children = getattr(component, "children", None)
    for child in children if isinstance(children, (list, tuple)) else [children]:
        if child is not None:
            yield from descendants(child)


def visible(component):
    return "\n".join(node for node in descendants(component) if isinstance(node, str))


def links(component):
    return [node.href for node in descendants(component) if isinstance(node, html.A)]


def media(kind="image", origin=None, evidence_id="media-one"):
    origin = origin or ("vision" if kind == "image" else "publisher_caption")
    text = "The supplied material describes a proposed carbon capture pilot."
    return {
        "evidence_id": evidence_id, "asset_id": "asset-one", "record_id": "record-one",
        "version_id": "a" * 64, "dataset": "native", "title": "A proposed capture pilot",
        "source_url": "https://example.test/ad", "media_type": kind, "origin": origin,
        "locator": {"kind": "image_region", "screenshot_sha256": "b" * 64, "page_number": 2,
                    "region": [0.1, 0.2, 0.8, 0.9]} if kind == "image" else {
                        "kind": "video_time", "start_ms": 12500, "end_ms": 19500,
                        "timing_scope": "supplied_segment"},
        "evidence_text": text, "quality_label": "Derived material; semantics have not been independently reviewed.",
        "asset_sha256": "b" * 64, "text_artifact_id": "derived-one", "artifact_sha256": "c" * 64,
        "derived_start": 0, "derived_end": len(text), "quote_from_original_body": False,
        "image_or_speech_semantics_verified": False,
    }


def citation(item):
    return {"evidence_id": item["evidence_id"], "quote": item["evidence_text"],
            "evidence_type": item["media_type"], "origin": item["origin"]}


def answer(item, status="answered"):
    return {"status": status, "answer_mode": "rag", "answer": "The material describes a planned pilot.",
            "media_evidence": [item], "citations": [citation(item)], "evidence": [],
            "summary": [{"text": "A planned pilot is described.", "citation_indices": [1]}],
            "cited_claims": [{"text": "The supplied description is about a proposed pilot.", "citation_indices": [1]}],
            "sections": [{"title": "Project status", "citation_indices": [1]}]}


@pytest.mark.parametrize("kind,origin,label", [
    ("image", "ocr", "OCR text"), ("image", "vision", "Model image description"),
    ("image", "human_description", "Human image description"),
    ("video", "publisher_caption", "Publisher captions"),
    ("video", "automatic_caption", "Automatic captions"), ("video", "transcript", "Speech transcription"),
    ("video", "human_description", "Human video description"),
])
def test_source_origins_have_explicit_labels_and_common_citation_anchors(kind, origin, label):
    item = media(kind, origin)
    cards = _media_evidence_cards([MediaAnswerEvidence(**item)], True, [citation(item)])
    text = visible(html.Div(cards))
    assert label in text and "not an original article quotation" in text
    assert "Citation [1]" in text
    assert {getattr(node, "id", None) for node in descendants(html.Div(cards))} >= {"answer-citation-1"}
    assert len([node for node in descendants(html.Div(cards)) if isinstance(node, html.Blockquote)]) == (
        0 if origin in {"vision", "human_description"} else 1
    )


def test_media_only_answer_uses_global_summary_findings_evidence_and_limits():
    result = answer(media())
    card = html.Div(_render_answer_result(result, True, None))
    text = visible(card)
    assert all(label in text for label in ("Summary", "Findings", "Evidence", "Scope and limits"))
    assert "A planned pilot is described." in text and "Project status" in text
    assert "Records and quoted evidence" not in text
    assert links(card).count("#answer-citation-1") == 2
    assert "not independently verified" in text


def test_mixed_answer_keeps_one_based_body_and_media_citation_numbers():
    item = media("video")
    result = answer(item)
    body = {"evidence_id": "article-one", "record_id": "body-record", "version_id": "old",
            "dataset": "native", "title": "Stored body", "text": "A separate article describes a pilot.",
            "start": 0, "end": 40}
    result["evidence"] = [body]
    result["citations"].insert(0, {"evidence_id": "article-one", "quote": "describes a pilot"})
    result["summary"][0]["citation_indices"] = [1, 2]
    result["sections"][0]["citation_indices"] = [2]
    result["cited_claims"][0]["citation_indices"] = [2]
    card = html.Div(_render_answer_result(result, True, None))
    anchors = {getattr(node, "id", None) for node in descendants(card)}
    assert {"answer-citation-1", "answer-citation-2"} <= anchors
    assert "Citation [1]" in visible(card) and "Citation [2]" in visible(card)
    assert "Stored body" in visible(card) and "Records and quoted evidence" in visible(card)
    assert "Image and video evidence" in visible(card)


@pytest.mark.parametrize("mutation", [
    {"evidence_type": "article_text", "origin": "original_text"},
    {"evidence_type": "video"}, {"origin": "ocr"}, {"quote": "An invented sentence."},
])
def test_wrong_media_citation_type_origin_or_text_does_not_make_an_anchor(mutation):
    item = media()
    result = answer(item)
    result["citations"][0].update(mutation)
    card = html.Div(_render_answer_result(result, True, None))
    assert "answer-citation-1" not in {getattr(node, "id", None) for node in descendants(card)}
    assert "#answer-citation-1" not in links(card)
    assert "Citation [1]" not in visible(card)
    assert item["evidence_text"] in visible(card)  # Still a retrieved candidate, not a cited finding.


def test_a_media_citation_cannot_be_reinterpreted_as_a_body_quote_with_the_same_id():
    item = media()
    body = {"evidence_id": item["evidence_id"], "record_id": "r", "dataset": "native",
            "title": "Saved body", "text": item["evidence_text"], "start": 0, "end": 65}
    cards = _evidence_cards([body], True, [citation(item)])
    assert "Citation [1]" not in visible(html.Div(cards))


@pytest.mark.parametrize("changes", [
    {"media_type": "image", "origin": "transcript"}, {"origin": "original_text"},
    {"quote_from_original_body": True}, {"quote_from_original_body": None},
])
def test_invalid_media_source_identity_is_not_rendered(changes):
    item = media()
    item.update(changes)
    assert _media_evidence_cards([item], True, [citation(item)]) == []


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///private/a.png", "C:/private/a.png",
                                 "https://user:secret@example.test/ad", "https://[invalid"])
def test_unsafe_media_source_urls_and_extra_local_paths_do_not_become_links(url):
    item = media()
    item.update(source_url=url, local_path="C:/private/source.png", producer="private/internal/model")
    card = html.Div(_media_evidence_cards([item], True))
    assert links(card) == ["/records/record-one"]
    assert "C:/private/source.png" not in visible(card) and "private/internal/model" not in visible(card)


def test_source_links_disabled_keeps_internal_record_access_and_citation_numbers():
    item = media()
    card = html.Div(_media_evidence_cards([item], False, [citation(item)]))
    assert links(card) == ["/records/record-one"]
    assert "Source links are disabled" in visible(card) and "Citation [1]" in visible(card)


def test_hashes_and_derived_offsets_are_only_inside_collapsed_technical_details():
    card = _media_evidence_cards([media()], True)[0]
    details = next(node for node in descendants(card) if isinstance(node, html.Details)
                   and getattr(node.children[0], "children", None) == "Technical details")
    assert not getattr(details, "open", False)
    assert all(char * 64 in visible(details) for char in ("a", "b", "c"))
    assert "a" * 64 not in visible(html.Div([child for child in card.children if child is not details]))


def test_image_page_region_and_supplied_video_frame_are_readable_without_body_positions():
    item = media()
    item["locator"].update(video_asset_id="video-parent", video_time_ms=2500)
    text = _media_location(item)
    assert "Page 2" in text and "0.10, 0.20, 0.80, 0.90" in text
    assert "Supplied video frame: 00:02.500" in text and "Character" not in text


@pytest.mark.parametrize("location", [{}, {"kind": "characters", "start": 1, "end": 9},
                                      {"kind": "image_region", "region": [0, 0, float("nan"), 1]},
                                      {"kind": "image_region", "region": [0.8, 0, 0.2, 1]}])
def test_unavailable_or_invalid_image_regions_are_not_inferred(location):
    item = media()
    item["locator"] = location
    assert "Image region not provided" in _media_location(item)


def test_video_times_are_supplied_segments_not_word_alignment():
    item = media("video")
    location = _media_location(item)
    assert "00:12.500–00:19.500" in location and "not per-word alignment" in location
    item["locator"] = {"kind": "video_time", "start_ms": None, "end_ms": None}
    assert "Video time not provided" in _media_location(item)
    assert "00:00" not in _media_location(item)


def test_media_only_partial_comparison_reports_citation_support_for_each_company():
    result = answer(media(), status="insufficient_evidence")
    result["structured_result"] = {"kind": "evidence_coverage", "groups": [
        {"label": "ExxonMobil", "passages": 0, "image_units": 1, "video_units": 0,
         "evidence_units": 1, "media_status": "ok", "cited_records": 1, "citation_coverage_status": "cited"},
        {"label": "Shell", "passages": 1, "image_units": 0, "video_units": 0,
         "evidence_units": 1, "media_status": "missing_material", "cited_records": 0,
         "citation_coverage_status": "uncited"},
    ]}
    assert _partial_collection_answer(result)
    card = html.Div(_render_answer_result(result, True, None))
    text = visible(card)
    assert "Partial collection evidence" in text and "1 cited record" in text and "0 cited records" in text
    assert "No source cited for: Shell." in text
    assert "No evidence retrieved for: Shell." not in text
    assert "Body passages" in text and "Image units" in text and "Video units" in text
    assert "not advertisement counts" in text
    # Zero body passages is not a gap when media supplies and supports both groups.
    complete = deepcopy(result)
    complete["structured_result"]["groups"][1]["cited_records"] = 1
    assert not _partial_collection_answer(complete)


@pytest.mark.parametrize("media_status,label", [
    ("not_configured", "Media collection not configured"),
    ("missing_material", "Requested media material not supplied"),
    ("no_match", "Supplied media did not match"), ("unavailable", "Media could not be verified"),
])
def test_media_statuses_explain_material_limits_without_asserting_absence(media_status, label):
    result = {"status": "insufficient_evidence", "answer": "No usable evidence was returned.",
              "media_evidence": [], "structured_result": {"kind": "evidence_coverage", "groups": [
                  {"label": "Shell", "passages": 0, "evidence_units": 0, "media_status": media_status},
              ]}}
    text = visible(_evidence_coverage(result))
    assert label in text and "does not prove absence" in text
    assert not any(isinstance(node, html.Article) for node in descendants(html.Div(_render_answer_result(result, True, None))))


def test_failure_candidates_keep_source_kind_and_do_not_become_numbered_findings():
    item = media("video", "transcript")
    result = {**answer(item), "status": "service_unavailable"}
    card = html.Div(_render_answer_result(result, True, None))
    text = visible(card)
    assert "Status" in text and "Next step" in text and "Retrieved media evidence" in text
    assert "Scope and limits" in text and "Speech transcription" in text
    assert "Citation [1]" not in text and "A planned pilot is described." not in text


def test_changed_media_explains_why_the_answer_was_withheld():
    result = {"status": "service_unavailable", "answer": "Internal source-change message.",
              "failure_reason": "media_evidence_mismatch", "media_evidence": [], "evidence": []}
    text = visible(html.Div(_render_answer_result(result, True, None)))
    assert "Media evidence could not be verified" in text and "The answer was withheld." in text
    assert "Internal source-change message" not in text and "Citation [" not in text


def test_actual_dash_callback_serializes_typed_media_summary_and_evidence(monkeypatch):
    from test_app import FakeService, callback, research_values

    service = FakeService()
    result = Answer(**answer(media("video", "automatic_caption")))
    monkeypatch.setattr(service, "answer", lambda question, filters, **kwargs: result)
    app = create_app(service, SimpleNamespace(show_source_links=True, cookie_secret="media-ui-test",
                                              monthly_budget_usd=100))
    response = callback(app, app.server.test_client(), "research-results.children",
                        research_values() | {"answer-paid.n_clicks": 1}, "answer-paid.n_clicks")
    text = str(response["research-results"]["children"])
    assert "Summary" in text and "Automatic captions" in text and "answer-citation-1" in text
    assert "Supplied video segment" in text and "not per-word alignment" in text
    assert not service.answer_calls and not service.search_calls
