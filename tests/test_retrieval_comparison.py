"""Offline comparison-runner regressions: no PostgreSQL instance or model needed."""

import importlib.util
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory.db import Database, digest
from observatory.evaluate import (
    EvaluationCase,
    EvaluationInvalid,
    load_cases,
    validate_gold,
)
from observatory.models import Filters

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("retrieval_comparison", ROOT / "scripts/compare_retrieval_chunks.py")
comparison = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = comparison
SPEC.loader.exec_module(comparison)


def source(body="The facility is proposed. More testing is necessary.", rid="r1", **payload):
    return {"record_id": rid, "version_id": f"v-{rid}", "dataset": "native", "body": body,
            "payload": {"retrievable": True, **payload}}


def case(**changes):
    values = {
        "id": "dev-01", "suite": "development", "dataset": "native", "status": "ready",
        "case_type": "retrieval", "question": "What does the facility propose?",
        "filters": {"dataset": "native"}, "required_record_ids": ["r1"],
        "support_quote": [{"record_id": "r1", "quote": "The facility is proposed."}],
        "rubric": ["Retain the proposed status."], "selection_note": "Offline fixture, not research evidence.",
    }
    values.update(changes)
    return EvaluationCase.model_validate(values)


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


@pytest.mark.parametrize("variant", comparison.VARIANTS, ids=lambda value: value.name)
def test_derived_chunks_preserve_source_gaps_unicode_and_version(variant):
    body = "First body sentence. " * 75 + "NAVIGATION" + "中文🙂 café 2.5 million. " * 90
    start = body.index("NAVIGATION")
    end = start + len("NAVIGATION")
    record = source(body, retrieval_ranges=[[0, start], [end, len(body)]], retrieval_end=4)
    snapshot = {"r1": record, "r2": source("Never retrievable.", "r2", retrievable=False)}
    chunks = comparison.build_chunks(snapshot, set(snapshot), variant)
    assert chunks and all(row["record_id"] == "r1" for row in chunks)
    assert all(comparison.valid_locator(row, snapshot, variant.max_tokens) for row in chunks)
    assert all(row["end"] <= start or row["start"] >= end for row in chunks)
    covered = {i for row in chunks for i in range(row["start"], row["end"])}
    assert all(i in covered for i, char in enumerate(body) if not char.isspace() and not start <= i < end)
    assert not comparison.valid_locator({**chunks[0], "version_id": "old-version"}, snapshot, variant.max_tokens)
    assert not comparison.valid_locator({**chunks[0], "text": "paraphrase"}, snapshot, variant.max_tokens)


def test_full_support_must_fit_one_chunk_and_record_hit_requires_all_records():
    body = "The facility is proposed. More testing is necessary."
    snapshot = {"r1": source(body), "r2": source(body, "r2")}
    variant = comparison.Variant("small", "legacy", 7, 0)
    chunks = comparison.build_chunks(snapshot, {"r1"}, variant)
    request = case(required_record_ids=["r1", "r2"], support_quote=[
        {"record_id": rid, "quote": body} for rid in snapshot
    ])
    located = validate_gold([request], snapshot, {request.id: [{"record_id": rid} for rid in snapshot]})
    result = comparison.score_case(request, chunks, located[request.id], snapshot, variant)
    assert result["record_hit_at_5"] is False
    assert result["complete_support_quote_coverage"] == {"numerator": 0, "denominator": 2, "rate": 0}
    assert result["returned_token_count"] == sum(row["token_count"] for row in chunks)
    assert result["semantic_correct"] is None
    assert result["locator_checks"]["rate"] == 1


def test_overlap_counts_only_same_source_version_and_retained_interval():
    rows = [
        {"record_id": "r1", "version_id": "v1", "retained_range_index": 0, "start": 0, "end": 10},
        {"record_id": "r1", "version_id": "v1", "retained_range_index": 0, "start": 5, "end": 15},
        {"record_id": "r2", "version_id": "v2", "retained_range_index": 0, "start": 0, "end": 10},
    ]
    assert comparison.interval_overlap(rows) == {
        "returned_characters": 30, "unique_source_characters": 25, "redundant_characters": 5, "ratio": 1 / 6,
    }
    assert comparison.interval_overlap([])["ratio"] is None


