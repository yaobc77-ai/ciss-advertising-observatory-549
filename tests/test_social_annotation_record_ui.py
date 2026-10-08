"""Social details show historical source states without native claim semantics."""

import hashlib
from copy import deepcopy
from types import SimpleNamespace

import pytest
from flask import Flask

from observatory import record_view
from observatory.social_annotations import social_annotation_details
from observatory.social_archive import SOCIAL_LABELS

BODY = "Stored source post."
SOURCE_HASH = "a" * 64
PRIVATE = "never-public-source-path"
# Captured from the old record_view template before adding its social branch.
NATIVE_PAGE_HASH = "2f5f6c69e12a8939618ea1ceefc22950e34b2e590c8d5d3bc2630a047def6c4f"


def source(**updates):
    return {
        "record_id": "post-1", "version_id": "version-1", "dataset": "social",
        "title": "Source post", "publisher": "Native publisher trap",
        "sponsor": "Exact Parent Entity", "account": "Exact channel.name",
        "platform": "Twitter", "date": "2024-02-01", "keyword": "Native collection trap",
        "body": BODY, "body_hash": hashlib.sha256(BODY.encode()).hexdigest(),
        "url": "https://example.org/source", "archive_url": "",
        "archive_status": "No verified archived copy linked", "archive_note": "Original archive note.",
        "attachments": [], "body_label": "Stored source text", "body_note": "Original body note.",
        "quality_notes": [], "body_characters": len(BODY), "retrievable": True,
        **updates,
    }


def annotation(row, *, positives=(), explanations=None):
    values = {key: key in positives for key in SOCIAL_LABELS}
    return {
        "version": "claims-social-export-v1", "status": "historical_automatic_unverified",
        "labels": [key for key in SOCIAL_LABELS if values[key]], "values": values,
        "body_sha256": row["body_hash"], "source_sha256": SOURCE_HASH, "source_row": 42,
        "basis": "supplied_source_post_id_and_exact_body",
        "taxonomy_mapping": "source_keys_preserved_no_native_label_mapping",
        "explanations": explanations or {}, "source_path": PRIVATE,
    }


def project(row, entries):
    row["social_historical_annotation"] = social_annotation_details({**row, "annotations": entries})
    return row


class NoSocialClaims:
    def __init__(self):
        self.calls = []

    def claims_matches(self, filters, **kwargs):
        self.calls.append((filters, kwargs))
        raise AssertionError("A social detail must never request CLAIMS2 assignments")


def page(row, service=None):
    app = Flask(__name__)
    details = SimpleNamespace(get=lambda identifier: row if row and identifier == row["record_id"] else None)
    register = record_view.register_record_page
    register(app, details, service=service)
    return app.test_client()


def test_social_page_retains_source_metadata_and_all_macro_and_subcode_states(monkeypatch):
    row = source()
    project(row, [annotation(row, positives=("green_binary", "renewable_energy", "fossil_fuel_binary"))])
    before = deepcopy(row)
    service = NoSocialClaims()

    def already_projected(_row):
        raise AssertionError("Do not revalidate the trusted RecordDetails projection")

    monkeypatch.setattr(record_view, "social_annotation_details", already_projected)
    response = page(row, service).get("/records/post-1")
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    for expected in ("Social post", "<dt>Account</dt>", "Exact channel.name", "<dt>Platform</dt>",
                     "Twitter", "<dt>Company affiliation</dt>", "Exact Parent Entity", "Publication date",
                     "does not establish verified paid sponsorship", "Supplied social historical labels",
                     "historical_automatic_unverified", "Binding bound", SOURCE_HASH, "source_row", "42"):
        assert expected in text
    for key in SOCIAL_LABELS:
        assert text.count(f"<code>{key}</code>") == 1
    assert text.count("(Macro code)") == 2 and text.count("(Subcode)") == 11
    assert text.count("Source export: True") == 3
    assert text.count("Source export: False") == 10
    assert "not verified themes, greenwashing findings or factual judgments" in text
    assert "According to the supplied field documentation" in text
    for hidden in ("Article record", "<dt>Publisher</dt>", "Sponsor / organization", "Collection search term:",
                   "Native publisher trap", "Native collection trap", "CLAIMS2 evidence", "record-claims"):
        assert hidden not in text
    assert service.calls == [] and row == before


def test_all_false_is_complete_source_output_and_not_missing_annotation():
    row = source()
    project(row, [annotation(row)])
    text = page(row).get("/records/post-1").get_data(as_text=True)
    assert "Binding bound" in text
    assert text.count("Source export: False") == 13
    assert "Source export: True" not in text and "Unknown — no usable source value" not in text
    assert "True and False describe what that export recorded" in text
    assert "not reviewed greenwashing findings or fact checks" in text


@pytest.mark.parametrize("binding", ["missing", "invalid", "ambiguous", "body_mismatch"])
def test_unusable_annotations_show_thirteen_unknowns_without_false_negatives(binding):
    row = source()
    good = annotation(row, positives=("green_binary",))
    entries = [good]
    if binding == "missing":
        entries = []
    elif binding == "invalid":
        good["values"]["renewable_energy"] = "false"
    elif binding == "ambiguous":
        entries.append(deepcopy(good))
    else:
        row["body_hash"] = "b" * 64
    project(row, entries)
    assert row["social_historical_annotation"]["validation_state"] == binding
    text = page(row).get("/records/post-1").get_data(as_text=True)
    assert f"Binding {binding}" in text
    assert text.count("Unknown — no usable source value") == 13
    assert "Source export: False" not in text and "Source export: True" not in text
    assert "Historical generated explanations" not in text


