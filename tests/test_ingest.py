import csv
import hashlib
import json
from pathlib import Path

import openpyxl

from observatory.ingest import (
    ARCHIVE_INDEX,
    ARCHIVE_REVIEW_BY_HASH,
    COMBINED_CSV,
    LABEL_FIELDS,
    LEGACY_CSV,
    NATIVE_CSV,
    NATIVE_METADATA,
    load_native,
    load_social,
)

BODY = (
    "The company describes its energy operations, planned projects, and the expected technical benefits. "
    * 20
)
FIELDS = [
    "publisher",
    "keyword",
    "url",
    "title",
    "Disclosure language",
    "sponsor",
    "date",
    "article",
    "is_video",
]


def write_csv(path: Path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def base_row(url="https://example.org/article", **changes):
    return {
        **dict(
            zip(
                FIELDS,
                [
                    "News",
                    "Energy",
                    url,
                    "Title",
                    "0",
                    "Company",
                    "23/08/2022",
                    BODY,
                    "0",
                ],
                strict=True,
            )
        ),
        **changes,
    }


def native_fixture(tmp_path, rows, metadata=None):
    write_csv(tmp_path / NATIVE_CSV, FIELDS, rows)
    path = tmp_path / NATIVE_METADATA
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["url", "disclosure language", "notes", "publisher"])
    for row in metadata or []:
        sheet.append(row)
    workbook.save(path)
    workbook.close()


def test_native_disclosure_date_body_and_source_provenance(tmp_path):
    rows = [base_row(), base_row("https://example.org/photo-400x400/", article="video")]
    native_fixture(
        tmp_path,
        rows,
        [
            [
                rows[0]["url"],
                "PAID POST",
                "URL date does not match the date in dataset",
                "News",
            ]
        ],
    )
    before = hashlib.sha256((tmp_path / NATIVE_CSV).read_bytes()).hexdigest()
    batch = load_native(tmp_path)
    first, asset = batch.records
    assert first.disclosure == "PAID POST"
    assert first.published_at is None and first.raw["baseline"]["date"] == "23/08/2022"
    assert first.countable and first.retrievable
    assert "disclosure_binary_mismatch" in {issue.code for issue in first.issues}
    assert first.provenance[0]["row"] == 2 and first.provenance[1]["row"] == 2
    assert first.provenance[0]["sha256"] == before
    assert not asset.countable and not asset.retrievable
    assert hashlib.sha256((tmp_path / NATIVE_CSV).read_bytes()).hexdigest() == before
    again = load_native(tmp_path)
    assert [r.model_dump() for r in batch.records] == [
        r.model_dump() for r in again.records
    ]


def test_pandera_bad_url_isolates_row_without_losing_valid_record(tmp_path):
    native_fixture(tmp_path, [base_row(), base_row("javascript:alert(1)")])
    batch = load_native(tmp_path)
    assert len(batch.records) == 1
    assert any(
        issue.code == "input_row_schema" and issue.row == 3 for issue in batch.rejected
    )


def test_native_duplicate_urls_are_all_isolated_not_first_row_wins(tmp_path):
    native_fixture(tmp_path, [base_row(), base_row(title="different title")])
    batch = load_native(tmp_path)
    assert batch.records == []
    assert {issue.row for issue in batch.rejected if issue.code == "duplicate_url"} == {
        2,
        3,
    }


def test_combined_additions_are_candidates_not_automatic_records(tmp_path):
    native_fixture(tmp_path, [base_row()])
    fields = ["url", "publisher", "title", "date", "sponsor", "keyword", "article"]
    added = {
        key: value
        for key, value in base_row("https://example.org/new").items()
        if key in fields
    }
    added["date"] = "1970-08-23"
    write_csv(tmp_path / COMBINED_CSV, fields, [added])
    batch = load_native(tmp_path)
    assert len(batch.records) == 1 and len(batch.candidates) == 1
    assert batch.candidates[0]["scope_status"] == "needs_review"
    assert "date_outlier" in {issue["code"] for issue in batch.candidates[0]["issues"]}


def test_historical_labels_require_url_and_exact_full_body(tmp_path):
    rows = [base_row(), base_row("https://example.org/second")]
    native_fixture(tmp_path, rows)
    fields = [
        "url",
        "text",
        "doc_id",
        *LABEL_FIELDS,
        *(field + "_cal" for field in LABEL_FIELDS),
    ]

    def labels(row, text):
        values = {"url": row["url"], "text": text, "doc_id": "0"}
        values.update({field: "True" for field in LABEL_FIELDS})
        values.update({field + "_cal": "False" for field in LABEL_FIELDS})
        return values

    write_csv(
        tmp_path / LEGACY_CSV,
        fields,
        [labels(rows[0], BODY), labels(rows[1], BODY + " ")],
    )
    batch = load_native(tmp_path)
    original, calibrated = batch.records[0].annotations
    assert original["version"] == "claims-original" and len(original["labels"]) == 12
    assert calibrated["version"] == "claims-calibrated" and calibrated["labels"] == []
    assert original["basis"] == "url_and_exact_body"
    assert batch.records[1].annotations == []
    assert any(issue.code == "legacy_identity_unverified" for issue in batch.rejected)