@pytest.mark.parametrize("question", [
    "How does the Baytown hydrogen facility link natural gas and CCS?",
    "the and is", "中文问题 CO2 2.5 for these with X-ray", " ".join(f"token{i}" for i in range(60)),
])
def test_query_and_single_channel_rrf_match_current_database_search(question):
    captured = []
    rows = [{"evidence_id": f"e{i}", "record_id": f"r{i // 2}", "version_id": "v1", "dataset": "native",
             "title": "Test", "text": "Body", "start": 20 - i, "end": 24 - i, "paragraph_ids": ["p1"], "score": 1}
            for i in range(16)]

    class Connection:
        def commit(self):
            pass

        def execute(self, sql, params=None):
            if params:
                captured.append(params)
            return Rows(rows if params else [])

    repository = Database("")
    repository.connect = lambda **kwargs: nullcontext(Connection())
    existing = repository.search(question, Filters(), chunks_per_record=3)
    query = comparison.lexical_query(question)
    if not query:
        assert existing == [] and captured == []
    else:
        assert captured[0][0] == query == captured[0][-1]
        derived = comparison.select_records([{**row, "chunk_id": row["evidence_id"]} for row in rows], chunks_per_record=3)
        assert [(row.evidence_id, row.score) for row in existing] == [(row["chunk_id"], row["score"]) for row in derived]
        assert len({row["record_id"] for row in derived}) == 5


def test_cte_uses_existing_metadata_predicate_and_postgres_english_rank():
    captured = []

    class Connection:
        def execute(self, sql, params):
            captured.append((sql, params))
            return Rows([])

    filters = Filters(record_ids=["r1"], sponsors=["exxonmobil"], labels=["label"], include_unknown_dates=False)
    evidence, matched = comparison.retrieve(Connection(), [{"record_id": "r1"}], "blue hydrogen", filters, chunks_per_record=1)
    sql, params = captured[0]
    where, filter_params = Database.where(filters)
    assert where in sql and "jsonb_to_recordset" in sql and "to_tsvector('english'" in sql
    assert "ts_rank_cd" in sql and "ORDER BY lexical_score DESC,c.chunk_id LIMIT 50" in sql
    assert params[2:-1] == filter_params
    assert evidence == [] and matched == 0


@pytest.mark.parametrize("reported_readonly,accepted", [("on", True), ("off", False)])
def test_connection_enforces_read_only_before_first_statement(monkeypatch, reported_readonly, accepted):
    captured = {}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql):
            assert self.read_only is True
            assert self.isolation_level == comparison.IsolationLevel.REPEATABLE_READ
            if "transaction_read_only" in sql:
                return Rows([{"transaction_read_only": reported_readonly}])
            return Rows([{"transaction_isolation": "repeatable read"}])

    def connect(url, **kwargs):
        captured.update(kwargs)
        return Connection()

    monkeypatch.setattr(comparison.psycopg, "connect", connect)
    if accepted:
        with comparison.readonly_repository("never-printed") as repository:
            assert repository.connect().__enter__() is repository.connection
            with pytest.raises(EvaluationInvalid, match="outside"):
                repository.connect(vector=True)
    else:
        with pytest.raises(EvaluationInvalid, match="read-only"):
            with comparison.readonly_repository("never-printed"):
                pytest.fail("Unsafe transaction must not be yielded")
    assert "default_transaction_read_only=on" in captured["options"]


def test_persisted_legacy_baseline_comparison_rejects_geometry_or_text_changes():
    chunks = comparison.build_chunks({"r1": source()}, {"r1"}, comparison.VARIANTS[2])
    assert comparison.compare_legacy_baseline(
        chunks, chunks, active_profile=comparison.LEGACY_PROFILE,
    )["baseline_matches_current_index"]
    changed = [{**chunks[0], "text_hash": digest("new text")}]
    result = comparison.compare_legacy_baseline(chunks, changed, active_profile=comparison.LEGACY_PROFILE)
    assert not result["baseline_matches_persisted_legacy_index"]
    assert not result["baseline_matches_current_index"]
    assert result["persisted_legacy_missing_or_different_in_derived"] == 1


def test_translation_pairs_keep_original_gold_and_filters_and_are_not_heldout():
    originals = load_cases(ROOT / "eval/development.jsonl")
    before = [item.model_dump() for item in originals]
    pairs, origins = comparison.diagnostics(originals)
    assert len(pairs) == len(origins) == 8
    by_id = {item.id: item for item in originals}
    for pair in pairs:
        origin = origins[pair.id]
        original = by_id[origin["source_case_id"]]
        assert pair.support_quote == original.support_quote
        assert pair.filters == original.filters and pair.required_record_ids == original.required_record_ids
        assert pair.question != original.question
        assert origin["independent_heldout"] is False and "translation" in origin["kind"]
    assert [item.model_dump() for item in originals] == before


