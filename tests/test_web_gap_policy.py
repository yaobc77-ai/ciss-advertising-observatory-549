"""Global fallback rules: operational errors never become paid web misses."""


import pytest

from observatory.models import Answer, Filters
from observatory.service import Service


class Harness:
    _ensure_answer = Service._ensure_answer

    def __init__(self):
        self.calls = 0

    def health(self):
        return {"status": "ok", "data_version": "version-1"}

    def _search_external(self, *args, **kwargs):
        self.calls += 1
        return {"status": "disabled", "reason": "web_search_not_enabled", "cost_usd": 0}


@pytest.mark.parametrize("reason", ["research_tool_unavailable", "research_tool_invalid_request",
    "research_agent_unavailable", "answer_language_mismatch", "Contradictory answer status",
    "unclassified_operational_error"])
def test_operational_failure_is_not_a_web_miss(reason):
    harness = Harness()
    answer = Answer(status="service_unavailable", answer="The read failed.", failure_reason=reason)
    result = harness._ensure_answer("Explain the museum's reopening plan.", Filters(), "dev", answer)
    assert harness.calls == 0 and result is answer


def test_clarification_still_attempts_a_labeled_web_answer():
    # User decision 2026-10-05: every question gets an answer. The clarification
    # stays visible as the reason; a web result is labelled as a supplement.
    harness = Harness()
    answer = Answer(status="insufficient_evidence", answer_mode="clarification",
                    answer="Which source value did you mean?")
    result = harness._ensure_answer("Which Cedar organization published this?", Filters(), "dev", answer)
    assert harness.calls == 1 and result is answer  # disabled lookup leaves the clarification unchanged


def test_valid_evidence_shortfall_can_still_attempt_labeled_web_lookup():
    harness = Harness()
    answer = Answer(status="insufficient_evidence", answer="No supporting passage was found.")
    harness._ensure_answer("Explain the museum's reopening plan.", Filters(), "dev", answer)
    assert harness.calls == 1
