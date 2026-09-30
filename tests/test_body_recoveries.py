"""Partial recoveries must stay source-bound and never inherit old coordinates."""

import copy
import hashlib
import json

import pytest
from test_ingest import BODY, base_row, native_fixture

from observatory.body_recoveries import BODY_RECOVERIES_PATH, apply_body_recoveries
from observatory.chunking import chunk_retrieval_body
from observatory.ingest import NATIVE_CSV, load_native
from observatory.models import Issue
from observatory.quality import body_hash


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save_manifest(root, manifest):
    path = root / BODY_RECOVERIES_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def recovery_fixture(root, *, count=1, text=None):
    old_body = (BODY * 2)[:2995] + "..."
    native_fixture(root, [
        base_row(f"https://example.org/article-{i}", article=old_body)
        for i in range(count)
    ])
    batch = load_native(root)
    pdf_path, text_path = "sources/recovery.pdf", "sources/recovery.txt"
    pdf = b"%PDF-1.7\nSynthetic bytes used only for hash-binding tests.\n"
    (root / pdf_path).write_bytes(pdf)
    first = "Recovered café and 中文 research observations. " * 12
    navigation = "\nFor more on the subject: ANOTHER ARTICLE\r\n\f"
    last = "Later same-article evidence explains limitations and monitoring costs. " * 12
    extracted = first + navigation + last + "\f" if text is None else text + "\f"
    (root / text_path).write_bytes(extracted.encode("utf-8"))
    split = len(first + navigation)
    manifest = {
        "schema_version": 1,
        "review_id": "synthetic-recovery-v1",
        "reviewer_type": "ai",
        "source_path": NATIVE_CSV.as_posix(),
        "source_sha256": batch.source_hashes[NATIVE_CSV.as_posix()],
        "decisions": [],
    }
    for i, record in enumerate(batch.records):
        record.retrieval_end = 91
        record.retrieval_ranges = [(0, 40), (70, 91)]
        record.raw["body_review"] = {"old_review": True, "body_sha256": body_hash(old_body)}
        record.annotations = [{
            "version": "claims-calibrated", "basis": "url_and_exact_body",
            "body_sha256": body_hash(old_body), "labels": ["green_labels.green_binary"],
            "source": "old-labels.csv", "source_sha256": "c" * 64,
        }]
        record.provenance.append({"role": "internal_archive_candidate", "match_state": "candidate_unverified"})
        record.issues.extend([
            Issue(code="body_navigation_intervals_reviewed", severity="info", detail="Old CSV intervals."),
            Issue(code="archive_candidate_unverified", severity="info", detail="Candidate association."),
        ])
        pages = [
            {"page": 1, "start": 0, "end": split},
            {"page": 2, "start": split, "end": len(extracted)},
        ] if text is None else [{"page": 1, "start": 0, "end": len(extracted)}]
        ranges = [[0, len(first)], [split, len(extracted) - 1]] if text is None else [[0, len(extracted)]]
        manifest["decisions"].append({
            "record_id": record.record_id, "url": record.url, "source_row": i + 2,
            "previous_body_sha256": body_hash(record.body),
            "source_pdf_path": pdf_path, "source_pdf_sha256": sha(pdf),
            "extracted_text_path": text_path, "extracted_text_sha256": sha(extracted.encode("utf-8")),
            "extraction_method": "Synthetic UTF-8 page extraction fixture, no PDF parser invoked",
            "pages": pages, "retained_ranges": ranges,
            "identity_basis": "Synthetic content-match review; does not prove a canonical URL",
            "evidence_refs": ["synthetic-review#record"],
            "limitations": ["Illustration text is unavailable.", "Navigation may obscure source text."],
        })
    save_manifest(root, manifest)
    return batch, manifest, extracted


