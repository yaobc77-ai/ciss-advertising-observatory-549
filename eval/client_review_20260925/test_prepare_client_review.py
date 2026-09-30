"""Synthetic guard fixtures only; no DB, model requests, or evaluation scores."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("prepare_client_review", ROOT / "scripts/prepare_client_review.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def case(rid="new", question="Synthetic unit question about a proposed facility?"):
    return {
        "id": rid, "suite": "acceptance_draft", "dataset": "native", "status": "ready",
        "case_type": "retrieval", "question": question, "filters": {"dataset": "native"},
        "required_record_ids": [rid], "support_quote": [{"record_id": rid, "quote": "A facility is proposed."}],
        "rubric": ["Synthetic unit fixture only."], "selection_note": "Synthetic fixture; never an actual gold question.",
    }


def packet(tmp_path):
    folder = tmp_path / "packet"
    folder.mkdir()
    plan = {"scope": "native", "reviewer": "Unit fixture reviewer", "approval_record": "unit-only",
            "question_source_note": "Synthetic", "acceptance_criteria": "Unit fixture",
            "representative_tasks_approved": True, "not_used_for_tuning": True,
            "article_groups_reviewed": True, "expected_data_version": "a" * 64}
    (folder / "review_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (folder / "questions.jsonl").write_text(json.dumps(case()) + "\n", encoding="utf-8")
    (folder / "article_groups.csv").write_text(
        "record_id,article_group_id,split,review_note\nold,old-group,development,unit\nnew,new-group,holdout,unit\n",
        encoding="utf-8")
    seen = tmp_path / "previous.jsonl"
    seen.write_text(json.dumps(case("old", "A different synthetic unit question?")), encoding="utf-8")
    return folder, [seen]


def test_empty_client_packet_is_pending_without_database(monkeypatch, capsys):
    class NeverConnect:
        def __init__(self, *args):
            raise AssertionError("No database access before prerequisites")
    monkeypatch.setattr(prepare, "Database", NeverConnect)
    assert prepare.main(["--check-sources"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "pending" and result["overall_pass"] is None
    assert "no_client_questions_or_gold" in result["blockers"]


def test_unreviewed_plan_cannot_be_frozen(tmp_path):
    folder, seen = packet(tmp_path)
    plan = json.loads((folder / "review_plan.json").read_text())
    plan["reviewer"] = None
    (folder / "review_plan.json").write_text(json.dumps(plan))
    data = prepare.inspect_packet(folder, seen)
    assert "missing_review:reviewer" in data["blockers"]
    with pytest.raises(prepare.EvaluationInvalid, match="prerequisites"):
        prepare.freeze_packet(data, folder, tmp_path / "frozen", {})
    assert not (tmp_path / "frozen").exists()


def test_seen_question_and_same_article_group_are_rejected(tmp_path):
    folder, seen = packet(tmp_path)
    (folder / "questions.jsonl").write_text(json.dumps(case(question="A different synthetic unit question?")))
    path = folder / "article_groups.csv"
    path.write_text(path.read_text().replace("new-group", "old-group"))
    errors = prepare.inspect_packet(folder, seen)["blockers"]
    assert "question_previously_used:new" in errors and "article_group_leakage" in errors


def test_freeze_requires_source_validation_and_detects_edited_packet(tmp_path):
    folder, seen = packet(tmp_path)
    data = prepare.inspect_packet(folder, seen)
    assert not data["blockers"]
    with pytest.raises(prepare.EvaluationInvalid, match="source_validation_missing"):
        prepare.freeze_packet(data, folder, tmp_path / "frozen", {})
    (folder / "questions.jsonl").write_text(json.dumps(case(question="Changed after validation")))
    with pytest.raises(prepare.EvaluationInvalid, match="packet_changed"):
        prepare.freeze_packet(data, folder, tmp_path / "frozen", {"new": []})


class ReadOnlyFixture:
    def health(self):
        return {"data_version": "a" * 64, "source_data_version": "b" * 32,
                "index_version": "c" * 64, "active_profile": "synthetic-profile"}

    def public_rows(self, filters):
        return [{"record_id": "new"}]


def sources(new_body="A facility is proposed."):
    return {rid: {"record_id": rid, "version_id": f"v-{rid}", "dataset": "native", "body": body,
                  "payload": {"retrievable": True}}
            for rid, body in [("old", "A different previous article."), ("new", new_body)]}


def test_source_quote_must_exist_before_freezing(tmp_path, monkeypatch):
    folder, seen = packet(tmp_path)
    monkeypatch.setattr(prepare, "load_snapshot", lambda db: sources("No project mentioned."))
    with pytest.raises(prepare.EvaluationInvalid, match="exact support quote"):
        prepare.check_sources(prepare.inspect_packet(folder, seen), ReadOnlyFixture())


def test_identical_body_leakage_is_rejected_even_with_different_group_names(tmp_path, monkeypatch):
    folder, seen = packet(tmp_path)
    snapshot = sources()
    snapshot["old"]["body"] = "  A FACILITY is proposed.  "
    monkeypatch.setattr(prepare, "load_snapshot", lambda db: snapshot)
    with pytest.raises(prepare.EvaluationInvalid, match="exact_body_overlap"):
        prepare.check_sources(prepare.inspect_packet(folder, seen), ReadOnlyFixture())


def test_ready_freeze_binds_inputs_and_never_claims_acceptance(tmp_path, monkeypatch):
    folder, seen = packet(tmp_path)
    monkeypatch.setattr(prepare, "load_snapshot", lambda db: sources())
    data = prepare.inspect_packet(folder, seen)
    located = prepare.check_sources(data, ReadOnlyFixture())
    output = tmp_path / "frozen"
    manifest = prepare.freeze_packet(data, folder, output, located)
    assert manifest["overall_pass"] is None and manifest["semantic_acceptance"] == "pending_human_review"
    assert manifest["gold_spans"]["new"][0]["version_id"] == "v-new"
    for name, digest in manifest["input_sha256"].items():
        assert prepare.sha256((output / name).read_bytes()) == digest
    with pytest.raises(FileExistsError):
        prepare.freeze_packet(data, folder, output, located)
