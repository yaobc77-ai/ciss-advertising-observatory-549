"""Fresh fictional contract cases; fixture declarations are not real human reviews."""

import copy
import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest
from pydantic import ValidationError

from observatory.semantic_review import (
    ReviewPacket,
    ReviewStore,
    body_digest,
    demo_packet,
    digest,
    make_server,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    book = json.loads((ROOT / "eval/semantic_review/semantic_codebook.v1.json").read_text(encoding="utf-8"))
    packet = demo_packet()
    packet["historical_auxiliary"] = ["unreviewed fixture auxiliary; never a reference label"]
    result = ReviewStore.initialize(tmp_path / "review", book, [packet])
    result.assign("fixture-human-a", "fixture-human-b", "fixture-human-c")
    return result


def access(store, role):
    return json.loads((store.root / "assignments.private.json").read_text(encoding="utf-8"))[role]


def submission(store, role="A", support="supports"):
    packet = next(iter(store.packets.values()))
    source = packet.sources[0]
    quote = "Our pilot may reduce water use."
    start = source.body.index(quote)
    return {"case_id": packet.case_id, "packet_sha256": store.manifest["packet_sha256"][packet.case_id],
            "codebook_sha256": store.manifest["codebook_sha256"],
            "reviewer_id": access(store, role)["reviewer_id"], "reviewer_kind": "human",
            "independent_human_attestation": True,
            "judgments": [{"claim_id": "claim-1", "support": support,
                "mention": "mentioned", "stance": "favorable", "external_truth": "not_checked",
                "summary": "The fictional company presents a possible reduction.",
                "reason": "Actor and modality are retained.", "limitations": ["Only a fictional supplied paragraph."],
                "evidence": [{"source_id": source.source_id, "start": start, "end": start + len(quote),
                    "quote": quote, "expression_subject": "Oriole Grid", "article_handling": "quotes_distances"}]}]}


def save(store, role, value=None):
    return store.save(role, access(store, role)["token"], value or submission(store, role))


def test_frozen_codebook_has_independent_examples_and_three_schemas(store):
    assert len(store.codebook["rules"]) == 11
    assert all(len(v["examples"]) >= 2 for v in store.codebook["rules"])
    assert all(e["provenance"] == "fictional_independent" for r in store.codebook["rules"] for e in r["examples"])
    assert store.codebook["gold"] is False and store.codebook["customer_approved"] is False
    assert store.agreement()["paired_claims"] == 0
    assert store.agreement()["human_accuracy"] is None
    assert all(v["raw_agreement"] is None and v["cohen_kappa"] is None
               for v in store.agreement()["axes"].values())


@pytest.mark.parametrize("role,wrong_role", [("A", "B"), ("B", "A"), ("adjudicator", "A")])
def test_cross_slot_token_denied(store, role, wrong_role):
    with pytest.raises(PermissionError):
        store.view(role, access(store, wrong_role)["token"], "synthetic-oriole-001")


def test_a_b_blind_view_and_append_preservation(store):
    first = save(store, "B")
    a = store.view("A", access(store, "A")["token"], "synthetic-oriole-001")
    assert "own_review" not in a and "independent_reviews" not in a
    assert "historical_auxiliary" not in a["packet"]
    assert "fixture-human-b" not in json.dumps(a)
    second = save(store, "B", submission(store, "B", "partial"))
    assert first["review_sha256"] != second["review_sha256"]
    assert len(store._entries("B")) == 2
    assert store._entries("B")[0]["review"]["judgments"][0]["support"] == "supports"
    assert second["gold"] is False


@pytest.mark.parametrize("field", ["packet_sha256", "codebook_sha256", "reviewer_id"])
def test_binding_mismatch_rejected(store, field):
    value = submission(store)
    value[field] = "wrong-reviewer" if field == "reviewer_id" else "0" * 64
    with pytest.raises(ValueError):
        save(store, "A", value)
    assert not store._entries("A")


@pytest.mark.parametrize("change", ["start", "end", "quote", "source_id", "bool_offset"])
def test_invalid_locator_rejected(store, change):
    value = submission(store)
    quote = value["judgments"][0]["evidence"][0]
    if change == "start":
        quote["start"] += 1
    elif change == "end":
        quote["end"] = 99999
    elif change == "quote":
        quote["quote"] = "Our pilot guarantees reduced water use."
    elif change == "source_id":
        quote["source_id"] = "other-source"
    else:
        quote["start"] = True
    with pytest.raises(ValueError):
        save(store, "A", value)


@pytest.mark.parametrize("field,value", [("reviewer_kind", "ai"),
                                         ("independent_human_attestation", False)])
def test_ai_or_no_independent_human_declaration_rejected(store, field, value):
    data = submission(store)
    data[field] = value
    with pytest.raises(ValidationError):
        save(store, "A", data)


@pytest.mark.parametrize("support", ["supports", "partial", "contradicted"])
def test_positive_or_conflict_states_need_evidence(store, support):
    value = submission(store, support=support)
    value["judgments"][0]["evidence"] = []
    with pytest.raises(ValueError):
        save(store, "A", value)


@pytest.mark.parametrize("support", ["partial", "unsupported", "uncertain"])
def test_limited_states_need_limits(store, support):
    value = submission(store, support=support)
    value["judgments"][0]["limitations"] = []
    with pytest.raises(ValueError):
        save(store, "A", value)


@pytest.mark.parametrize("external", ["corroborated", "refuted"])
def test_source_assertion_is_not_external_truth(store, external):
    value = submission(store)
    value["judgments"][0]["external_truth"] = external
    with pytest.raises(ValueError):
        save(store, "A", value)


@pytest.mark.parametrize("change", ["duplicate", "missing", "unknown"])
def test_every_claim_exactly_once(store, change):
    value = submission(store)
    if change == "duplicate":
        value["judgments"] *= 2
    elif change == "missing":
        value["judgments"] = []
    else:
        value["judgments"][0]["claim_id"] = "unknown-claim"
    with pytest.raises(ValueError):
        save(store, "A", value)


def test_third_party_requires_both_and_exact_latest_pair(store):
    with pytest.raises(ValueError, match="Both independent"):
        store.view("adjudicator", access(store, "adjudicator")["token"], "synthetic-oriole-001")
    a, b = save(store, "A"), save(store, "B")
    value = submission(store, "adjudicator")
    value.update(review_a_sha256=a["review_sha256"], review_b_sha256=b["review_sha256"],
                 adjudication_reason="Fixture third-person explanation.")
    saved = save(store, "adjudicator", value)
    assert saved["status"] == "adjudication_saved_not_gold"
    save(store, "A", submission(store, "A", "partial"))
    with pytest.raises(ValueError, match="exact latest"):
        save(store, "adjudicator", value)
    view = store.view("adjudicator", access(store, "adjudicator")["token"], "synthetic-oriole-001")
    assert view["status"] == "adjudication_stale_after_review_revision"
    assert "reviewer_id" not in json.dumps(view["independent_reviews"])


def test_agreement_keeps_uncertain_and_unpaired_denominator(store, tmp_path):
    save(store, "A", submission(store, "A", "uncertain"))
    save(store, "B", submission(store, "B", "supports"))
    metric = store.agreement()
    assert metric["total_claims"] == metric["paired_claims"] == 1
    assert metric["axes"]["support"]["raw_agreement"] == 0
    assert metric["axes"]["support"]["cohen_kappa"] == 0
    assert metric["axes"]["support"]["counts_a"] == {"uncertain": 1}
    assert metric["axes"]["stance"]["cohen_kappa"] is None  # p_e = 1
    store.export(tmp_path / "export")
    exported = json.loads((tmp_path / "export/manifest.json").read_text(encoding="utf-8"))
    assert exported["gold"] is False and exported["export_status"] == "review_material_not_gold"
    with pytest.raises(FileExistsError):
        store.export(tmp_path / "export")


@pytest.mark.parametrize("file", ["codebook.frozen.json", "packets.jsonl", "manifest.json"])
def test_live_frozen_drift_blocks_saving(store, file):
    path = store.root / file
    if file == "packets.jsonl":
        value = json.loads(path.read_text(encoding="utf-8"))
        value["task"] = "Changed task"
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
        value["extra_drift"] = True
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        save(store, "A")


def test_original_unicode_body_and_observation_binding():
    value = demo_packet()
    value["sources"][0].update(body="苔鹿说：可能改善🌿。", body_hash=body_digest("苔鹿说：可能改善🌿。"),
        source_observation_id="fictional:observation", source_version_id="1" * 64,
        source_body_hash=body_digest("苔鹿说：可能改善🌿。"))
    packet = ReviewPacket.model_validate(value)
    assert packet.sources[0].body[8:9] == "🌿"
    value["sources"][0]["source_body_hash"] = "0" * 64
    with pytest.raises(ValueError):
        ReviewPacket.model_validate(value)


def test_three_distinct_reviewers_and_new_store_required(store, tmp_path):
    with pytest.raises(ValueError):
        store.assign("one-person", "one-person", "third-person")
    with pytest.raises(ValueError):
        store.assign("TBD", "person-b", "person-c")
    with pytest.raises(FileExistsError):
        store.assign("new-a", "new-b", "new-c")
    with pytest.raises(FileExistsError):
        ReviewStore.initialize(store.root, store.codebook, [demo_packet()])
    assert not (tmp_path / "business.db").exists()


@pytest.mark.integration
def test_actual_http_page_save_and_blind_access(store):
    server = make_server(store, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    params = urlencode({"role": "A", "token": access(store, "A")["token"], "case": "synthetic-oriole-001"})
    try:
        with urlopen(base + "/review?" + params) as response:
            page = response.read().decode("utf-8")
        assert "保存本次独立审核" in page and "fixture-human-b" not in page
        assert '<select class="support">' in page and 'id="review"' not in page
        req = Request(base + "/save?" + params,
                      data=json.dumps(submission(store)).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req) as response:
            result = json.loads(response.read())
        assert result["status"] == "independent_review_saved"
        assert len(store._entries("A")) == 1  # actual durable HTTP write
        bad = urlencode({"role": "B", "token": access(store, "A")["token"], "case": "synthetic-oriole-001"})
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/packet?" + bad)
        assert error.value.code == 403
        evil = Request(base + "/save?" + params, data=b"{}", headers={
            "Content-Type": "application/json", "Origin": "https://elsewhere.invalid"}, method="POST")
        with pytest.raises(HTTPError) as error:
            urlopen(evil)
        assert error.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_server_cannot_bind_public_interface(store):
    with pytest.raises(ValueError):
        make_server(store, host="0.0.0.0", port=0)


def test_bad_frozen_schema_rejected_before_store_creation(store, tmp_path):
    book = copy.deepcopy(store.codebook)
    book["review_schema"]["title"] = "ChangedSchema"
    with pytest.raises(ValueError):
        ReviewStore.initialize(tmp_path / "bad", book, [demo_packet()])
    assert not (tmp_path / "bad").exists()


def test_multiple_pair_kappa_and_unpaired_coverage(store, tmp_path):
    packets = []
    for index in range(3):
        packet = demo_packet()
        packet["case_id"] = f"fictional-case-{index}"
        packets.append(packet)
    result = ReviewStore.initialize(tmp_path / "multiple", store.codebook, packets)
    result.assign("fixture-human-a", "fixture-human-b", "fixture-human-c")
    for index, labels in enumerate([("supports", "supports"), ("partial", "contradicted")]):
        for role, label in zip(("A", "B"), labels):
            value = submission(result, role, label)
            value["case_id"] = f"fictional-case-{index}"
            value["packet_sha256"] = result.manifest["packet_sha256"][value["case_id"]]
            save(result, role, value)
    metric = result.agreement()
    assert (metric["total_cases"], metric["paired_cases"], metric["total_claims"], metric["paired_claims"]) == (3, 2, 3, 2)
    assert metric["axes"]["support"]["raw_agreement"] == 0.5
    assert metric["axes"]["support"]["chance_agreement"] == 0.25
    assert metric["axes"]["support"]["cohen_kappa"] == pytest.approx(1 / 3)
    assert sum(v["count"] for v in metric["axes"]["support"]["confusion"]) == 2


def test_external_truth_requires_separate_bound_source_positive(store, tmp_path):
    packet = demo_packet()
    packet["provenance"] = "independent_source_packet"  # exercise route; all material still fictional fixtures
    packet["sources"][0]["kind"] = "original_text"
    text = "Fictional independent report: the tested pilot reduced water use under condition R."
    packet["sources"].append({"source_id": "external-fixture", "kind": "external_evidence",
        "record_id": "fictional:independent-report", "version_id": "fictional-v1",
        "body_hash": body_digest(text), "body": text})
    packet["claims"][0].update(text="The fictional tested pilot reduced water use under condition R.", layer="external_truth")
    result = ReviewStore.initialize(tmp_path / "external", store.codebook, [packet])
    result.assign("fixture-human-a", "fixture-human-b", "fixture-human-c")
    value = submission(result)
    value["judgments"][0]["external_truth"] = "corroborated"
    value["judgments"][0]["evidence"] = [{"source_id": "external-fixture", "start": 0, "end": len(text),
        "quote": text, "expression_subject": "fictional independent tester", "article_handling": "asserts"}]
    assert save(result, "A", value)["status"] == "independent_review_saved"


def test_changed_journal_rejected_and_export_keeps_revisions(store, tmp_path):
    save(store, "A")
    save(store, "A", submission(store, "A", "partial"))
    store.export(tmp_path / "export-revisions")
    assert len((tmp_path / "export-revisions/A.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    path = store.root / "A/reviews.jsonl"
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    entries[0]["review"]["judgments"][0]["support"] = "unsupported"
    path.write_text("\n".join(json.dumps(v) for v in entries) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="journal drift"):
        store.agreement()


def test_synthetic_demo_saved_separately_and_never_counts_human(store):
    value = submission(store)
    for key in ("reviewer_id", "reviewer_kind", "independent_human_attestation"):
        value.pop(key)
    value["actor_kind"] = "synthetic_demo"
    saved = store.save_demo(value)
    assert saved["human_review"] is False and saved["gold"] is False
    assert saved["status"] == "synthetic_demo_saved_not_human_review"
    assert not store._entries("A") and not store._entries("B")
    assert store.agreement()["paired_claims"] == 0
    assert store.agreement()["human_accuracy"] is None
    assert (store.root / "synthetic_demo/demonstrations.jsonl").exists()


def test_python_exact_quote_locator_including_ambiguous_unicode(store, tmp_path):
    packet = demo_packet()
    body = "🌿苔鹿说可能。后段再次说可能。"
    packet["sources"][0].update(body=body, body_hash=body_digest(body))
    result = ReviewStore.initialize(tmp_path / "locator", store.codebook, [packet])
    matches = result.locate_quote("synthetic_demo", "", packet["case_id"], "source-1", "可能")
    assert [v["start"] for v in matches["matches"]] == [4, 12]
    assert all(body[v["start"]:v["end"]] == "可能" and "🌿" in v["context"] for v in matches["matches"])
    with pytest.raises(ValueError, match="逐字匹配"):
        result.locate_quote("synthetic_demo", "", packet["case_id"], "source-1", "已保证")


def test_original_source_cannot_be_public_demo(store, tmp_path):
    packet = demo_packet()
    packet["provenance"] = "independent_source_packet"
    packet["sources"][0]["kind"] = "original_text"
    result = ReviewStore.initialize(tmp_path / "private-original", store.codebook, [packet])
    with pytest.raises(ValueError, match="fictional"):
        result.demo_view()


@pytest.mark.parametrize("tamper", ["copy_reviewer", "unknown_review_field"])
def test_journal_reparse_rejects_identity_copy_and_rehashed_unknown_field(store, tamper):
    save(store, "A")
    value = json.loads((store.root / "A/reviews.jsonl").read_text(encoding="utf-8"))
    if tamper == "copy_reviewer":
        value["slot"] = "B"
        target = store.root / "B/reviews.jsonl"
    else:
        value["review"]["unexpected_field"] = "injected"
        value["review_sha256"] = digest(value["review"])
        target = store.root / "A/reviews.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        store.agreement()


def test_same_version_rule_edit_cannot_be_a_new_v1_store(store, tmp_path):
    modified = copy.deepcopy(store.codebook)
    modified["rules"][0]["operation"] = "Changed without a new version"
    with pytest.raises(ValueError, match="Frozen v1"):
        ReviewStore.initialize(tmp_path / "changed-rules", modified, [demo_packet()])
    assert not (tmp_path / "changed-rules").exists()
