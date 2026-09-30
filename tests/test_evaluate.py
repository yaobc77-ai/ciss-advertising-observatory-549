from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from observatory import evaluate
from observatory.config import Settings
from observatory.models import Answer, Citation, Evidence


def case(case_id="case-1", **changes):
    values = {
        "id": case_id, "suite": "development", "dataset": "native", "status": "ready",
        "case_type": "retrieval", "question": "What does the proposed project claim?",
        "filters": {"dataset": "native"}, "required_record_ids": ["r1"],
        "support_quote": [{"record_id": "r1", "quote": "The facility is proposed."}],
        "rubric": ["Preserve proposed versus achieved."], "selection_note": "Unit-test fixture only.",
    }
    values.update(changes)
    return evaluate.EvaluationCase.model_validate(values)


def record(rid="r1", body="The facility is proposed."):
    return {"record_id": rid, "dataset": "native", "version_id": "v1",
            "body": body, "payload": {"retrievable": True}}


def evidence(rid="r1", text="The facility is proposed.", start=0, evidence_id=None):
    return Evidence(evidence_id=evidence_id or f"e-{rid}", record_id=rid, version_id="v1", dataset="native",
                    title="Unit fixture", text=text, start=start, end=start + len(text))


class FakeService:
    def __init__(self, records=None, evidence_rows=None, result=None):
        self.records = records or {"r1": record()}
        self.evidence_rows = evidence_rows if evidence_rows is not None else [evidence()]
        self.result = result
        self.settings = Settings()
        self.version = "dataset-1"
        self.answer_calls = 0
        self.search_calls = 0
        self.db = SimpleNamespace(
            health=lambda: {"data_version": self.version},
            validate_evidence=lambda item: item.record_id in self.records and
            item.text == self.records[item.record_id]["body"][item.start:item.end],
        )

    def browse(self, filters):
        return [{"record_id": rid} for rid in self.records]

    def statistics(self, filters):
        return {"total": len(self.records)}

    def search(self, *args, **kwargs):
        self.search_calls += 1
        return self.evidence_rows

    def answer(self, *args, **kwargs):
        self.answer_calls += 1
        assert self.result is not None, "Unexpected paid dispatch in a free test"
        return self.result


def bind(monkeypatch, service):
    monkeypatch.setattr(evaluate, "load_snapshot", lambda db: service.records)
    monkeypatch.setattr(evaluate, "ledger_usage", lambda db, visitor: {
        "status": "ledger_observed", "settled_usd": 0.016, "unresolved_reserved_usd": 0.0,
    })


def test_nonverbatim_gold_is_rejected_before_any_search_or_answer(monkeypatch):
    service = FakeService()
    bind(monkeypatch, service)
    altered = case(support_quote=[{"record_id": "r1", "quote": "The facility is operating."}])
    with pytest.raises(evaluate.EvaluationInvalid, match="exact support quote"):
        evaluate.run_evaluation([altered], service, paid=True)
    assert service.search_calls == service.answer_calls == 0


def test_gold_record_must_belong_to_the_selected_filter_set():
    with pytest.raises(evaluate.EvaluationInvalid, match="excluded by filters"):
        evaluate.validate_gold([case()], {"r1": record()}, {"case-1": []})


def test_gold_in_quarantined_navigation_is_rejected_before_dispatch(monkeypatch):
    source = record()
    source["payload"]["retrieval_end"] = 5
    service = FakeService(records={"r1": source})
    bind(monkeypatch, service)
    with pytest.raises(evaluate.EvaluationInvalid, match="accepted retrieval boundary"):
        evaluate.run_evaluation([case()], service, paid=True)
    assert service.search_calls == service.answer_calls == 0


def test_hit_at_five_requires_all_gold_records_and_free_never_calls_answer(monkeypatch):
    service = FakeService(records={"r1": record(), "r2": record("r2")})
    bind(monkeypatch, service)
    multi = case(required_record_ids=["r1", "r2"], support_quote=[
        {"record_id": rid, "quote": "The facility is proposed."} for rid in ("r1", "r2")
    ])
    result = evaluate.run_evaluation([multi], service)
    assert result["summary"]["native"]["hit_at_5"] == {"numerator": 0, "denominator": 1, "rate": 0}
    assert service.answer_calls == 0 and service.search_calls == 1
    assert result["summary"]["native"]["settled_cost_usd"] == 0
    assert result["summary"]["native"]["citation_locator_valid"]["rate"] is None


