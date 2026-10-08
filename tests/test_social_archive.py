"""Offline social export staging never asserts ad status or silently maps labels."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from zipfile import ZipFile

import pytest

from observatory.import_records import load_records
from observatory.social_archive import SOCIAL_LABELS, prepare_social_archive


def post(source_id="101", platform_id="9001"):
    return {
        "id": source_id, "channel_id": "42", "platform": "Twitter",
        "post_text": "Original post — unchanged.\nA second line?",
        "channel": {"name": "Example Company", "username": "example"},
        "parent_entity": "Example Parent",
        "date_time": {
            "created_at": "2025-01-01T00:00:00Z",
            "published_at": "2023-05-02T12:34:56Z",
        },
        "urls": {
            "post_url": [
                "https://t.co/example", f"https://twitter.com/example/status/{platform_id}",
                "https://pbs.twimg.com/media/example.jpg",
            ],
        },
        "post": {"junkipedia_uid": platform_id, "image_text": "Text only in the image"},
        "junkipedia_link": f"https://www.junkipedia.org/posts/{source_id}",
        "ads_data": None, "year": 2023,
        **{label: label in {"green_binary", "renewable_energy"} for label in SOCIAL_LABELS},
        "green_explanation": "Historical model explanation", "fossil_fuel_explanation": "",
    }


def prepare(tmp_path: Path, rows, *, zipped=False):
    data = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    source = tmp_path / ("supplied.zip" if zipped else "supplied.json")
    if zipped:
        with ZipFile(source, "w") as archive:
            archive.writestr("Social Media Data/claims_twitter_sample.json", data)
            archive.writestr("Social Media Data/Data documentation.docx", b"documentation")
            archive.writestr("Social Media Data/Manual dataset Native Ads.zip", b"not consumed")
    else:
        source.write_bytes(data)
    out, report = tmp_path / "private" / "records.jsonl", tmp_path / "private" / "audit.json"
    result = prepare_social_archive(source, out=out, report=report)
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    return records, result, source, out, report


def test_legacy_nul_explanations_are_reversible_without_changing_body_or_labels(tmp_path):
    original = post()
    original["green_explanation"] = "Private\u0000original; existing literal \\u0000; quote \" and \\ slash"
    original["explanation"] = "Another\u0000old result"
    records, receipt, source, out, _ = prepare(tmp_path, [original], zipped=True)
    item = records[0]
    stored = item["raw"]["source_row"]
    encoding = item["raw"]["source_string_storage"]
    assert encoding["scheme"] == "json-string-nul-v1"
    restored = dict(stored)
    for key, value in encoding["original_json_strings"].items():
        assert "\u0000" not in value
        restored[key] = json.loads(value)
        assert item["annotations"][0]["explanations"][key] == stored[key]
    assert restored == original
    assert stored["green_explanation"] == original["green_explanation"].replace("\u0000", r"\u0000")
    assert item["body"] == original["post_text"]
    assert item["annotations"][0]["values"] == {key: original[key] for key in SOCIAL_LABELS}
    assert item["countable"] is False and item["retrievable"] is False
    assert receipt["historical_explanation_storage"]["escaped_record_count"] == 1
    assert receipt["rejected_row_count"] == 0
    assert load_records(out, dataset="social").records[0].body == original["post_text"]
    with ZipFile(source) as archive:
        assert json.loads(archive.read("Social Media Data/claims_twitter_sample.json")) == [original]


def test_literal_nul_escape_is_unchanged_and_not_marked_as_processed(tmp_path):
    original = post()
    original["green_explanation"] = r"A literal \u0000 is ordinary source text."
    records, receipt, _, _, _ = prepare(tmp_path, [original])
    assert records[0]["raw"]["source_row"] == original
    assert "source_string_storage" not in records[0]["raw"]
    assert "explanation_storage" not in records[0]["annotations"][0]
    assert receipt["historical_explanation_storage"]["escaped_record_count"] == 0


@pytest.mark.parametrize("field", ["post_text", "title", "nested", "key", "structured_explanation"])
def test_nul_outside_legacy_explanation_strings_is_not_cleaned(tmp_path, field):
    original = post()
    if field == "nested":
        original["extra"] = {"nested": ["PRIVATE-SENTINEL\u0000"]}
    elif field == "key":
        original["PRIVATE-SENTINEL\u0000"] = "value"
    elif field == "structured_explanation":
        original["explanation"] = {"text": "PRIVATE-SENTINEL\u0000"}
    else:
        original[field] = "PRIVATE-SENTINEL\u0000"
    records, receipt, _, _, _ = prepare(tmp_path, [original])
    assert records == [] and receipt["rejected_row_count"] == 1
    assert "PRIVATE-SENTINEL" not in json.dumps(receipt)


@pytest.mark.parametrize("zipped", [False, True])
def test_nested_export_maps_exact_source_fields_and_validates_canonical_contract(tmp_path, zipped):
    original = post()
    records, receipt, source, out, report = prepare(tmp_path, [original], zipped=zipped)
    record = records[0]
    assert record["record_id"] == "junkipedia:101"
    assert record["url"] == "https://twitter.com/example/status/9001"
    assert record["body"] == original["post_text"]
    assert record["raw"]["source_row"] == original
    assert record["published_at"] == "2023-05-02"
    assert record["publisher"] == ""
    assert record["account"] == "Example Company"
    assert record["sponsor"] == "Example Parent"
    assert record["countable"] is False and record["retrievable"] is False
    assert record["raw"]["ad_status"] == "unknown"
    assert record["raw"]["sponsor_basis"] == "company_affiliation_not_verified_paid_sponsor"
    assert receipt["input_rows"] == receipt["prepared_records"] == 1
    assert receipt["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert receipt["output_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert receipt == json.loads(report.read_text(encoding="utf-8"))
    assert len(load_records(out, dataset="social").records) == 1
    if zipped:
        assert receipt["documentation_sha256"] == hashlib.sha256(b"documentation").hexdigest()


def test_source_social_labels_are_separate_and_not_converted_to_native_taxonomy(tmp_path):
    records, receipt, *_ = prepare(tmp_path, [post()])
    annotation = records[0]["annotations"][0]
    assert annotation["version"] == "claims-social-export-v1"
    assert annotation["status"] == "historical_automatic_unverified"
    assert set(annotation["values"]) == set(SOCIAL_LABELS)
    assert annotation["values"]["renewable_energy"] is True
    assert annotation["explanations"]["green_explanation"] == "Historical model explanation"
    assert "green_labels.viable_solutions" not in annotation["values"]
    assert receipt["historical_annotations"] == 1


@pytest.mark.parametrize("value", ["true", 1, None])
def test_historical_boolean_labels_are_not_coerced_or_partially_attached(tmp_path, value):
    original = post()
    original["green_binary"] = value
    records, receipt, *_ = prepare(tmp_path, [original])
    assert records[0]["annotations"] == []
    assert records[0]["raw"]["source_row"]["green_binary"] == value
    assert receipt["issue_counts"]["social_historical_labels_invalid"] == 1


def test_duplicate_urls_and_bodies_remain_separate_and_flagged(tmp_path):
    original, another = post(), post("102")
    records, receipt, *_ = prepare(tmp_path, [original, another])
    assert len(records) == 2
    assert records[0]["record_id"] != records[1]["record_id"]
    assert receipt["unique_original_post_urls"] == 1
    assert receipt["duplicate_original_url_groups"] == 1
    assert receipt["duplicate_original_url_rows"] == 2
    assert receipt["duplicate_body_rows"] == 2
    assert records[0]["raw"]["duplicate_original_post_url_source_rows"] == [1, 2]
    assert records[0]["countable"] is False


def test_repeated_source_ids_exclude_all_ambiguous_rows_without_replacement_ids(tmp_path):
    first, second = post(), post(platform_id="9002")
    records, receipt, *_ = prepare(tmp_path, [first, second])
    assert records == []
    assert receipt["rejected_row_count"] == 2
    assert [row["row"] for row in receipt["rejected_rows"]] == [1, 2]


@pytest.mark.parametrize("urls", [
    ["https://twitter.com/example/status/9999"],
    ["https://t.co/example"],
    ["https://twitter.com/example/status/9001", "https://x.com/example/status/9001"],
    "https://twitter.com/example/status/9001?credential=hidden",
])
def test_unmatched_or_ambiguous_post_urls_are_rejected_and_not_guessed(tmp_path, urls):
    original = post()
    original["urls"]["post_url"] = urls
    records, receipt, *_ = prepare(tmp_path, [original])
    assert records == []
    assert receipt["rejected_row_count"] == 1
    assert "credential=hidden" not in json.dumps(receipt)


def test_string_post_url_is_supported(tmp_path):
    original = post()
    original["urls"]["post_url"] = "https://twitter.com/example/status/9001"
    records, receipt, *_ = prepare(tmp_path, [original])
    assert len(records) == 1
    assert receipt["rejected_row_count"] == 0


@pytest.mark.parametrize("published", [None, "invalid", "2023-05-02"])
def test_missing_publication_date_never_uses_collection_date_or_year(tmp_path, published):
    original = post()
    original["date_time"]["published_at"] = published
    records, receipt, *_ = prepare(tmp_path, [original])
    assert records[0]["published_at"] is None
    assert records[0]["raw"]["date_precision"] == "unknown"
    assert receipt["publication_dates_available"] == 0


def test_timestamp_normalizes_to_utc_publication_day(tmp_path):
    original = post()
    original["date_time"]["published_at"] = "2023-05-02T00:30:00+02:00"
    records, *_ = prepare(tmp_path, [original])
    assert records[0]["published_at"] == "2023-05-01"


def test_media_and_reference_text_are_preserved_but_never_added_to_citable_body(tmp_path):
    original = post()
    original["post"].update(audio_text="Audio transcript", referenced_tweet_text="Someone else's post")
    records, *_ = prepare(tmp_path, [original])
    assert records[0]["body"] == original["post_text"]
    assert records[0]["raw"]["source_row"]["post"] == original["post"]
    assert "Audio transcript" not in records[0]["body"]


def test_missing_archive_url_is_explicit_and_does_not_replace_original(tmp_path):
    original = post()
    original["junkipedia_link"] = None
    records, receipt, *_ = prepare(tmp_path, [original])
    assert records[0]["archive_url"] == ""
    assert records[0]["url"].startswith("https://twitter.com/")
    assert receipt["issue_counts"]["social_archive_url_missing_or_invalid"] == 1


def test_supplied_ad_metadata_does_not_automatically_admit_the_row(tmp_path):
    original = post()
    original["ads_data"] = {"claimed_ad": True}
    records, receipt, *_ = prepare(tmp_path, [original])
    assert receipt["ads_data_nonnull_rows"] == 1
    assert records[0]["raw"]["ad_status"] == "unknown"
    assert records[0]["countable"] is False


@pytest.mark.parametrize("unsafe", ["../claims_twitter_sample.json", "C:/claims_twitter_sample.json", "..\\claims_twitter_sample.json"])
def test_archive_path_traversal_is_rejected_without_extraction(tmp_path, unsafe):
    source = tmp_path / "unsafe.zip"
    with ZipFile(source, "w") as archive:
        archive.writestr(unsafe, json.dumps([post()]))
    with pytest.raises(ValueError, match="unsafe member path"):
        prepare_social_archive(source, out=tmp_path / "records.jsonl", report=tmp_path / "audit.json")
    assert not (tmp_path / "records.jsonl").exists()


def test_multiple_matching_json_members_are_not_selected_by_order(tmp_path):
    source = tmp_path / "ambiguous.zip"
    with ZipFile(source, "w") as archive:
        for prefix in ("first", "second"):
            archive.writestr(f"{prefix}/claims_twitter_sample.json", json.dumps([post()]))
    with pytest.raises(ValueError, match="exactly one"):
        prepare_social_archive(source, out=tmp_path / "records.jsonl", report=tmp_path / "audit.json")


def test_outputs_cannot_overwrite_source_or_existing_files(tmp_path):
    original = post()
    _, _, source, out, report = prepare(tmp_path, [original])
    before = source.read_bytes(), out.read_bytes(), report.read_bytes()
    with pytest.raises(ValueError, match="distinct"):
        prepare_social_archive(source, out=source, report=tmp_path / "new.json")
    with pytest.raises(ValueError, match="already exist"):
        prepare_social_archive(source, out=out, report=report)
    assert before == (source.read_bytes(), out.read_bytes(), report.read_bytes())


@pytest.mark.parametrize("bad", ["[]", "{}", '[{"id":"101","id":"102"}]', '[{"id":NaN}]'])
def test_malformed_or_ambiguous_json_has_no_outputs(tmp_path, bad):
    source = tmp_path / "bad.json"
    source.write_text(bad, encoding="utf-8")
    with pytest.raises(ValueError):
        prepare_social_archive(source, out=tmp_path / "records.jsonl", report=tmp_path / "audit.json")
    assert not (tmp_path / "records.jsonl").exists()


def test_report_has_no_post_text(tmp_path):
    original = post()
    _, _, _, _, report = prepare(tmp_path, [original])
    text = report.read_text(encoding="utf-8")
    assert original["post_text"] not in text
    assert "Text only in the image" not in text


def test_id_is_stable_across_archive_and_json_input_location(tmp_path):
    first, second = tmp_path / "json", tmp_path / "zip"
    first.mkdir()
    second.mkdir()
    json_records, *_ = prepare(first, [deepcopy(post())])
    zip_records, *_ = prepare(second, [deepcopy(post())], zipped=True)
    assert json_records[0]["record_id"] == zip_records[0]["record_id"]