@pytest.mark.parametrize("active_profile", [comparison.LEGACY_PROFILE, "sentence600-v1"])
def test_end_to_end_orchestration_keeps_groups_counts_and_pending_separate(monkeypatch, active_profile):
    snapshot = {"r1": source()}
    baseline = comparison.build_chunks(snapshot, {"r1"}, comparison.VARIANTS[2])
    calls = []

    class Repository:
        connection = None

        def __init__(self):
            self.connection = self

        def execute(self, sql, params=None):
            calls.append(sql)
            if "FROM chunks" in sql:
                # Both profiles can coexist for one source version. A scan of
                # chunks alone would contaminate the persisted legacy baseline.
                assert "JOIN chunk_profile_membership" in sql
                assert "m.profile_id=%s" in sql and params == (comparison.LEGACY_PROFILE,)
                return Rows(baseline)
            if "FROM embeddings" in sql:
                return Rows([{"text_hash": baseline[0]["text_hash"]}])
            raise AssertionError(f"Unexpected SQL: {sql}")

        def health(self):
            return {"data_version": "test-snapshot", "source_data_version": "test-source",
                    "active_profile": active_profile, "chunks": 1, "record_counts": {"native": 1}}

        def public_rows(self, filters):
            return [{"record_id": "r1", "title": "Fixture"}]

    monkeypatch.setattr(comparison, "load_snapshot", lambda repository: snapshot)
    monkeypatch.setattr(comparison, "retrieve", lambda connection, chunks, question, filters, **kwargs: (chunks, len(chunks)))
    groups = {
        "development": [case(), case(id="count", case_type="count", support_quote=[], expected_count=1),
                        case(id="pending", dataset="social", status="pending_social", filters={"dataset": "social"}, required_record_ids=[], support_quote=[])],
        "pdf265_smoke": [case(id="smoke")],
    }
    result = comparison.compare(Repository(), groups, expected_data_version="test-snapshot")
    assert len(result["variants"]) == 4
    persisted = result["legacy600_baseline"]
    assert persisted["scope"] == "persisted_legacy_baseline"
    assert persisted["baseline_matches_persisted_legacy_index"]
    assert persisted["persisted_legacy_chunks"] == len(baseline)
    is_current = active_profile == comparison.LEGACY_PROFILE
    assert persisted["baseline_is_current_index"] is is_current
    assert persisted["baseline_matches_current_index"] is (True if is_current else None)
    assert result["current_active_profile"] == active_profile
    assert result["source_data_version"] == "test-source"
    assert result["retrieval_profile"]["index_writes"] is False
    for variant in result["variants"]:
        development = variant["groups"]["development"]
        assert development["summary"]["record_hit_at_5"]["denominator"] == 1
        assert development["summary"]["case_status_counts"]["pending_social"] == 1
        assert development["summary"]["count_exact"]["rate"] == 1
        assert development["summary"]["overall_pass"] is None
        assert variant["groups"]["pdf265_smoke"]["summary"]["record_hit_at_5"]["denominator"] == 1
        assert variant["existing_embedding_cache"]["hybrid_status"] == "not_run"
    assert len(calls) == 2


def test_output_refuses_overwrite_and_cli_hides_connection_error(monkeypatch, tmp_path, capsys):
    output = tmp_path / "result.json"
    comparison.write_report(output, {"state": "original"})
    with pytest.raises(FileExistsError):
        comparison.write_report(output, {"state": "replacement"})
    assert "original" in output.read_text()
    monkeypatch.setattr(comparison.Settings, "from_env", lambda: SimpleNamespace(database_url="private", embedding_model="test"))

    def reject(url):
        raise comparison.psycopg.OperationalError("password=DO_NOT_EXPOSE")

    monkeypatch.setattr(comparison, "readonly_repository", reject)
    with pytest.raises(SystemExit) as exit_info:
        comparison.main(["--output", str(tmp_path / "failed.json")])
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "OperationalError" in captured.err and "DO_NOT_EXPOSE" not in captured.err
    assert not (tmp_path / "failed.json").exists()
