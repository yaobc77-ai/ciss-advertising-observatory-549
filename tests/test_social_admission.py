"""Company-post decisions preserve duplicates without inventing paid ads."""

import hashlib
import json
from copy import deepcopy

import pytest

from observatory.import_records import load_records
from observatory.models import RecordInput
from observatory.quality import body_hash
from observatory.social_admission import (
    canonical_post_url,
    file_sha256,
    prepare_social_admission,
    public_social_admission,
)
from observatory.social_archive import SOCIAL_LABELS


def source_post(source_id="101", url="https://twitter.com/example/status/9001", body="A short supplied post?"):
    values = {key: key == "green_binary" for key in SOCIAL_LABELS}
    return RecordInput(
        record_id="junkipedia:" + source_id, dataset="social", platform="Twitter", url=url,
        account="Example account", sponsor="Example parent", title="Source title " + source_id,
        published_at="2023-05-02", body=body, countable=False, retrievable=False,
        raw={"body_sha256": body_hash(body), "source_row": {"id": source_id, "private_path": "C:/PRIVATE"},
             "sponsor_basis": "company_affiliation_not_verified_paid_sponsor"},
        annotations=[{"version": "claims-social-export-v1", "status": "historical_automatic_unverified",
                      "basis": "supplied_source_post_id_and_exact_body", "source_sha256": "b" * 64,
                      "source_row": int(source_id), "body_sha256": body_hash(body), "values": values,
                      "labels": [key for key in SOCIAL_LABELS if values[key]],
                      "taxonomy_mapping": "source_keys_preserved_no_native_label_mapping"}],
    )


def files(tmp_path, records):
    source = tmp_path / "source.jsonl"
    source.write_text("".join(record.model_dump_json() + "\n" for record in records), encoding="utf-8")
    data = tmp_path / "data.json"
    data.write_text(json.dumps({"decision_items": [
        {"id": "D02", "decision": {"options": [{"id": "collected_posts"}]}},
        {"id": "D03", "decision": {"options": [{"id": "unique_post"}]}},
    ]}), encoding="utf-8")
    reviews = tmp_path / "reviews.jsonl"
    reviews.write_text("".join(json.dumps({"id": key + "-event", "item_id": key, "status": "confirmed",
                                        "decision_choice": choice, "source_data_sha256": file_sha256(data)}) + "\n"
                               for key, choice in (("D02", "collected_posts"), ("D03", "unique_post"))), encoding="utf-8")
    return source, data, reviews


def prepare(tmp_path, records):
    source, data, reviews = files(tmp_path, records)
    receipt = prepare_social_admission(source, review_data=data, reviews=reviews, out=tmp_path / "new")
    projected = [json.loads(line) for line in (tmp_path / "new/records.jsonl").read_text(encoding="utf-8").splitlines()]
    return receipt, projected


def test_same_original_post_counts_once_and_preserves_every_source_variant(tmp_path):
    a, b = source_post(), source_post("102", "https://x.com/EXAMPLE/status/9001/")
    receipt, rows = prepare(tmp_path, [b, a])
    assert receipt["source_records"] == 2 and receipt["unique_posts"] == 1
    assert receipt["countable_records"] == receipt["retrievable_records"] == 1
    assert receipt["database_connections"] == receipt["model_calls"] == 0
    assert receipt["customer_acceptance"] is False
    assert rows[0]["record_id"] == "social-post:" + body_hash("twitter\nhttps://twitter.com/example/status/9001")
    assert rows[0]["raw"]["social_admission"]["selected_source_record_id"] == a.record_id
    assert {item["source_record_id"] for item in rows[0]["raw"]["source_variants"]} == {a.record_id, b.record_id}
    assert (tmp_path / "source.jsonl").read_bytes() == (tmp_path / "new/source_records.original.jsonl").read_bytes()
    assert len(load_records(tmp_path / "new/records.jsonl", dataset="social").records) == 1


def test_same_body_at_different_original_posts_remains_two_count_units(tmp_path):
    receipt, rows = prepare(tmp_path, [source_post(), source_post("102", "https://twitter.com/example/status/9002")])
    assert receipt["unique_posts"] == 2 and len(rows) == 2