def test_partial_recovery_preserves_prior_text_labels_metadata_and_coordinates(tmp_path):
    batch, manifest, text = recovery_fixture(tmp_path)
    original = batch.records[0]
    before = batch.records[0].model_dump(mode="json")
    files_before = {p: (tmp_path / p).read_bytes() for p in [
        NATIVE_CSV, "sources/recovery.pdf", "sources/recovery.txt",
    ]}
    apply_body_recoveries(tmp_path, batch, required=True)
    record = batch.records[0]
    item = manifest["decisions"][0]
    assert record.body == text and "\r\n\f" in record.body
    assert record.retrievable and record.countable
    assert record.retrieval_end is None
    assert record.retrieval_ranges == [tuple(r) for r in item["retained_ranges"]]
    assert record.retrieval_ranges != before["retrieval_ranges"]
    assert record.raw["baseline"] == before["raw"]["baseline"]
    assert record.raw["previous_body"] == before["body"]
    assert record.raw["previous_body_annotations"] == before["annotations"]
    assert record.annotations == []
    assert record.raw["previous_body_review"] == before["raw"]["body_review"]
    assert "body_review" not in record.raw
    assert record.raw["body_sha256"] == sha(text.encode("utf-8"))
    review = record.raw["body_recovery"]
    assert review["pages"] == item["pages"] and review["coverage"] == "partial"
    assert review["reviewer_type"] == "ai"
    assert review["previous_retrieval_end"] == 91
    assert review["previous_retrieval_ranges"] == before["retrieval_ranges"]
    assert review["previous_body_sha256"] == before["raw"]["body_sha256"]
    for key in ["record_id", "dataset", "url", "publisher", "title", "published_at",
                "sponsor", "keyword", "platform", "account", "disclosure", "archive_url"]:
        assert record.model_dump(mode="json")[key] == before[key]
    assert record.provenance[:-1] == before["provenance"]
    assert record.provenance[-1]["role"] == "content_match_reviewed"
    assert record.provenance[-1]["basis"] == item["identity_basis"]
    codes = {i.code for i in record.issues}
    assert {"body_truncated_suspected", "body_partial_recovery",
            "historical_annotations_prior_body", "archive_candidate_unverified"} <= codes
    assert "body_navigation_intervals_reviewed" not in codes
    old_issue = next(i for i in original.issues if i.code == "body_truncated_suspected")
    new_issue = next(i for i in record.issues if i.code == "body_truncated_suspected")
    assert new_issue is not old_issue
    assert new_issue.detail == "Previous CSV body: " + old_issue.detail
    assert not old_issue.detail.startswith("Previous CSV body: ")
    assert all((tmp_path / p).read_bytes() == data for p, data in files_before.items())
    assert batch.source_hashes["sources/recovery.txt"] == item["extracted_text_sha256"]
    assert batch.source_hashes[BODY_RECOVERIES_PATH.as_posix()] == sha((tmp_path / BODY_RECOVERIES_PATH).read_bytes())


def test_recovered_chunks_never_include_navigation_or_bridge_removed_gap(tmp_path):
    batch, manifest, text = recovery_fixture(tmp_path)
    apply_body_recoveries(tmp_path, batch)
    record = batch.records[0]
    chunks = chunk_retrieval_body(record.body, 73, 13, retrieval_ranges=record.retrieval_ranges)
    assert len(chunks) > 2
    assert any("Later same-article evidence" in chunk["text"] for chunk in chunks)
    for chunk in chunks:
        assert chunk["text"] == text[chunk["start"]:chunk["end"]]
        assert "ANOTHER ARTICLE" not in chunk["text"]
        assert any(start <= chunk["start"] < chunk["end"] <= end for start, end in record.retrieval_ranges)


@pytest.mark.parametrize("reason", ["metadata_only", "out_of_scope", "garbled", "short"])
def test_recovery_cannot_bypass_scope_or_body_quality(tmp_path, reason):
    text = None
    if reason == "garbled":
        text = BODY + "Broken replacement \ufffd remains in selected text."
    elif reason == "short":
        text = "A short title."
    batch, _, _ = recovery_fixture(tmp_path, text=text)
    if reason == "metadata_only":
        batch.records[0].raw["admission_review"] = {"body_mode": "metadata_only"}
    elif reason == "out_of_scope":
        batch.records[0].countable = False
    apply_body_recoveries(tmp_path, batch)
    assert not batch.records[0].retrievable
    assert batch.records[0].raw["body_recovery"]["coverage"] == "partial"


