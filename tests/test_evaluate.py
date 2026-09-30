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


def record(rid="r1", body="The facility is proposed.", dataset="native"):
    return {"record_id": rid, "dataset": dataset, "version_id": "v1",
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
        return [{"record_id": rid} for rid, source in self.records.items()
                if filters.dataset in {"all", source["dataset"]}
                and (not filters.record_ids or rid in filters.record_ids)]

    def statistics(self, filters):
        return {"total": len(self.browse(filters))}

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


REVIEWED_FIXTURE_RELEASE = {
    "data_version": "a" * 64, "reviewer": "Synthetic unit-test reviewer",
    "approval_record": "Synthetic unit fixture; no real customer approval",
}


def test_ready_social_requires_an_explicit_reviewed_release():
    with pytest.raises(ValidationError, match="separately reviewed dataset release"):
        case(dataset="social", filters={"dataset": "social"})


@pytest.mark.parametrize("field", ["reviewer", "approval_record"])
def test_reviewed_release_cannot_have_blank_human_reference(field):
    release = {**REVIEWED_FIXTURE_RELEASE, field: " "}
    with pytest.raises(ValidationError, match="reviewer and approval record"):
        case(dataset="social", filters={"dataset": "social"}, reviewed_release=release)


def test_social_gold_validates_its_own_dataset_and_release():
    social = case(dataset="social", filters={"dataset": "social"},
                  reviewed_release=REVIEWED_FIXTURE_RELEASE)
    source = record(dataset="social")
    located = evaluate.validate_gold([social], {"r1": source},
                                    {"case-1": [{"record_id": "r1"}]}, data_version="a" * 64)
    assert located["case-1"][0]["quote"] == source["body"]
    with pytest.raises(evaluate.EvaluationInvalid, match="out-of-scope dataset"):
        evaluate.validate_gold([social], {"r1": record(), "r2": record("r2", dataset="social")},
                               {"case-1": [{"record_id": "r1"}]}, data_version="a" * 64)


def test_native_gold_rejects_a_social_source_even_if_quote_and_id_match():
    with pytest.raises(evaluate.EvaluationInvalid, match="out-of-scope dataset"):
        evaluate.validate_gold([case()], {"r1": record(dataset="social")},
                               {"case-1": [{"record_id": "r1"}]})


def test_cross_gold_requires_both_reviewed_collections():
    cross = case(dataset="cross", filters={"dataset": "all"},
                 reviewed_release=REVIEWED_FIXTURE_RELEASE, required_record_ids=["r1", "r2"],
                 support_quote=[{"record_id": rid, "quote": "The facility is proposed."}
                                for rid in ("r1", "r2")])
    sources = {"r1": record(), "r2": record("r2", dataset="social")}
    rows = {"case-1": [{"record_id": rid} for rid in sources]}
    assert len(evaluate.validate_gold([cross], sources, rows, data_version="a" * 64)["case-1"]) == 2
    sources["r2"] = record("r2")
    sources["r3"] = record("r3", dataset="social")
    with pytest.raises(evaluate.EvaluationInvalid, match="must include both datasets"):
        evaluate.validate_gold([cross], sources, rows, data_version="a" * 64)


def test_reviewed_social_version_mismatch_blocks_search_and_answer(monkeypatch):
    service = FakeService(records={"r1": record(dataset="social")})
    bind(monkeypatch, service)
    social = case(dataset="social", filters={"dataset": "social"},
                  reviewed_release=REVIEWED_FIXTURE_RELEASE)
    with pytest.raises(evaluate.EvaluationInvalid, match="reviewed dataset release"):
        evaluate.run_evaluation([social], service, paid=True)
    assert service.answer_calls == service.search_calls == 0


def test_reviewed_social_case_can_run_scoped_fixture_retrieval(monkeypatch):
    service = FakeService(records={"r1": record(dataset="social")},
                          evidence_rows=[evidence().model_copy(update={"dataset": "social"})])
    service.version = "a" * 64
    bind(monkeypatch, service)
    social = case(dataset="social", filters={"dataset": "social"},
                  reviewed_release=REVIEWED_FIXTURE_RELEASE)
    result = evaluate.run_evaluation([social], service)
    assert result["summary"]["social"]["ready_cases"] == 1
    assert result["summary"]["social"]["evidence_locator_valid"]["rate"] == 1
    assert result["cases"][0]["reviewed_release"] == REVIEWED_FIXTURE_RELEASE
    assert service.answer_calls == 0


def test_evidence_from_wrong_collection_does_not_pass_locator_checks(monkeypatch):
    service = FakeService(records={"r1": record(), "r2": record("r2", dataset="social")},
                          evidence_rows=[evidence("r2").model_copy(update={"dataset": "social"})])
    bind(monkeypatch, service)
    row = evaluate.run_evaluation([case()], service)["cases"][0]
    assert row["evidence_locator_valid"] == {"numerator": 0, "denominator": 1, "rate": 0}


def statistics_result(filters=None, total=1, dataset="native", **changes):
    values = {
        "status": "answered", "answer_mode": "statistics", "answer": "A database result.",
        "structured_result": {"method": "database", "kind": "count", "filters": filters or {"dataset": dataset},
                              "collections": [{"dataset": dataset, "total": total}]},
        "research_trace": {"route": "statistics", "fixture": True},
    }
    values.update(changes)
    return Answer(**values)


def test_count_baseline_keeps_database_only_even_with_paid_flag(monkeypatch):
    service = FakeService()
    bind(monkeypatch, service)
    count = case(case_type="count", support_quote=[], expected_count=1)
    result = evaluate.run_evaluation([count], service, paid=True)
    assert service.answer_calls == service.search_calls == 0
    assert result["cases"][0]["execution_status"] == "count_only"
    assert result["cases"][0]["count_exact"] is True
    assert result["count_evaluation_mode"] == "database_only"
    assert result["summary"]["native"]["answer_count_exact"]["denominator"] == 0


def test_count_answer_opt_in_requires_paid_before_dispatch(monkeypatch):
    service = FakeService()
    bind(monkeypatch, service)
    count = case(case_type="count", support_quote=[], expected_count=1)
    with pytest.raises(evaluate.EvaluationInvalid, match="explicit --paid"):
        evaluate.run_evaluation([count], service, answer_counts=True)
    assert service.answer_calls == service.search_calls == 0


def test_count_answer_measures_actual_service_path_and_keeps_ledger(monkeypatch):
    service = FakeService(result=statistics_result())
    service.settings.research_agent_enabled = True
    bind(monkeypatch, service)
    count = case(case_type="count", support_quote=[], expected_count=1)
    result = evaluate.run_evaluation([count], service, paid=True, answer_counts=True)
    row = result["cases"][0]
    assert service.answer_calls == 1 and service.search_calls == 0
    assert row["count_exact"] is True and row["answer_count_exact"] is True
    assert row["research_trace"] == {"route": "statistics", "fixture": True}
    assert row["expected_collection_counts"] == row["answer_collection_counts"] == {"native": 1}
    assert result["count_evaluation_mode"] == "service_answer" and result["research_agent_enabled"] is True
    assert result["summary"]["native"]["answer_count_exact"] == {"numerator": 1, "denominator": 1, "rate": 1}
    assert result["summary"]["native"]["settled_cost_usd"] == 0.016
    assert result["summary"]["native"]["semantic_support"]["status"] == "pending_human_review"


def test_count_answer_equal_number_with_wrong_scope_does_not_pass(monkeypatch):
    service = FakeService(result=statistics_result(filters={"dataset": "native", "sponsors": ["unrequested"]}))
    bind(monkeypatch, service)
    count = case(case_type="count", support_quote=[], expected_count=1)
    row = evaluate.run_evaluation([count], service, paid=True, answer_counts=True)["cases"][0]
    assert row["count_exact"] is True
    assert row["answer_count_scope_valid"] is False and row["answer_count_exact"] is False


@pytest.mark.parametrize("answer", [
    statistics_result(total=2),
    statistics_result(dataset="social"),
    Answer(status="answered", answer="There is 1 ad.", answer_mode="rag"),
    statistics_result(status="service_unavailable", failure_reason="research_agent_unavailable"),
])
def test_bad_count_answer_retains_failure_in_the_denominator(monkeypatch, answer):
    service = FakeService(result=answer)
    bind(monkeypatch, service)
    count = case(case_type="count", support_quote=[], expected_count=1)
    result = evaluate.run_evaluation([count], service, paid=True, answer_counts=True)
    assert result["summary"]["native"]["answer_count_exact"] == {"numerator": 0, "denominator": 1, "rate": 0}
    assert service.answer_calls == 1
    assert result["cases"][0]["count_exact"] is True


def test_cross_count_keeps_native_and_social_totals_separate(monkeypatch):
    sources = {"r1": record(), "r2": record("r2", dataset="social")}
    answer = statistics_result(filters={"dataset": "all"})
    answer.structured_result["collections"] = [{"dataset": "native", "total": 1}, {"dataset": "social", "total": 1}]
    service = FakeService(records=sources, result=answer)
    service.version = "a" * 64
    bind(monkeypatch, service)
    cross = case(case_type="count", dataset="cross", filters={"dataset": "all"},
                 reviewed_release=REVIEWED_FIXTURE_RELEASE, required_record_ids=["r1", "r2"],
                 support_quote=[], expected_count=2)
    row = evaluate.run_evaluation([cross], service, paid=True, answer_counts=True)["cases"][0]
    assert row["count_exact"] is True and row["answer_count_exact"] is True
    assert row["actual_collection_counts"] == {"native": 1, "social": 1}
    assert row["expected_collection_counts"] == {"native": 1, "social": 1}
    # Swapping equal combined totals between units must still fail.
    answer.structured_result["collections"] = [{"dataset": "native", "total": 2}, {"dataset": "social", "total": 0}]
    row = evaluate.run_evaluation([cross], service, paid=True, answer_counts=True)["cases"][0]
    assert row["answer_count_exact"] is False


def test_cli_count_answer_flag_without_paid_is_rejected(monkeypatch):
    monkeypatch.setattr("sys.argv", ["observatory.evaluate", "--answer-counts"])
    with pytest.raises(SystemExit) as exc:
        evaluate.main()
    assert exc.value.code == 2


@pytest.mark.parametrize("dataset,available", [
    ("social", {"r1": record()}),
    ("social", {}),
    ("cross", {"r1": record()}),
    ("cross", {"r1": record(dataset="social")}),
])
@pytest.mark.parametrize("case_type", ["count", "no_evidence"])
def test_missing_active_collection_cannot_pass_as_a_reviewed_empty_result(monkeypatch, dataset, available, case_type):
    service = FakeService()
    service.records = available
    service.version = "a" * 64
    bind(monkeypatch, service)
    empty_case = case(case_type=case_type, dataset=dataset,
                      filters={"dataset": "all" if dataset == "cross" else dataset,
                               "record_ids": ["not-selected"]},
                      reviewed_release=REVIEWED_FIXTURE_RELEASE, required_record_ids=[], support_quote=[],
                      expected_count=0 if case_type == "count" else None)
    with pytest.raises(evaluate.EvaluationInvalid, match="no active records"):
        evaluate.run_evaluation([empty_case], service, paid=True, answer_counts=True)
    assert service.answer_calls == service.search_calls == 0


@pytest.mark.parametrize("dataset", ["social", "cross"])
@pytest.mark.parametrize("case_type", ["count", "no_evidence"])
def test_connected_collections_allow_legitimately_empty_filtered_scope(monkeypatch, dataset, case_type):
    sources = {"r1": record(), "r2": record("r2", dataset="social")}
    service = FakeService(records=sources, evidence_rows=[])
    service.version = "a" * 64
    bind(monkeypatch, service)
    empty_case = case(case_type=case_type, dataset=dataset,
                      filters={"dataset": "all" if dataset == "cross" else dataset,
                               "record_ids": ["not-selected"]},
                      reviewed_release=REVIEWED_FIXTURE_RELEASE, required_record_ids=[], support_quote=[],
                      expected_count=0 if case_type == "count" else None)
    row = evaluate.run_evaluation([empty_case], service)["cases"][0]
    if case_type == "count":
        assert row["count_exact"] is True
        assert row["actual_collection_counts"] == ({"social": 0} if dataset == "social" else {"native": 0, "social": 0})
    else:
        assert row["execution_status"] == "retrieval_only"
        assert row["evidence"] == []
    assert service.answer_calls == 0
