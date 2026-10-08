"""Stored quality reaches native evidence without changing exact text/offsets."""

import pytest

from observatory.social_source_binding import bind_search_row
from observatory.source_text_quality import (
    current_text_quality_codes,
    text_quality_context,
)


def row(issues):
    body = "The gallery plans an opening."
    return {"evidence_id": "quality-e1", "record_id": "gallery", "version_id": "v1",
            "dataset": "native", "title": "Gallery notice", "text": body,
            "start": 12, "end": 12 + len(body), "source_text_issues": issues}


def test_current_quality_is_carried_without_cleaning_or_social_provenance():
    original = row([{"code": "body_source_partial", "severity": "info"},
                    {"code": "body_truncated_suspected", "severity": "info"}])
    result = bind_search_row(original)
    assert result.source_text_quality_codes == ["body_source_partial", "body_truncated_suspected"]
    assert result.text == original["text"] and result.start == original["start"] and result.end == original["end"]
    assert result.source_observation_id is None and result.source_quality_codes == []
    assert "source_text_issues" not in result.model_dump()


def test_old_body_truncation_is_not_a_current_body_diagnosis():
    issues = [{"code": "body_truncated_suspected", "detail": "Previous CSV body: ends with ellipsis"},
              {"code": "body_partial_recovery", "severity": "info"},
              {"code": "historical_annotations_prior_body", "severity": "info"}]
    assert current_text_quality_codes(issues) == ["body_partial_recovery"]


@pytest.mark.parametrize("issues", [None, {}, [None, 3, "text"], [{"code": "unapproved_quality_guess"}]])
def test_missing_or_unknown_flags_do_not_become_verified_complete(issues):
    result = bind_search_row(row(issues))
    context = text_quality_context(result.source_text_quality_codes)
    assert context["codes"] == [] and context["complete_article_verified"] is False


def test_duplicate_flags_do_not_inflate_quality_findings():
    assert current_text_quality_codes([{"code": "body_short"}] * 3) == ["body_short"]


def test_social_evidence_does_not_acquire_native_quality_flags():
    original = {**row([{"code": "body_source_partial"}]), "dataset": "social"}
    result = bind_search_row(original)
    assert result.source_text_quality_codes == []


def test_generation_request_receives_current_quality_without_private_issue_text():
    import json

    from test_accuracy_context import fake_generation, ferry_sentence, source

    rag, calls, _, _, _ = fake_generation()
    evidence = source(ferry_sentence()).model_copy(update={
        "source_text_quality_codes": ["body_partial_recovery", "body_truncated_suspected"]})
    result = rag.generate("What does this planning notice propose?", [evidence], "synthetic-quality")
    assert result.status == "answered"
    payload = json.loads(calls[0]["input"][1]["content"])
    quality = payload["evidence"][0]["source_text_quality"]
    assert quality["codes"] == evidence.source_text_quality_codes
    assert quality["complete_article_verified"] is False
    assert "source_text_issues" not in payload["evidence"][0]