@pytest.mark.parametrize("mutation", [
    "baseline_hash", "baseline_bytes", "pdf_hash", "pdf_bytes", "text_hash", "text_bytes",
    "invalid_utf8", "body", "identity", "row", "missing_record", "duplicate_record",
    "duplicate_batch_record", "duplicate_page", "page_gap", "page_overlap", "page_first",
    "page_end", "page_shift_contiguous", "missing_formfeed", "embedded_formfeed",
    "empty_pages", "duplicate_range", "range_overlap", "range_outside",
    "range_bool", "empty_ranges", "missing_pdf", "missing_text",
])
def test_drift_or_invalid_mapping_fails_before_any_record_or_hash_change(tmp_path, mutation):
    batch, manifest, _ = recovery_fixture(tmp_path, count=2)
    # The invalid second decision must not partially apply the first one.
    item = manifest["decisions"][1]
    if mutation == "baseline_hash":
        manifest["source_sha256"] = "0" * 64
    elif mutation == "baseline_bytes":
        with (tmp_path / NATIVE_CSV).open("ab") as handle:
            handle.write(b"\n")
    elif mutation in {"pdf_hash", "text_hash"}:
        item["source_pdf_sha256" if mutation == "pdf_hash" else "extracted_text_sha256"] = "0" * 64
    elif mutation in {"pdf_bytes", "text_bytes"}:
        (tmp_path / ("sources/recovery.pdf" if mutation == "pdf_bytes" else "sources/recovery.txt")).write_bytes(b"drift")
    elif mutation == "invalid_utf8":
        data = b"\xffinvalid UTF-8"
        (tmp_path / "sources/recovery.txt").write_bytes(data)
        for decision in manifest["decisions"]:
            decision["extracted_text_sha256"] = sha(data)
    elif mutation == "body":
        item["previous_body_sha256"] = "0" * 64
    elif mutation == "identity":
        item["url"] = "https://example.org/wrong"
    elif mutation == "row":
        item["source_row"] = 80
    elif mutation == "missing_record":
        item["record_id"] = "missing-record"
    elif mutation == "duplicate_record":
        manifest["decisions"].append(copy.deepcopy(item))
    elif mutation == "duplicate_batch_record":
        batch.records.append(batch.records[-1])
    elif mutation == "duplicate_page":
        item["pages"][1]["page"] = 1
    elif mutation == "page_gap":
        item["pages"][1]["start"] += 1
    elif mutation == "page_overlap":
        item["pages"][1]["start"] -= 1
    elif mutation == "page_first":
        item["pages"][0]["start"] = 1
    elif mutation == "page_end":
        item["pages"][-1]["end"] -= 1
    elif mutation == "page_shift_contiguous":
        item["pages"][0]["end"] += 10
        item["pages"][1]["start"] += 10
    elif mutation in {"missing_formfeed", "embedded_formfeed"}:
        data = (tmp_path / "sources/recovery.txt").read_bytes()
        data = data.replace(b"\f", b" ", 1) if mutation == "missing_formfeed" else b"\f" + data[1:]
        (tmp_path / "sources/recovery.txt").write_bytes(data)
        for decision in manifest["decisions"]:
            decision["extracted_text_sha256"] = sha(data)
    elif mutation == "empty_pages":
        item["pages"] = []
    elif mutation == "duplicate_range":
        item["retained_ranges"].append(item["retained_ranges"][-1])
    elif mutation == "range_overlap":
        item["retained_ranges"][1][0] = 1
    elif mutation == "range_outside":
        item["retained_ranges"][-1][1] = 999999
    elif mutation == "range_bool":
        item["retained_ranges"][0][0] = False
    elif mutation == "empty_ranges":
        item["retained_ranges"] = []
    elif mutation == "missing_pdf":
        (tmp_path / item["source_pdf_path"]).unlink()
    elif mutation == "missing_text":
        (tmp_path / item["extracted_text_path"]).unlink()
    save_manifest(tmp_path, manifest)
    before = batch.model_dump(mode="json")
    with pytest.raises(ValueError):
        apply_body_recoveries(tmp_path, batch, required=True)
    assert batch.model_dump(mode="json") == before


