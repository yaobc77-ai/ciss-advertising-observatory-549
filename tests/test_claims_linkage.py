import csv
import json
import sqlite3
from dataclasses import replace

import pytest

from observatory.claims_linkage import (
    InputParagraph,
    LinkageIndex,
    SourceVersion,
    _write_evidence_review_csv,
    attach_claim_evidence,
    load_inputs,
    load_saved_results,
    source_fingerprint,
    text_hash,
    validate_retained_inputs,
)
from observatory.claims_taxonomy import TaxonomyBundle


def source(body, *, record="r1", version="v1", current=True, active=True,
           retrievable=True, allowed=None):
    return SourceVersion(record, version, body, text_hash(body), "https://example.org/ad", "native",
                         current, active, retrievable, tuple(allowed or [(0, len(body))]))


def saved(paragraph, **changes):
    return {"original_id": paragraph.source_id, "text": paragraph.text,
            "metadata_json": json.dumps({"article_id": paragraph.article_id}), **changes}


def result_with_quote(p, snippet):
    raw = {"created": 1776864000, "model": "recorded-model",
           "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "refusal": None,
                       "content": json.dumps({"responses": [{
                           "action_type": "match_existing_category", "matched_categories": ["NC_1"],
                           "new_categories": [], "updated_categories": [], "paragraph_number": "1",
                           "source_snippet": snippet, "super_claim": "SC_1", "rationale": "Candidate.",
                       }]})}}]}
    return saved(p, raw_response=json.dumps(raw))


def taxonomy_fixture(tmp_path):
    return TaxonomyBundle(tmp_path, {}, "0" * 64, {"NC_1": "Definition"}, {"SC_1": "Parent"},
                          {"NC_1": "SC_1"}, {"NC_1": "SC_1"}, {}, "2026-01-01T00:00:00", ())


def attach(index, inputs, results, tmp_path, run_id="saved-fixture"):
    rows, _ = index.audit(inputs, results)
    return attach_claim_evidence(index, rows, results, inputs, taxonomy_fixture(tmp_path), run_id=run_id)


def test_label_quote_cannot_escape_its_original_input(tmp_path):
    p = InputParagraph("1", "1", "This paragraph has no selected quotation.", 2)
    body = p.text + " Quotation only in a different paragraph."
    candidates, summary = attach(LinkageIndex([source(body)]), [p],
                                 [result_with_quote(p, "Quotation only in a different paragraph.")], tmp_path)
    claim = candidates[0]["claims"][0]
    assert claim["quote_state"] == "snippet_not_in_input"
    assert claim["source_evidence_candidates"] == []
    assert summary["results_published"] == 0


def test_quote_and_run_have_stable_keys_with_original_offsets(tmp_path):
    p = InputParagraph("1", "1", "Company cuts emissions.", 2)
    body = "🧪 Company\n cuts emissions."
    index = LinkageIndex([source(body)])
    result = result_with_quote(p, "cuts emissions.")
    first, _ = attach(index, [p], [result], tmp_path)
    repeated, _ = attach(index, [p], [result], tmp_path)
    another_run, _ = attach(index, [p], [result], tmp_path, run_id="another-run")
    quote = first[0]["claims"][0]["source_evidence_candidates"][0]
    assert first[0]["claims"][0]["quote_state"] == "unique_current_quote_candidate"
    assert body[quote["start"]:quote["end"]] == quote["quote"] == "cuts emissions."
    assert quote["candidate_key"] == repeated[0]["claims"][0]["source_evidence_candidates"][0]["candidate_key"]
    assert quote["candidate_key"] != another_run[0]["claims"][0]["source_evidence_candidates"][0]["candidate_key"]
    assert first[0]["prompt_identifier"] is None
    assert not first[0]["association_is_approved"]


def test_partial_source_does_not_become_full_article_quote_association(tmp_path):
    p = InputParagraph("1", "1", "Company cuts emissions.", 2)
    absent = InputParagraph("2", "1", "Missing retained context.", 3)
    candidates, _ = attach(LinkageIndex([source(p.text)]), [p, absent],
                           [result_with_quote(p, "cuts emissions.")], tmp_path)
    assert candidates[0]["claims"][0]["quote_state"] == "unique_current_partial_article_quote"
    assert not candidates[0]["claims"][0]["association_is_approved"]


def test_quote_in_excluded_interval_is_not_eligible(tmp_path):
    p = InputParagraph("1", "1", "Approved text. Footer claim.", 2)
    candidates, _ = attach(LinkageIndex([source(p.text, allowed=[(0, 14)])]), [p],
                           [result_with_quote(p, "Footer claim.")], tmp_path)
    assert candidates[0]["claims"][0]["quote_state"] == "excluded_current_quote"