def test_social_mapping_short_posts_dates_and_row_quarantine(tmp_path):
    path = tmp_path / "posts.csv"
    write_csv(
        path,
        ["link", "text", "created", "author"],
        [
            {
                "link": "https://example.org/post/1",
                "text": "Which energy sources do we need?",
                "created": "2026-05-03",
                "author": "account",
            },
            {
                "link": "bad link",
                "text": "bad row",
                "created": "2026-05-03",
                "author": "account",
            },
            {
                "link": "https://example.org/post/2",
                "text": "A complete short post.",
                "created": "unknown date",
                "author": "account",
            },
        ],
    )
    mapping = {
        "columns": {
            "url": "link",
            "body": "text",
            "published_at": "created",
            "account": "author",
        },
        "constants": {"platform": "Twitter"},
    }
    batch = load_social(path, mapping)
    assert len(batch.records) == 2
    assert all(record.retrievable for record in batch.records)
    assert batch.records[1].published_at is None
    assert any(
        issue.row == 3 and issue.code == "input_row_schema" for issue in batch.rejected
    )
    assert (
        batch.records[0].raw["source_row"]["text"] == "Which energy sources do we need?"
    )
    assert len(load_social(path, {"url": "link"}).records) == 0


def test_social_missing_configured_column_does_not_guess(tmp_path):
    path = tmp_path / "posts.csv"
    write_csv(
        path, ["url", "body"], [{"url": "https://example.org/a", "body": "hello"}]
    )
    batch = load_social(
        path,
        {
            "columns": {"url": "url", "body": "post_text"},
            "constants": {"platform": "Twitter"},
        },
    )
    assert batch.records == []
    assert any(
        issue.code == "social_mapping_column_missing" for issue in batch.rejected
    )


def test_missing_six_field_header_is_a_structural_failure(tmp_path):
    path = tmp_path / NATIVE_CSV
    write_csv(
        path, ["url", "article"], [{"url": "https://example.org/a", "article": BODY}]
    )
    batch = load_native(tmp_path)
    assert batch.records == []
    assert any(issue.code == "input_schema" for issue in batch.rejected)


def test_related_navigation_limits_retrieval_without_rewriting_source(tmp_path):
    contaminated = (
        BODY + "For more on the subject: Unrelated biogas story. Later real text."
    )
    native_fixture(
        tmp_path,
        [
            base_row(article=contaminated),
            base_row(
                "https://example.org/no-body",
                article="For more on the subject: Other story",
            ),
        ],
    )
    first, second = load_native(tmp_path).records
    assert (
        first.body == contaminated and first.raw["baseline"]["article"] == contaminated
    )
    assert first.retrieval_end == len(BODY) and first.retrievable
    assert first.body[: first.retrieval_end] == BODY
    assert first.countable and second.countable
    assert second.retrieval_end == 0 and not second.retrievable


def test_archive_candidate_never_becomes_verified_or_replaces_body(tmp_path):
    row = base_row()
    native_fixture(tmp_path, [row])
    index = tmp_path / ARCHIVE_INDEX
    index.parent.mkdir(parents=True, exist_ok=True)
    digest = next(iter(ARCHIVE_REVIEW_BY_HASH))
    index.write_text(
        json.dumps(
            [
                {
                    "id": "PDF-006",
                    "path": "C:/archive/example.pdf",
                    "sha256": digest,
                    "source_urls": [row["url"]],
                    "source_match": "unique title match",
                    "low_text": False,
                    "text_head": "Different text must never replace the CSV.",
                },
                {
                    "id": "PDF-unrelated",
                    "path": "C:/archive/same-title.pdf",
                    "sha256": "a" * 64,
                    "source_urls": ["https://example.org/other"],
                    "title": row["title"],
                },
            ]
        ),
        encoding="utf-8",
    )
    batch = load_native(tmp_path)
    record = batch.records[0]
    candidates = [
        p for p in record.provenance if p["role"] == "internal_archive_candidate"
    ]
    assert len(candidates) == 1
    assert candidates[0]["match_state"] == "candidate_unverified"
    assert candidates[0]["archive_sha256"] == digest
    assert (
        candidates[0]["archive_hash_verification"] == "recorded_in_index_not_rehashed"
    )
    assert candidates[0]["review_reasons"]
    assert any(issue.code == "archive_review" for issue in record.issues)
    assert record.body == BODY and record.retrievable and record.countable
    assert record.archive_url == "" and record.retrieval_end is None
    assert ARCHIVE_INDEX.as_posix() in batch.source_hashes
