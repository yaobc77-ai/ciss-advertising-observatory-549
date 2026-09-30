"""Frozen-input engineering checks using synthetic files and an offline service.

These fixtures are not real customer questions, released social data, semantic
reviews or an acceptance run. No production database or model is contacted.
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from observatory import evaluate
from observatory.config import Settings
from observatory.models import Answer, Evidence

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_frozen_fixture", ROOT / "scripts/prepare_client_review.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)

IDENTITY = {"data_version": "a" * 64, "source_data_version": "b" * 32,
            "index_version": "c" * 64, "active_profile": "synthetic-profile"}
RELEASE = {"data_version": IDENTITY["data_version"], "reviewer": "Synthetic fixture reviewer",
           "approval_record": "Synthetic fixture only; no customer decision"}


def source(rid, dataset="native"):
    body = {"old": "A previously used unrelated fixture article.",
            "n1": "A native facility is proposed. It is not yet operating.",
            "s1": "A social facility is planned. This is a synthetic fixture."}[rid]
    return {"record_id": rid, "dataset": dataset, "version_id": f"v-{rid}",
            "body": body, "payload": {"retrievable": True, "countable": True}}


def case(dataset="native", *, kind="retrieval", status="ready", case_id=None):
    records = {"native": ["n1"], "social": ["s1"], "cross": ["n1", "s1"]}[dataset]
    quotes = {"n1": "A native facility is proposed.", "s1": "A social facility is planned."}
    values = {"id": case_id or f"fixture-{dataset}-{kind}", "suite": "acceptance_draft",
              "dataset": dataset, "status": status, "case_type": kind,
              "question": f"Synthetic {dataset} {kind} question?",
              "filters": {"dataset": "all" if dataset == "cross" else dataset, "record_ids": records},
              "required_record_ids": records if kind != "no_evidence" else [],
              "support_quote": [{"record_id": rid, "quote": quotes[rid]} for rid in records] if kind == "retrieval" else [],
              "expected_count": len(records) if kind == "count" else None,
              "rubric": ["Synthetic engineering fixture; human semantic review remains pending."],
              "selection_note": "Generated fixture for guards, never a real holdout task."}
    if status == "pending_social":
        values.update(required_record_ids=[], support_quote=[], expected_count=None)
        values["filters"]["record_ids"] = []
    elif dataset != "native":
        values["reviewed_release"] = RELEASE
    return values


class OfflineService:
    def __init__(self, records):
        self.records = records
        self.identity = dict(IDENTITY)
        self.settings = Settings()
        self.calls = {name: 0 for name in ("health", "browse", "search", "answer", "statistics")}
        self.db = SimpleNamespace(health=self.health, public_rows=self.browse, validate_evidence=self.valid_evidence)

    def health(self):
        self.calls["health"] += 1
        return dict(self.identity)

    def browse(self, filters):
        self.calls["browse"] += 1
        return [{"record_id": rid} for rid, record in self.records.items()
                if filters.dataset in {"all", record["dataset"]}
                and (not filters.record_ids or rid in filters.record_ids)]

    def statistics(self, filters):
        self.calls["statistics"] += 1
        return {"total": len(self.browse(filters))}

    def search(self, question, filters, limit=5):
        self.calls["search"] += 1
        return [Evidence(evidence_id=f"e-{rid}", record_id=rid, version_id=self.records[rid]["version_id"],
                         dataset=self.records[rid]["dataset"], title="Synthetic fixture",
                         text=self.records[rid]["body"], start=0, end=len(self.records[rid]["body"]))
                for rid in (row["record_id"] for row in self.browse(filters))][:limit]

    def answer(self, *args):
        self.calls["answer"] += 1
        return Answer(status="insufficient_evidence", answer="Synthetic abstention fixture.")

    def valid_evidence(self, evidence):
        return evidence.text == self.records[evidence.record_id]["body"][evidence.start:evidence.end]


def packet(tmp_path, cases, scope):
    folder = tmp_path / "packet"
    folder.mkdir()
    plan = {"scope": scope, "reviewer": "Synthetic fixture reviewer", "approval_record": RELEASE["approval_record"],
            "question_source_note": "Synthetic fixture; no real user research",
            "acceptance_criteria": "Engineering guards only; no semantic acceptance",
            "representative_tasks_approved": True, "not_used_for_tuning": True, "article_groups_reviewed": True,
            "expected_data_version": IDENTITY["data_version"]}
    (folder / "review_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (folder / "questions.jsonl").write_text("".join(json.dumps(item) + "\n" for item in cases), encoding="utf-8")
    (folder / "article_groups.csv").write_text(
        "record_id,article_group_id,split\nold,old-group,development\nn1,native-holdout,holdout\ns1,social-holdout,holdout\n",
        encoding="utf-8")
    seen = tmp_path / "seen.jsonl"
    previous = case(case_id="old")
    previous.update(question="A previously used synthetic question?", required_record_ids=["old"],
                    support_quote=[{"record_id": "old", "quote": "A previously used unrelated fixture article."}])
    seen.write_text(json.dumps(previous) + "\n", encoding="utf-8")
    return folder, [seen]


def frozen_fixture(tmp_path, monkeypatch, cases=None, scope="native", records=None):
    cases = cases if cases is not None else [case()]
    records = records if records is not None else {"old": source("old"), "n1": source("n1"), "s1": source("s1", "social")}
    service = OfflineService(records)
    monkeypatch.setattr(prepare, "load_snapshot", lambda db: service.records)
    monkeypatch.setattr(evaluate, "load_snapshot", lambda db: service.records)
    monkeypatch.setattr(evaluate, "ledger_usage", lambda db, visitor: {
        "status": "synthetic_fixture", "settled_usd": 0, "unresolved_reserved_usd": 0})
    folder, seen = packet(tmp_path, cases, scope)
    groups = folder / "article_groups.csv"
    groups.write_text("\n".join(line for line in groups.read_text(encoding="utf-8").splitlines()
                               if line.startswith("record_id,") or line.split(",")[0] in records) + "\n", encoding="utf-8")
    inspected = prepare.inspect_packet(folder, seen)
    assert inspected["blockers"] == []
    located = prepare.check_sources(inspected, service.db)
    output = tmp_path / "frozen"
    manifest = prepare.freeze_packet(inspected, folder, output, located)
    service.calls = {name: 0 for name in service.calls}
    return output, manifest, evaluate.load_cases(output / "questions.jsonl"), service


def run(output, cases, service, **options):
    return evaluate.run_evaluation(cases, service, frozen_manifest=output / "manifest.json", **options)


def rewrite_manifest(output, transform):
    path = output / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    transform(value)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize("scope", ["native", "social", "cross"])
def test_reviewed_collection_freeze_is_consumed_without_claiming_acceptance(tmp_path, monkeypatch, scope):
    output, manifest, cases, service = frozen_fixture(tmp_path, monkeypatch, [case(scope)], scope)
    assert manifest["scope"] == scope and manifest["snapshot_identity"] == IDENTITY
    for name, expected in manifest["input_sha256"].items():
        assert evaluate.file_digest((output / name).read_bytes()) == expected
    result = run(output, cases, service)
    assert result["gold_status"] == "frozen_inputs_verified"
    assert result["frozen_inputs"]["semantic_acceptance"] == "pending_human_review"
    assert result["frozen_inputs"]["overall_pass"] is None
    assert result["summary"][scope]["hit_at_5"]["denominator"] == 1
    assert result["summary"][scope]["semantic_support"]["rate"] is None
    assert service.calls["answer"] == 0


def test_cross_packet_preserves_dataset_denominators_and_missing_social_pending(tmp_path, monkeypatch):
    output, _, cases, service = frozen_fixture(
        tmp_path, monkeypatch, [case(), case("social", status="pending_social"), case("cross", status="pending_social")],
        "cross", records={"old": source("old"), "n1": source("n1")})
    result = run(output, cases, service)
    assert result["summary"]["native"]["hit_at_5"]["denominator"] == 1
    for scope in ("social", "cross"):
        summary = result["summary"][scope]
        assert summary["pending_cases"] == 1 and summary["ready_cases"] == 0
        assert summary["hit_at_5"]["denominator"] == 0 and summary["overall_pass"] is None
    assert service.calls["search"] == 1 and service.calls["answer"] == 0


@pytest.mark.parametrize("kind", ["count", "no_evidence"])
def test_frozen_special_cases_do_not_manufacture_semantic_acceptance(tmp_path, monkeypatch, kind):
    output, _, cases, service = frozen_fixture(tmp_path, monkeypatch, [case(kind=kind)])
    result = run(output, cases, service)
    summary = result["summary"]["native"]
    assert summary["semantic_support"] == {"status": "pending_human_review", "rate": None}
    assert summary["overall_pass"] is None
    if kind == "count":
        assert summary["count_exact"]["rate"] == 1 and summary["answer_count_exact"]["denominator"] == 0
    else:
        assert summary["abstention_on_no_evidence"]["denominator"] == 0
        assert summary["abstention_status"] == "not_run_lexical"


@pytest.mark.parametrize("name", ["questions.jsonl", "review_plan.json", "article_groups.csv", "reviewed_supports.json",
                                 "previously_used_cases/0001.jsonl"])
def test_exact_frozen_file_mutation_blocks_all_service_calls(tmp_path, monkeypatch, name):
    output, _, cases, service = frozen_fixture(tmp_path, monkeypatch)
    path = output / name
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(evaluate.EvaluationInvalid, match="Frozen input changed"):
        run(output, cases, service, paid=True)
    assert sum(service.calls.values()) == 0


@pytest.mark.parametrize("field", ["question", "support_quote", "expected_count", "filters", "reviewed_release"])
def test_in_memory_case_cannot_bypass_frozen_questions(tmp_path, monkeypatch, field):
    output, _, cases, service = frozen_fixture(tmp_path, monkeypatch)
    changes = {"question": "Altered question", "support_quote": [], "expected_count": 999,
               "filters": cases[0].filters.model_copy(update={"dataset": "all"}),
               "reviewed_release": evaluate.ReviewedDatasetRelease.model_validate(RELEASE)}
    altered = cases[0].model_copy(update={field: changes[field]})
    with pytest.raises(evaluate.EvaluationInvalid, match="Frozen case identity"):
        run(output, [altered], service, paid=True)
    assert sum(service.calls.values()) == 0


@pytest.mark.parametrize("key,value", [("format_version", 1), ("scope", "social"), ("reviewer", "Other reviewer"),
                                     ("overall_pass", True), ("case_ids", ["missing-case"])])
def test_manifest_cannot_change_format_scope_review_or_verdict(tmp_path, monkeypatch, key, value):
    output, _, cases, service = frozen_fixture(tmp_path, monkeypatch)
    rewrite_manifest(output, lambda manifest: manifest.update({key: value}))
    with pytest.raises(evaluate.EvaluationInvalid):
        run(output, cases, service, paid=True)
    assert sum(service.calls.values()) == 0


def test_support_count_contract_is_checked_even_if_its_file_hash_is_replaced(tmp_path, monkeypatch):
    output, _, cases, service = frozen_fixture(tmp_path, monkeypatch, [case(kind="count")])
    path = output / "reviewed_supports.json"
    supports = json.loads(path.read_text(encoding="utf-8"))
    supports["cases"][0]["expected_count"] = 7
    path.write_text(json.dumps(supports), encoding="utf-8")
    rewrite_manifest(output, lambda manifest: manifest["input_sha256"].update({path.name: evaluate.file_digest(path.read_bytes())}))
    with pytest.raises(evaluate.EvaluationInvalid, match="count/support/review binding"):
        run(output, cases, service, paid=True)
    assert sum(service.calls.values()) == 0


@pytest.mark.parametrize("field", list(IDENTITY))
def test_changed_collection_or_index_blocks_dispatch_before_source_loading(tmp_path, monkeypatch, field):
    output, _, cases, service = frozen_fixture(tmp_path, monkeypatch)
    service.identity[field] = "changed-profile" if field == "active_profile" else "d" * 64
    monkeypatch.setattr(evaluate, "load_snapshot", lambda db: pytest.fail("Stale snapshot must not be loaded"))
    with pytest.raises(evaluate.EvaluationInvalid, match="differs from the frozen snapshot"):
        run(output, cases, service, paid=True)
    assert service.calls["browse"] == service.calls["answer"] == service.calls["search"] == service.calls["statistics"] == 0


@pytest.mark.parametrize("change", ["version", "body", "metadata", "count_set", "locator"])
def test_stale_original_bindings_fail_before_search_answer_or_statistics(tmp_path, monkeypatch, change):
    output, _, cases, service = frozen_fixture(tmp_path, monkeypatch, [case(kind="count" if change == "count_set" else "retrieval")])
    if change == "version":
        service.records["n1"]["version_id"] = "v-new"
    elif change == "body":
        service.records["n1"]["body"] += " New ending."
    elif change == "metadata":
        service.records["n1"]["payload"]["sponsor"] = "A changed sponsor"
    elif change == "count_set":
        service.browse = lambda filters: []
    else:
        service.records["n1"]["body"] = "Added introduction. " + service.records["n1"]["body"]
    with pytest.raises(evaluate.EvaluationInvalid):
        run(output, cases, service, paid=True)
    assert service.calls["answer"] == service.calls["search"] == service.calls["statistics"] == 0


def test_profile_change_during_run_invalidates_frozen_evaluation(tmp_path, monkeypatch):
    output, _, cases, service = frozen_fixture(tmp_path, monkeypatch)
    original_search = service.search
    def changed(*args, **kwargs):
        result = original_search(*args, **kwargs)
        service.identity["active_profile"] = "another-profile"
        return result
    service.search = changed
    with pytest.raises(evaluate.EvaluationInvalid, match="Source/index/profile changed"):
        run(output, cases, service)


def test_manifest_change_during_run_is_rejected(tmp_path, monkeypatch):
    output, _, cases, service = frozen_fixture(tmp_path, monkeypatch)
    original_search = service.search
    def changed(*args, **kwargs):
        result = original_search(*args, **kwargs)
        rewrite_manifest(output, lambda manifest: manifest.update(created_at="changed-after-dispatch"))
        return result
    service.search = changed
    with pytest.raises(evaluate.EvaluationInvalid, match="manifest changed during evaluation"):
        run(output, cases, service)


def test_cli_verifies_frozen_files_before_constructing_service(tmp_path, monkeypatch):
    output, _, _, _ = frozen_fixture(tmp_path, monkeypatch)
    (output / "reviewed_supports.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(evaluate, "Service", lambda settings: pytest.fail("Service must not be constructed"))
    with pytest.raises(evaluate.EvaluationInvalid, match="Frozen input changed"):
        evaluate.main(["--frozen-manifest", str(output / "manifest.json"), "--paid"])


def test_freeze_rejects_missing_collection_identity(tmp_path, monkeypatch):
    folder, seen = packet(tmp_path, [case()], "native")
    service = OfflineService({"old": source("old"), "n1": source("n1"), "s1": source("s1", "social")})
    service.identity.pop("index_version")
    monkeypatch.setattr(prepare, "load_snapshot", lambda db: service.records)
    with pytest.raises(evaluate.EvaluationInvalid, match="complete source/index/profile identity"):
        prepare.check_sources(prepare.inspect_packet(folder, seen), service.db)


def test_ready_social_without_active_formal_collection_cannot_be_frozen(tmp_path, monkeypatch):
    folder, seen = packet(tmp_path, [case("social", kind="no_evidence")], "social")
    # Keep the grouping file valid for the available native records only.
    groups = folder / "article_groups.csv"
    groups.write_text("record_id,article_group_id,split\nold,old-group,development\nn1,native-holdout,holdout\n", encoding="utf-8")
    service = OfflineService({"old": source("old"), "n1": source("n1")})
    monkeypatch.setattr(prepare, "load_snapshot", lambda db: service.records)
    inspected = prepare.inspect_packet(folder, seen)
    assert inspected["blockers"] == []
    with pytest.raises(evaluate.EvaluationInvalid, match="no active records"):
        prepare.check_sources(inspected, service.db)


def test_no_manifest_preserves_draft_mode(tmp_path, monkeypatch):
    _, _, cases, service = frozen_fixture(tmp_path, monkeypatch)
    result = evaluate.run_evaluation(cases, service)
    assert result["gold_status"] == "draft_not_frozen" and result["frozen_inputs"] is None


def test_frozen_bundle_is_portable_without_original_seen_paths(tmp_path, monkeypatch):
    output, _, cases, service = frozen_fixture(tmp_path, monkeypatch)
    (tmp_path / "seen.jsonl").unlink()
    assert run(output, cases, service)["gold_status"] == "frozen_inputs_verified"


@pytest.mark.parametrize("name", ["manifest.json", "review_plan.json", "reviewed_supports.json", "questions.jsonl",
                                 "previously_used_cases/0001.jsonl"])
def test_duplicate_keys_cannot_hide_contradictory_review_or_case_values(tmp_path, monkeypatch, name):
    output, _, cases, service = frozen_fixture(tmp_path, monkeypatch)
    path = output / name
    text = path.read_text(encoding="utf-8")
    if name == "manifest.json":
        text = text.replace('"overall_pass": null', '"overall_pass": true, "overall_pass": null')
    elif name == "review_plan.json":
        text = text.replace('"not_used_for_tuning": true', '"not_used_for_tuning": false, "not_used_for_tuning": true')
    elif name == "reviewed_supports.json":
        text = text.replace('"expected_count": null', '"expected_count": 999, "expected_count": null')
    else:
        text = text.replace('"question":', '"question": "Changed first duplicate value", "question":')
    path.write_text(text, encoding="utf-8")
    if name != "manifest.json":
        rewrite_manifest(output, lambda manifest: manifest["input_sha256"].update({name: evaluate.file_digest(path.read_bytes())}))
    with pytest.raises(evaluate.EvaluationInvalid, match="Duplicate JSON key"):
        run(output, cases, service, paid=True)
    assert sum(service.calls.values()) == 0


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_manifest_fields_cannot_be_ignored(tmp_path, monkeypatch, value):
    output, _, cases, service = frozen_fixture(tmp_path, monkeypatch)
    path = output / "manifest.json"
    text = path.read_text(encoding="utf-8").replace('"created_at":', f'"nonfinite_fixture": {value}, "created_at":')
    path.write_text(text, encoding="utf-8")
    with pytest.raises(evaluate.EvaluationInvalid, match="Nonfinite JSON value"):
        run(output, cases, service, paid=True)
    assert sum(service.calls.values()) == 0


@pytest.mark.parametrize("name", ["review_plan.json", "questions.jsonl"])
def test_preparation_rejects_duplicate_keys_without_source_checks(tmp_path, name):
    folder, seen = packet(tmp_path, [case()], "native")
    path = folder / name
    text = path.read_text(encoding="utf-8")
    if name == "review_plan.json":
        text = text.replace('"not_used_for_tuning": true', '"not_used_for_tuning": false, "not_used_for_tuning": true')
    else:
        text = text.replace('"question":', '"question": "Contradiction", "question":')
    path.write_text(text, encoding="utf-8")
    assert prepare.inspect_packet(folder, seen)["blockers"] == ["invalid_input:EvaluationInvalid"]


def test_preparation_rejects_nonfinite_json_numbers(tmp_path):
    folder, seen = packet(tmp_path, [case()], "native")
    path = folder / "review_plan.json"
    text = path.read_text(encoding="utf-8").replace('"scope":', '"nonfinite_fixture": 1e999, "scope":')
    path.write_text(text, encoding="utf-8")
    assert prepare.inspect_packet(folder, seen)["blockers"] == ["invalid_input:EvaluationInvalid"]


@pytest.mark.parametrize("content", [
    "record_id,record_id,article_group_id,split\nignored,old,old-group,development\n",
    "record_id,article_group_id\nold,old-group\n",
    "record_id,article_group_id,split,unrecognized\nold,old-group,development,ignored\n",
    "record_id,article_group_id,split\nold,old-group,development,extra\n",
    "record_id,article_group_id,split\nold,old-group\n",
    'record_id,article_group_id,split\nold,"unterminated,development\n',
])
def test_article_group_csv_rejects_ambiguous_headers_or_row_shapes(tmp_path, monkeypatch, content):
    output, _, cases, service = frozen_fixture(tmp_path, monkeypatch)
    path = output / "article_groups.csv"
    path.write_text(content, encoding="utf-8")
    rewrite_manifest(output, lambda manifest: manifest["input_sha256"].update({path.name: evaluate.file_digest(path.read_bytes())}))
    with pytest.raises(evaluate.EvaluationInvalid, match="article-group CSV"):
        run(output, cases, service, paid=True)
    assert sum(service.calls.values()) == 0
    folder = tmp_path / "packet"
    (folder / "article_groups.csv").write_text(content, encoding="utf-8")
    assert prepare.inspect_packet(folder, [tmp_path / "seen.jsonl"])["blockers"] == ["invalid_input:EvaluationInvalid"]


def test_documented_optional_review_note_is_retained_by_strict_csv_reader():
    payload = b"record_id,article_group_id,split,review_note\nr1,g1,holdout,Human review reference\n"
    assert evaluate.reviewed_group_rows(payload) == [{"record_id": "r1", "article_group_id": "g1",
                                                    "split": "holdout", "review_note": "Human review reference"}]