@pytest.mark.parametrize("bad_path", ["../outside.txt", "sources/../../outside.txt", "C:/outside.txt", "C:outside.txt", "/outside.txt", "\\\\server\\share\\text.txt"])
def test_recovery_paths_must_stay_project_relative(tmp_path, bad_path):
    batch, manifest, _ = recovery_fixture(tmp_path)
    manifest["decisions"][0]["extracted_text_path"] = bad_path
    save_manifest(tmp_path, manifest)
    before = batch.model_dump()
    with pytest.raises(ValueError, match="project-relative"):
        apply_body_recoveries(tmp_path, batch)
    assert batch.model_dump() == before


def test_resolved_external_asset_is_rejected_without_reading_it(tmp_path, monkeypatch):
    batch, _, _ = recovery_fixture(tmp_path)
    from observatory import body_recoveries

    original_resolve = body_recoveries.Path.resolve
    def resolve(path, *args, **kwargs):
        if path.name == "recovery.txt":
            return tmp_path.parent / "external.txt"
        return original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(body_recoveries.Path, "resolve", resolve)
    before = batch.model_dump()
    with pytest.raises(ValueError, match="escapes"):
        apply_body_recoveries(tmp_path, batch)
    assert batch.model_dump() == before


def test_optional_missing_manifest_and_required_missing_manifest(tmp_path):
    native_fixture(tmp_path, [base_row()])
    original = load_native(tmp_path)
    assert original.records
    before = original.model_dump()
    apply_body_recoveries(tmp_path, original)
    assert original.model_dump() == before
    with pytest.raises(ValueError, match="Required body recovery manifest"):
        load_native(tmp_path, require_body_recoveries=True)


def test_native_loader_applies_recovery_after_old_interval_review_and_is_repeatable(tmp_path):
    from observatory.body_reviews import BODY_REVIEWS_PATH

    _, manifest, text = recovery_fixture(tmp_path)
    item = manifest["decisions"][0]
    old_review = {
        "schema_version": 1, "review_id": "earlier-interval-review", "reviewer_type": "ai",
        "source_path": manifest["source_path"], "source_sha256": manifest["source_sha256"],
        "decisions": [{"record_id": item["record_id"], "url": item["url"],
                       "source_row": item["source_row"], "body_sha256": item["previous_body_sha256"],
                       "retained_ranges": [[0, 1700]], "reason": "Original CSV partial interval.",
                       "evidence_refs": ["old fixture review"]}],
    }
    (tmp_path / BODY_REVIEWS_PATH).write_text(json.dumps(old_review), encoding="utf-8")
    first = load_native(tmp_path, require_body_reviews=True, require_body_recoveries=True)
    second = load_native(tmp_path, require_body_reviews=True, require_body_recoveries=True)
    assert first.model_dump() == second.model_dump()
    assert first.records[0].body == text
    assert first.records[0].raw["previous_body_review"]["manifest_id"] == "earlier-interval-review"
    assert first.records[0].raw["body_recovery"]["previous_retrieval_ranges"] == [[0, 1700]]


def test_cli_requires_all_manifests_before_snapshot_import(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from observatory import cli, ingest

    native_fixture(tmp_path, [base_row()])
    observed = {}
    def load(root, **kwargs):
        observed.update(kwargs)
        raise ValueError("Required body recovery manifest is missing")
    def no_database(*args, **kwargs):
        pytest.fail("Invalid source recovery must fail before database import")
    monkeypatch.setattr(ingest, "load_native", load)
    monkeypatch.setattr(cli.Settings, "from_env", lambda: SimpleNamespace(database_url=""))
    monkeypatch.setattr(cli.Database, "import_batch", no_database)
    monkeypatch.setattr("sys.argv", ["observatory", "import-native", "--root", str(tmp_path)])
    with pytest.raises(ValueError, match="Required body recovery manifest"):
        cli.main()
    assert observed == {"require_admissions": True, "require_body_reviews": True,
                        "require_body_recoveries": True}