@pytest.mark.parametrize("bad_hash,binding", [(False, "missing"), (True, "body_mismatch")])
def test_missing_public_projection_uses_unknown_fallback_without_raw_annotations(monkeypatch, bad_hash, binding):
    row = source()
    row["annotations"] = [annotation(row, positives=SOCIAL_LABELS)]
    row["raw"] = {"private": PRIVATE}
    if bad_hash:
        row["body_hash"] = "bad hash"
    seen = []

    def public_only(candidate):
        seen.append(candidate)
        assert set(candidate) == {"dataset", "record_id", "version_id", "body", "body_hash", "annotations"}
        assert candidate["annotations"] == []
        return social_annotation_details(candidate)

    monkeypatch.setattr(record_view, "social_annotation_details", public_only)
    text = page(row).get("/records/post-1").get_data(as_text=True)
    assert len(seen) == 1 and f"Binding {binding}" in text
    assert text.count("Unknown — no usable source value") == 13
    assert "Source export: True" not in text and "Source export: False" not in text
    assert PRIVATE not in text


@pytest.mark.parametrize("missing", [None, "", "(Unknown)"])
def test_missing_social_metadata_uses_specific_unknown_names(missing):
    row = source(account=missing, platform=missing, sponsor=missing, publisher="")
    text = page(row).get("/records/post-1").get_data(as_text=True)
    for label in ("Unknown account", "Unknown platform", "Unknown company affiliation"):
        assert label in text
    assert "Unknown outlet" not in text and "Unknown sponsor" not in text


def test_generated_explanations_are_collapsed_plain_text_and_html_is_escaped():
    unsafe = '<script>alert("source")</script>'
    row = source(title=unsafe, account=unsafe, sponsor=unsafe, body=unsafe,
                 body_hash=hashlib.sha256(unsafe.encode()).hexdigest(), body_characters=len(unsafe))
    long_text = unsafe + "x" * 2100
    project(row, [annotation(row, explanations={"green_explanation": long_text,
                                               "fossil_fuel_explanation": "Earlier classifier explanation.",
                                               "private_explanation": PRIVATE})])
    text = page(row).get("/records/post-1").get_data(as_text=True)
    assert "<script>alert" not in text and "&lt;script&gt;" in text
    assert '<details><summary>Historical generated explanations</summary>' in text
    assert "not original post quotations or independent verification" in text
    assert "Explanation shortened for display" in text
    assert "x" * 2100 not in text
    assert "<blockquote>" not in text and PRIVATE not in text
    assert "Earlier classifier explanation." in text


def test_escaped_explanation_displays_its_storage_note_without_original_raw_value():
    row = source()
    entry = annotation(row, explanations={"green_explanation": r"Old \u0000 result"})
    entry["explanation_storage"] = {"scheme": "json-string-nul-v1", "escaped_fields": ["green_explanation"]}
    row["raw"] = {"source_string_storage": {"original_json_strings": {"green_explanation": PRIVATE}}}
    project(row, [entry])
    text = page(row).get("/records/post-1").get_data(as_text=True)
    assert r"Old \u0000 result" in text
    assert "Original U+0000 characters are displayed" in text
    assert "Exact original strings are preserved" in text
    assert PRIVATE not in text and "\u0000" not in text


def test_native_render_matches_original_bytes_and_keeps_native_metadata_and_claims():
    body = "Original native body."
    row = source(record_id="native-1", dataset="native", title="Native title", publisher="Native publisher",
                 sponsor="exxonmobil", keyword="Native collection term", body=body,
                 body_hash=hashlib.sha256(body.encode()).hexdigest(), body_characters=len(body))
    response = page(row).get("/records/native-1")
    assert response.status_code == 200
    assert hashlib.sha256(response.data).hexdigest() == NATIVE_PAGE_HASH
    text = response.get_data(as_text=True)
    assert "Article record" in text and "<dt>Publisher</dt>" in text and "ExxonMobil" in text
    assert "Collection search term:" in text and "CLAIMS2 evidence" in text
    assert "Supplied social historical labels" not in text and "<dt>Account</dt>" not in text


def test_native_page_still_calls_claims_with_current_record_scope():
    row = source(dataset="native")
    service = NoSocialClaims()
    response = page(row, service).get("/records/post-1")
    assert response.status_code == 200 and len(service.calls) == 1
    filters, kwargs = service.calls[0]
    assert filters.dataset == "native" and filters.record_ids == ["post-1"] and kwargs == {"limit": 1}
    assert "CLAIMS2 assignments are temporarily unavailable" in response.get_data(as_text=True)


def test_unavailable_or_unadmitted_record_stays_404_without_claims_or_annotation_call(monkeypatch):
    def no_annotation(_row):
        raise AssertionError("Missing records do not enter annotation rendering")

    monkeypatch.setattr(record_view, "social_annotation_details", no_annotation)
    service = NoSocialClaims()
    client = page(None, service)
    assert client.get("/records/unadmitted-post").status_code == 404
    assert service.calls == []