def test_projection_is_explicit_and_original_quote_is_never_ascii_rewritten(tmp_path):
    p = InputParagraph("1", "1", 'Company says "lower emissions" today.', 2)
    body = 'Company says “lower emissions” today.'
    strict, _ = attach(LinkageIndex([source(body)]), [p], [result_with_quote(p, '"lower emissions"')], tmp_path)
    projected, _ = attach(LinkageIndex([source(body)], projection="upstream-ascii-v1"), [p],
                          [result_with_quote(p, '"lower emissions"')], tmp_path)
    assert strict[0]["claims"][0]["quote_state"] == "snippet_not_in_original"
    evidence = projected[0]["claims"][0]["source_evidence_candidates"][0]
    assert evidence["quote"] == '“lower emissions”' and evidence["lossy"]
    assert evidence["match_method"] == "upstream_ascii_projection_v1"


def test_review_export_retains_unclassified_rows_and_blank_human_review(tmp_path):
    path = tmp_path / "review.csv"
    _write_evidence_review_csv(path, [{
        "original_id": "1", "report_state": "needs_review", "claims": (),
        "errors": ({"code": "no_reviewable_claims"},),
    }])
    with path.open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["report_state"] == "needs_review"
    assert row["nc_id"] == ""
    assert row["issue_codes"] == "no_reviewable_claims"
    assert row["source_identity_review"] == row["semantic_support_review"] == "pending"
    assert row["reviewer"] == row["reviewed_at"] == ""


def test_numeric_id_does_not_choose_an_article():
    paragraph = InputParagraph("1", "1", "Company makes a claim.", 2)
    versions = [source("Unrelated source.", record="1"), source(paragraph.text, record="other", version="v2")]
    rows, _ = LinkageIndex(versions).audit([paragraph], [saved(paragraph)])
    assert rows[0]["state"] == "unique_current_text_candidate"
    assert rows[0]["candidates"][0]["record_id"] == "other"
    assert not rows[0]["association_is_approved"]


def test_article_anchor_disambiguates_shared_paragraph_but_retains_all_candidates():
    generic = InputParagraph("1", "10", "Shared message.", 2)
    anchor = InputParagraph("2", "10", "Unique company context.", 3)
    versions = [source(generic.text + "\n" + anchor.text),
                source(generic.text + "\nOther context.", record="r2", version="v2")]
    rows, articles = LinkageIndex(versions).audit([generic, anchor], [saved(generic), saved(anchor)])
    assert rows[0]["state"] == "unique_current_text_candidate"
    assert len(rows[0]["candidates"]) == 2
    assert articles[0]["current_record_candidates"] == ["r1"]
    assert not articles[0]["input_is_complete_original_article"]


def test_unlocated_retained_paragraph_prevents_forced_article_assignment():
    first = InputParagraph("1", "10", "Unique message.", 2)
    missing = InputParagraph("2", "10", "Absent source paragraph.", 3)
    rows, articles = LinkageIndex([source(first.text)]).audit([first, missing], [saved(first)])
    assert rows[0]["state"] == "partial_article_candidate"
    assert articles[0]["current_record_candidates"] == []
    assert articles[0]["best_partial_coverage"] == 1


def test_input_order_is_not_equivalent_to_a_bag_of_paragraphs():
    first = InputParagraph("1", "10", "First context.", 2)
    second = InputParagraph("2", "10", "Second context.", 3)
    rows, articles = LinkageIndex([source(second.text + "\n" + first.text)]).audit(
        [first, second], [saved(first), saved(second)],
    )
    assert articles[0]["current_record_candidates"] == []
    assert {row["state"] for row in rows} == {"partial_article_candidate"}


def test_repeated_occurrences_are_ambiguous_not_first_match():
    p = InputParagraph("1", "1", "Repeated claim.", 2)
    rows, _ = LinkageIndex([source(p.text + " " + p.text)]).audit([p], [saved(p)])
    assert rows[0]["state"] == "ambiguous_current"
    assert len(rows[0]["candidates"]) == 2


def test_distinct_records_with_identical_bodies_are_ambiguous():
    p = InputParagraph("1", "1", "Same entire text.", 2)
    versions = [source(p.text), source(p.text, record="r2", version="v2")]
    rows, _ = LinkageIndex(versions).audit([p], [saved(p)])
    assert rows[0]["state"] == "ambiguous_current"


def test_metadata_only_old_versions_do_not_create_current_ambiguity():
    p = InputParagraph("1", "1", "Same original text.", 2)
    versions = [source(p.text), source(p.text, version="old", current=False)]
    rows, _ = LinkageIndex(versions).audit([p], [saved(p)])
    assert rows[0]["state"] == "unique_current_text_candidate"
    assert rows[0]["candidate_version_ids"] == ["old", "v1"]