def test_paid_keeps_all_fifteen_passages_and_ranks_distinct_records(monkeypatch):
    sentences = ["The facility is proposed.", "The facility is planned.", "The facility needs funding."]
    body = " ".join(sentences)
    records = {f"r{i}": record(f"r{i}", body) for i in range(1, 6)}
    passages = [evidence(rid, text, body.index(text), f"e-{rid}-{part}")
                for rid in records for part, text in enumerate(sentences)]
    service = FakeService(records=records, result=Answer(
        status="answered", answer="Funding is still needed.", evidence=passages,
        citations=[Citation(evidence_id="e-r5-2", quote=sentences[2])],
    ))
    bind(monkeypatch, service)
    multi = case(required_record_ids=["r1", "r5"], support_quote=[
        {"record_id": "r1", "quote": sentences[0]}, {"record_id": "r5", "quote": sentences[2]},
    ])
    result = evaluate.run_evaluation([multi], service, paid=True)
    row = result["cases"][0]
    assert len(row["evidence"]) == 15
    assert row["top_five_distinct_record_ids"] == list(records)
    assert row["hit_at_5"] is True
    assert row["evidence_locator_valid"] == {"numerator": 15, "denominator": 15, "rate": 1}
    assert row["citation_locator_valid"]["rate"] == 1
    assert row["support_passage_coverage"] == {"numerator": 2, "denominator": 2, "rate": 1}
    assert row["support_passage_matches"][1]["evidence_ids"] == ["e-r5-2"]


def test_sixth_distinct_record_does_not_count_as_hit_at_five(monkeypatch):
    records = {f"r{i}": record(f"r{i}") for i in range(1, 7)}
    service = FakeService(records=records, result=Answer(
        status="answered", answer="Proposed.", evidence=[evidence(rid) for rid in records],
    ))
    bind(monkeypatch, service)
    sixth = case(required_record_ids=["r6"], support_quote=[
        {"record_id": "r6", "quote": "The facility is proposed."},
    ])
    row = evaluate.run_evaluation([sixth], service, paid=True)["cases"][0]
    assert row["hit_at_5"] is False
    assert row["support_passage_coverage"]["rate"] == 1  # Coverage examines all returned evidence.
    assert row["top_five_distinct_record_ids"] == ["r1", "r2", "r3", "r4", "r5"]


def test_support_passage_requires_whole_quote_from_its_own_record(monkeypatch):
    service = FakeService(records={"r1": record(), "r2": record("r2")}, evidence_rows=[
        evidence(text="The facility"), evidence("r2"),
    ])
    bind(monkeypatch, service)
    result = evaluate.run_evaluation([case()], service)
    row = result["cases"][0]
    assert row["hit_at_5"] is True
    assert row["support_passage_coverage"] == {"numerator": 0, "denominator": 1, "rate": 0}
    assert result["summary"]["native"]["support_passage_coverage"] == row["support_passage_coverage"]


def test_support_passage_rejects_stale_evidence_even_when_quote_matches(monkeypatch):
    stale = evidence().model_copy(update={"version_id": "old-version"})
    service = FakeService(evidence_rows=[stale])
    bind(monkeypatch, service)
    row = evaluate.run_evaluation([case()], service)["cases"][0]
    assert row["support_passage_coverage"]["numerator"] == 0
    assert row["evidence_locator_valid"]["numerator"] == 0


def test_failed_retrieval_keeps_support_quote_in_denominator(monkeypatch):
    service = FakeService()
    bind(monkeypatch, service)
    def failed_search(*args, **kwargs):
        raise RuntimeError("fixture failure")
    service.search = failed_search
    summary = evaluate.run_evaluation([case()], service)["summary"]["native"]
    assert summary["support_passage_coverage"] == {"numerator": 0, "denominator": 1, "rate": 0}


def test_missing_social_is_pending_even_with_paid_flag(monkeypatch):
    service = FakeService()
    bind(monkeypatch, service)
    pending = case(dataset="cross", status="pending_social", filters={"dataset": "all"},
                   required_record_ids=[], support_quote=[])
    result = evaluate.run_evaluation([pending], service, paid=True)
    summary = result["summary"]["cross"]
    assert summary["pending_cases"] == 1 and summary["ready_cases"] == 0
    assert summary["hit_at_5"]["denominator"] == 0 and summary["overall_pass"] is None
    assert summary["support_passage_coverage"]["denominator"] == 0
    assert service.answer_calls == service.search_calls == 0


def test_pending_social_cannot_include_fabricated_gold():
    with pytest.raises(ValidationError, match="invented gold"):
        case(dataset="social", status="pending_social", filters={"dataset": "social"})


def test_lexical_empty_results_do_not_count_as_verified_abstention(monkeypatch):
    service = FakeService(evidence_rows=[])
    bind(monkeypatch, service)
    negative = case(case_type="no_evidence", required_record_ids=[])
    summary = evaluate.run_evaluation([negative], service)["summary"]["native"]
    assert summary["abstention_on_no_evidence"]["denominator"] == 0
    assert summary["abstention_status"] == "not_run_lexical"
    assert summary["support_passage_coverage"]["denominator"] == 0