def test_source_disagreements_make_identity_unknown_and_pause_conflicting_body(tmp_path):
    first, other = source_post(), source_post("102", body="A different source observation.")
    other.sponsor, other.account = "Other affiliation", "Other account"
    receipt, rows = prepare(tmp_path, [other, first])
    row = rows[0]
    assert row["sponsor"] == row["account"] == ""
    assert row["countable"] is True and row["retrievable"] is False
    assert row["body"] == first.body and row["annotations"] == []
    assert set(row["raw"]["social_admission"]["conflicting_fields"]) == {"body", "sponsor", "account"}
    assert receipt["conflict_counts"]["body"] == 1
    public = public_social_admission(row["raw"])
    assert {variant["body"] for variant in public["variants"]} == {first.body, other.body}
    assert "PRIVATE" not in json.dumps(public)


def test_historical_label_disagreement_does_not_become_or_merged_boolean(tmp_path):
    first, other = source_post(), source_post("102")
    other.annotations[0]["values"]["renewable_energy"] = True
    other.annotations[0]["labels"].append("renewable_energy")
    receipt, rows = prepare(tmp_path, [first, other])
    assert receipt["conflict_counts"]["historical_labels"] == 1
    assert rows[0]["annotations"] == []
    assert rows[0]["retrievable"] is True
    public = public_social_admission(rows[0]["raw"])
    states = [next(item["state"] for item in variant["historical_states"] if item["key"] == "renewable_energy")
              for variant in public["variants"]]
    assert states == ["source_false", "source_true"]


def test_missing_original_url_is_quarantined_without_guessing_identity(tmp_path):
    missing, valid = source_post("102", ""), source_post()
    receipt, rows = prepare(tmp_path, [missing, valid])
    assert receipt["quarantined_source_rows"] == 1 and receipt["unique_posts"] == 1
    quarantine = json.loads((tmp_path / "new/quarantined.jsonl").read_text(encoding="utf-8"))
    assert quarantine["record"]["url"] == "" and quarantine["record"]["record_id"] == missing.record_id
    assert len(rows) == 1 and receipt["all_source_members_preserved"] is True


@pytest.mark.parametrize("body", ["video", "", "privacy policy all rights reserved"])
def test_identity_count_remains_one_while_unusable_text_is_not_retrieved(tmp_path, body):
    receipt, rows = prepare(tmp_path, [source_post(body=body)])
    assert receipt["countable_records"] == 1 and receipt["retrievable_records"] == 0
    assert rows[0]["body"] == body


@pytest.mark.parametrize("change", ["review", "data", "choice", "unadmitted", "body_hash", "duplicate_id"])
def test_drift_or_ambiguous_source_stops_before_new_output(tmp_path, change):
    records = [source_post()]
    if change == "unadmitted":
        records[0].countable = True
    if change == "body_hash":
        records[0].raw["body_sha256"] = "a" * 64
    if change == "duplicate_id":
        records.append(deepcopy(records[0]))
    source, data, reviews = files(tmp_path, records)
    if change in {"review", "choice"}:
        events = [json.loads(line) for line in reviews.read_text().splitlines()]
        events[0]["status" if change == "review" else "decision_choice"] = "defer"
        reviews.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    elif change == "data":
        data.write_bytes(data.read_bytes() + b"\n")
    with pytest.raises(ValueError):
        prepare_social_admission(source, review_data=data, reviews=reviews, out=tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_existing_outputs_are_not_rewritten(tmp_path):
    source, data, reviews = files(tmp_path, [source_post()])
    output = tmp_path / "new"
    output.mkdir()
    sentinel = output / "existing.txt"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="new directory"):
        prepare_social_admission(source, review_data=data, reviews=reviews, out=output)
    assert sentinel.read_text() == "preserve"


@pytest.mark.parametrize("url", ["", "https://evil.test/example/status/9001", "https://twitter.com/example/status/9001?x=1",
                                "https://user:pass@twitter.com/example/status/9001", "https://twitter.com/example"])
def test_unsupported_urls_are_not_canonicalized_into_source_posts(url):
    assert canonical_post_url("Twitter", url) is None


def test_public_projection_rejects_variant_not_matching_the_canonical_source(tmp_path):
    _, rows = prepare(tmp_path, [source_post()])
    raw = rows[0]["raw"]
    raw["source_variants"][0]["source_record"]["url"] = "https://twitter.com/example/status/9002"
    assert public_social_admission(raw) is None


def test_source_and_review_snapshots_are_byte_exact(tmp_path):
    source, data, reviews = files(tmp_path, [source_post()])
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, data, reviews)}
    prepare_social_admission(source, review_data=data, reviews=reviews, out=tmp_path / "new")
    for path, name in ((source, "source_records.original.jsonl"), (data, "review_data.original.json"), (reviews, "reviews.original.jsonl")):
        assert file_sha256(path) == before[path.name] == file_sha256(tmp_path / "new" / name)