@pytest.mark.parametrize("changes", [{"current": False}, {"active": False}])
def test_old_or_inactive_source_is_preserved_but_not_current(changes):
    p = InputParagraph("1", "1", "Prior version claim.", 2)
    rows, _ = LinkageIndex([source(p.text, **changes)]).audit([p], [saved(p)])
    assert rows[0]["state"] == "historical_only_or_inactive"


def test_excluded_intervals_do_not_become_eligible_evidence():
    p = InputParagraph("1", "1", "Footer claim.", 2)
    body = "Approved body. " + p.text
    rows, _ = LinkageIndex([source(body, allowed=[(0, 14)])]).audit([p], [saved(p)])
    assert rows[0]["state"] == "excluded_or_unretrievable"
    assert not rows[0]["candidates"][0]["inside_retrieval_scope"]


def test_whitespace_projection_offsets_slice_original_characters():
    p = InputParagraph("1", "1", "Company cuts emissions.", 2)
    body = "🧪 Company\n\t cuts\u00a0emissions."
    rows, _ = LinkageIndex([source(body)]).audit([p], [saved(p)])
    candidate = rows[0]["candidates"][0]
    assert candidate["quote"] == body[candidate["start"]:candidate["end"]]
    assert candidate["match_method"] == "whitespace_normalized"


@pytest.mark.parametrize("changes", [
    {"text": "Different input."}, {"metadata_json": '{"article_id":"other"}'},
    {"metadata_json": "broken"}, {"original_id": "absent"},
    {"metadata_json": '{"article_id":"other","article_id":"1"}'},
    {"metadata_json": '{"article_id":1.0}'},
    {"metadata_json": '{"article_id":true}'},
])
def test_invalid_input_join_cannot_be_used(changes):
    p = InputParagraph("1", "1", "Original input.", 2)
    rows, _ = LinkageIndex([source(p.text)]).audit([p], [saved(p, **changes)])
    assert rows[0]["state"] == "input_mismatch"
    assert not rows[0]["input_join_valid"]
    assert rows[0]["candidates"] == []


def test_body_hash_and_duplicate_versions_are_rejected():
    s = source("Original body.")
    with pytest.raises(ValueError, match="stored hash"):
        replace(s, body="Changed body.")
    with pytest.raises(ValueError, match="Duplicate source"):
        LinkageIndex([s, s])


def test_fingerprint_tracks_source_scope_and_current_version():
    s = source("Original body.")
    assert source_fingerprint([s]) == source_fingerprint([s])
    assert source_fingerprint([s]) != source_fingerprint([replace(s, current=False)])
    assert source_fingerprint([s]) != source_fingerprint([replace(s, allowed_spans=((0, 5),))])


def test_csv_contract_rejects_duplicate_ids(tmp_path):
    path = tmp_path / "inputs.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerows([["id", "article_id", "text"], ["1", "10", "First."], ["1", "11", "Second."]])
    with pytest.raises(ValueError, match="Duplicate paragraph"):
        load_inputs(path)


@pytest.mark.parametrize("row", ['1,10,First.,unexpected\n', '1,10\n'])
def test_csv_contract_rejects_wrong_row_width(tmp_path, row):
    path = tmp_path / "inputs.csv"
    path.write_text('id,article_id,text\n' + row, encoding="utf-8")
    with pytest.raises(ValueError, match="row width"):
        load_inputs(path)


def test_cleaned_article_id_is_not_the_sample_paragraph_id(tmp_path):
    path = tmp_path / "cleaned.csv"
    path.write_text('id,text\n10,First.\n10,Second.\n11,Other.\n', encoding="utf-8")
    sampled = [InputParagraph("1", "10", "First.", 2), InputParagraph("2", "10", "Second.", 3)]
    receipt = validate_retained_inputs(sampled, path)
    assert receipt["sampled_article_groups"] == 1
    assert receipt["cleaned_article_groups"] == 2
    assert receipt["complete_original_article_coverage"] == "not_established"
    with pytest.raises(ValueError, match="original order"):
        validate_retained_inputs(list(reversed(sampled)), path)


def test_sqlite_loader_is_read_only_and_missing_file_is_not_created(tmp_path):
    absent = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        load_saved_results(absent)
    assert not absent.exists()
    path = tmp_path / "saved.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE post_analysis (original_id TEXT PRIMARY KEY,text TEXT,metadata_json TEXT,"
                     "raw_response TEXT,matched_categories TEXT,new_categories TEXT,updated_categories TEXT,source_snippet TEXT)")
        conn.execute("INSERT INTO post_analysis (original_id,text) VALUES ('1','Original.')")
    before = path.read_bytes()
    assert load_saved_results(path)[0]["original_id"] == "1"
    assert path.read_bytes() == before