def test_paid_abstention_and_total_ledger_cost_include_embedding(monkeypatch):
    service = FakeService(result=Answer(status="insufficient_evidence", answer="No audit provided.",
                                       evidence=[evidence()], cost_usd=0.015))
    bind(monkeypatch, service)
    negative = case(case_type="no_evidence", required_record_ids=[])
    result = evaluate.run_evaluation([negative], service, paid=True)
    assert service.answer_calls == 1 and service.search_calls == 0
    assert result["summary"]["native"]["abstention_on_no_evidence"]["rate"] == 1
    assert result["summary"]["native"]["settled_cost_usd"] == 0.016
    assert result["cases"][0]["reported_answer_cost_usd"] == 0.015


def test_service_failure_reason_is_retained_separately_from_abstention(monkeypatch):
    service = FakeService(result=Answer(status="service_unavailable", answer="Unavailable.",
                                       evidence=[evidence()], failure_reason="citation_mismatch"))
    bind(monkeypatch, service)
    negative = case(case_type="no_evidence", required_record_ids=[])
    result = evaluate.run_evaluation([negative], service, paid=True)
    assert result["cases"][0]["failure_reason"] == "citation_mismatch"
    assert result["summary"]["native"]["failure_reasons"] == {"citation_mismatch": 1}
    assert result["summary"]["native"]["abstention_on_no_evidence"]["numerator"] == 0


def test_locatable_quote_does_not_promote_false_statement_to_semantic_pass(monkeypatch):
    service = FakeService(result=Answer(status="answered", answer="This proves the facility is already operating.",
                                       evidence=[evidence()], citations=[Citation(evidence_id="e-r1", quote="is proposed")]))
    bind(monkeypatch, service)
    summary = evaluate.run_evaluation([case()], service, paid=True)["summary"]["native"]
    assert summary["citation_locator_valid"]["rate"] == 1
    assert summary["semantic_support"] == {"status": "pending_human_review", "rate": None}
    assert summary["overall_pass"] is None


@pytest.mark.parametrize("extra_words,expected", [(0, 1), (1, 0)])
def test_citation_length_uses_shared_rag_word_limit(monkeypatch, extra_words, expected):
    quote = " ".join(f"word{i}" for i in range(evaluate.MAX_QUOTE_WORDS + extra_words)) + "."
    service = FakeService(records={"r1": record(body=quote)}, result=Answer(
        status="answered", answer="A unit fixture claim.", evidence=[evidence(text=quote)],
        citations=[Citation(evidence_id="e-r1", quote=quote)],
    ))
    bind(monkeypatch, service)
    long_quote = case(support_quote=[{"record_id": "r1", "quote": quote}])
    result = evaluate.run_evaluation([long_quote], service, paid=True)
    assert result["summary"]["native"]["citation_locator_valid"]["rate"] == expected
    assert result["max_quote_words"] == evaluate.MAX_QUOTE_WORDS


def test_data_change_during_retrieval_invalidates_the_run(monkeypatch):
    service = FakeService()
    bind(monkeypatch, service)
    def changed_search(*args, **kwargs):
        service.version = "dataset-2"
        return [evidence()]
    service.search = changed_search
    with pytest.raises(evaluate.EvaluationInvalid, match="Data changed"):
        evaluate.run_evaluation([case()], service)


def test_explicit_dataset_version_must_match_before_dispatch(monkeypatch):
    service = FakeService()
    bind(monkeypatch, service)
    with pytest.raises(evaluate.EvaluationInvalid, match="--expected-data-version"):
        evaluate.run_evaluation([case()], service, expected_data_version="previous-version")
    assert service.search_calls == service.answer_calls == 0


def test_count_gold_covers_the_whole_filter_set():
    count_case = case(case_type="count", support_quote=[], expected_count=1)
    with pytest.raises(evaluate.EvaluationInvalid, match="count gold is stale"):
        evaluate.validate_gold([count_case], {"r1": record(), "r2": record("r2")},
                               {"case-1": [{"record_id": "r1"}, {"record_id": "r2"}]})


@pytest.mark.parametrize("filename,suite", [("development.jsonl", "development"),
                                           ("acceptance.draft.jsonl", "acceptance_draft")])
def test_real_case_files_preserve_scope_and_draft_status(filename, suite):
    path = Path(__file__).resolve().parents[1] / "eval" / filename
    cases = evaluate.load_cases(path)
    assert len(cases) == 20 and {c.suite for c in cases} == {suite}
    assert sum(c.dataset == "native" and c.case_type == "retrieval" for c in cases) == 8
    assert sum(c.dataset == "native" and c.case_type == "count" for c in cases) == 2
    assert sum(c.dataset == "native" and c.case_type == "no_evidence" for c in cases) == 3
    assert sum(c.dataset == "social" and c.status == "pending_social" for c in cases) == 4
    assert sum(c.dataset == "cross" and c.status == "pending_social" for c in cases) == 3
