"""Independent fictional review-store audit; never actual human reviews."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from observatory.semantic_review import (
    ReviewPacket,
    ReviewStore,
    ReviewSubmission,
    body_digest,
    digest,
)


@pytest.fixture
def store(tmp_path):
    codebook = json.loads((Path(__file__).resolve().parents[1] /
        "eval/semantic_review/semantic_codebook.v1.json").read_text(encoding="utf-8"))
    body = 'Moss Quay Guild says: "A pilot may save water." No measurements were supplied.'
    packet = {"case_id": "synthetic-moss-quay", "provenance": "synthetic_independent",
        "task": "Judge this fresh fictional source statement.", "sources": [{
            "source_id": "independent-source", "kind": "synthetic_independent", "record_id": "fictional:moss",
            "version_id": "fictional:moss-v1", "body": body, "body_hash": body_digest(body)}],
        "claims": [{"claim_id": "moss-claim", "text": "The guild says a pilot may save water.", "layer": "in_document"}],
        "candidate_answer": {"summary": "A tentative fictional statement.", "evidence": [], "limitations": []},
        "source_scope": "Fictional single source, offline test only.", "extraction_limitations": [],
        "historical_auxiliary": ["AI-AUXILIARY-MUST-NOT-APPEAR-IN-BLIND-VIEW"]}
    result = ReviewStore.initialize(tmp_path / "store", codebook, [packet])
    result.assign("fixture-only-a", "fixture-only-b", "fixture-only-c")
    return result


def access(store, role):
    return json.loads((store.root / "assignments.private.json").read_text(encoding="utf-8"))[role]


def review(store, role="A"):
    packet = store.packets["synthetic-moss-quay"]
    body = packet.sources[0].body
    return {"case_id": packet.case_id, "packet_sha256": store.manifest["packet_sha256"][packet.case_id],
        "codebook_sha256": store.manifest["codebook_sha256"], "reviewer_id": access(store, role)["reviewer_id"],
        "reviewer_kind": "human", "independent_human_attestation": True,
        "judgments": [{"claim_id": "moss-claim", "support": "supports", "mention": "mentioned",
            "stance": "uncertain", "external_truth": "not_checked", "summary": "Tentative statement only.",
            "reason": "PRIVATE-A-JUDGMENT-DO-NOT-LEAK", "evidence": [{"source_id": "independent-source",
                "start": 0, "end": len(body), "quote": body, "expression_subject": "Moss Quay Guild",
                "article_handling": "quotes_neutral"}], "limitations": []}]}


def save_a(store):
    return store.save("A", access(store, "A")["token"], review(store))


def test_blind_b_view_never_receives_a_or_historical_auxiliary(store):
    save_a(store)
    result = store.view("B", access(store, "B")["token"], "synthetic-moss-quay")
    serialized = json.dumps(result)
    assert "PRIVATE-A-JUDGMENT" not in serialized and "AI-AUXILIARY" not in serialized
    assert "fixture-only-a" not in serialized and "own_review" not in result


def test_saved_review_identity_cannot_cross_role_at_normal_save(store):
    with pytest.raises(ValueError, match="Reviewer"):
        store.save("B", access(store, "B")["token"], review(store, "A"))


def test_replayed_a_journal_cannot_be_counted_as_independent_b(store):
    save_a(store)
    entry = json.loads((store.root / "A/reviews.jsonl").read_text(encoding="utf-8"))
    entry["slot"] = "B"
    (store.root / "B").mkdir()
    (store.root / "B/reviews.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
    # The stored review remains authored by A, although its outer slot says B.
    with pytest.raises(ValueError):
        store.agreement()


def test_rehashed_journal_with_unknown_review_field_is_not_a_valid_review(store):
    save_a(store)
    path = store.root / "A/reviews.jsonl"
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["review"]["unexpected_field"] = "malformed journal"
    entry["review_sha256"] = digest(entry["review"])
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        store.view("A", access(store, "A")["token"], "synthetic-moss-quay")


@pytest.mark.parametrize("location", ["packet", "review", "judgment", "quote"])
def test_unknown_input_fields_are_rejected(store, location):
    value = review(store)
    if location == "packet":
        packet = deepcopy(store.packets["synthetic-moss-quay"].model_dump())
        packet["unexpected"] = "never source metadata"
        with pytest.raises(ValidationError):
            ReviewPacket.model_validate(packet)
    else:
        target = value if location == "review" else value["judgments"][0]
        if location == "quote":
            target = target["evidence"][0]
        target["unexpected"] = "never a valid schema field"
        with pytest.raises(ValidationError):
            ReviewSubmission.model_validate(value)


def test_demo_save_never_contributes_to_human_pair_counts(store):
    value = review(store)
    for key in ("reviewer_id", "reviewer_kind", "independent_human_attestation"):
        value.pop(key)
    value["actor_kind"] = "synthetic_demo"
    result = store.save_demo(value)
    assert result["human_review"] is False and result["gold"] is False
    agreement = store.agreement()
    assert agreement["paired_claims"] == agreement["paired_cases"] == 0
    assert agreement["human_accuracy"] is None
