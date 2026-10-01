"""Private source review packets bind synthetic evidence without fetching or approval."""

import csv
import hashlib
import io
import json
from pathlib import Path

import pytest

from observatory.claims_source_review import write_source_review_packet

EXCERPT = "Our carbon capture project plans to reduce emissions across the industrial region."
URL = "https://www.example.org/advertising/carbon-capture"
SECOND_URL = "https://publisher.example.org/advertising/another-page"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def put_json(path, value):
    path.write_bytes((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def read_json(path):
    return json.loads(path.read_bytes().decode("utf-8"))


def write_csv(path, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(["id", "article_id", "text"])
    writer.writerows(rows)
    path.write_bytes(stream.getvalue().encode("utf-8"))


def files(tmp_path, *, text=None, excerpt=EXCERPT, candidates=None):
    source = tmp_path / "input.csv"
    lookup = tmp_path / "lookup.json"
    original = text if text is not None else "🛢️中文 introduction.\r\n" + excerpt + "\r\nConclusion."
    write_csv(source, [("1", "article-a", "Unselected original paragraph."), ("2", "100", original)])
    receipt = {
        "tool": "find_claims_source_candidates",
        "policy_version": "claims-source-discovery-v1",
        "excerpt": excerpt,
        "excerpt_sha256": sha(excerpt.encode("utf-8")),
        "status": "ok",
        "identity_verified": False,
        "candidates": candidates if candidates is not None else [
            {"url": URL, "title": "Possible publisher article", "source_kind": "external_search",
             "provider_source": "url_citation", "source_association": "needs_review"},
        ],
        "searched_at": "2026-09-30T12:00:00Z",
    }
    put_json(lookup, receipt)
    return {"source": source, "lookup": lookup, "out": tmp_path / "packet",
            "original": original, "receipt": receipt}


def capture(files, *, text=None, completeness="unknown", candidate_url=URL, **metadata):
    root = files["source"].parent / "captures"
    root.mkdir(exist_ok=True)
    text_file = root / "original.txt"
    text_file.write_bytes((text if text is not None else "🛢️ prefix\r\n" + EXCERPT + "\r\nSuffix.").encode("utf-8"))
    manifest = root / "captures.json"
    value = {"schema_version": "claims-source-captures-v1", "captures": [{
        "candidate_url": candidate_url, "text_file": "original.txt",
        "captured_at": "2026-09-30T13:00:00+00:00", "capture_method": "synthetic saved text",
        "completeness": completeness, **metadata,
    }]}
    put_json(manifest, value)
    files.update(capture_manifest=manifest, capture_text=text_file, capture_value=value)
    return manifest


def run(files, **kwargs):
    return write_source_review_packet(files["source"], "2", files["lookup"], files["out"], **kwargs)


def candidate(files):
    return read_json(files["out"] / "packet.json")["candidates"][0]


def values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from values(item)
    elif isinstance(value, list):
        for item in value:
            yield from values(item)
    else:
        yield value


def test_packet_freezes_receipt_hashes_every_artifact_and_keeps_reviews_pending(tmp_path):
    item = files(tmp_path)
    before = {name: item[name].read_bytes() for name in ("source", "lookup")}
    summary = run(item)
    assert isinstance(summary, dict)
    assert item["source"].read_bytes() == before["source"]
    assert item["lookup"].read_bytes() == before["lookup"]
    required = {"source_input.json", "lookup_receipt.json", "packet.json", "review.json", "review.html", "packet_manifest.json"}
    assert required <= {path.name for path in item["out"].iterdir()}
    assert (item["out"] / "lookup_receipt.json").read_bytes() == before["lookup"]
    manifest = read_json(item["out"] / "packet_manifest.json")
    assert manifest["complete"] is True
    actual = {path.relative_to(item["out"]).as_posix(): sha(path.read_bytes())
              for path in item["out"].rglob("*") if path.is_file() and path.name != "packet_manifest.json"}
    assert manifest["artifacts"] == actual
    source_input = read_json(item["out"] / "source_input.json")
    assert item["original"] in set(values(source_input))
    assert sha(before["source"]) in set(values(source_input))
    row = candidate(item)
    assert row["comparison_state"] == "not_captured"
    assert row["match_count"] == 0 and row["locations"] == []
    review_values = set(value for value in values(read_json(item["out"] / "review.json")) if isinstance(value, str))
    assert "pending" in review_values
    assert not review_values.intersection({"confirmed", "supported", "approved", "publish"})


def test_unique_capture_retains_unicode_character_offsets_and_original_crlf_bytes(tmp_path):
    item = files(tmp_path)
    text = "🛢️中文 before.\r\n" + EXCERPT + "\r\nAfter."
    manifest = capture(item, text=text, final_url=SECOND_URL, title="Captured page", publisher="Publisher")
    raw = item["capture_text"].read_bytes()
    run(item, captures=manifest)
    row = candidate(item)
    assert row["comparison_state"] == "unique_excerpt" and row["match_count"] == 1
    span = row["locations"][0]
    assert span["start"] == text.index(EXCERPT) and span["end"] == text.index(EXCERPT) + len(EXCERPT)
    assert span["quote"] == text[span["start"]:span["end"]] == EXCERPT
    assert span["match_method"] == "exact"
    copied = list((item["out"] / "captured-text").glob("*.txt"))
    assert len(copied) == 1 and copied[0].read_bytes() == raw
    assert item["capture_text"].read_bytes() == raw


def test_layout_only_match_preserves_exact_capture_slice(tmp_path):
    item = files(tmp_path)
    original_quote = EXCERPT.replace("carbon capture", "carbon\t\r\ncapture").replace("industrial region", "industrial\u00a0region")
    text = "prefix " + original_quote + " suffix"
    run(item, captures=capture(item, text=text))
    row = candidate(item)
    assert row["comparison_state"] == "unique_excerpt" and row["match_count"] == 1
    span = row["locations"][0]
    assert span["quote"] == original_quote == text[span["start"]:span["end"]]
    assert span["match_method"] == "whitespace_normalized"


def test_repeated_capture_matches_are_not_silently_selected(tmp_path):
    item = files(tmp_path)
    text = EXCERPT + "\n" + EXCERPT.replace("carbon capture", "carbon\tcapture") + "\n" + EXCERPT
    run(item, captures=capture(item, text=text))
    row = candidate(item)
    assert row["comparison_state"] == "repeated_excerpt" and row["match_count"] == 3
    assert len(row["locations"]) == 3
    assert [span["match_method"] for span in row["locations"]] == ["exact", "whitespace_normalized", "exact"]
    for span in row["locations"]:
        assert text[span["start"]:span["end"]] == span["quote"]


def test_overlap_capture_matches_remain_distinct(tmp_path):
    excerpt = "a" * 40
    item = files(tmp_path, excerpt=excerpt)
    run(item, captures=capture(item, text="a" * 42))
    row = candidate(item)
    assert row["comparison_state"] == "repeated_excerpt" and row["match_count"] == 3
    assert [(span["start"], span["end"]) for span in row["locations"]] == [(0, 40), (1, 41), (2, 42)]


def test_match_count_is_complete_when_stored_locations_are_bounded(tmp_path):
    item = files(tmp_path)
    text = "\n".join([EXCERPT] * 103)
    run(item, captures=capture(item, text=text))
    row = candidate(item)
    assert row["comparison_state"] == "repeated_excerpt" and row["match_count"] == 103
    assert len(row["locations"]) == 100
    assert len({(span["start"], span["end"]) for span in row["locations"]}) == 100


@pytest.mark.parametrize("text", [
    "A related carbon capture article without the excerpt.",
    EXCERPT.lower(),
    EXCERPT.replace("industrial region.", "industrial-region."),
])
def test_subject_case_or_lexical_similarity_cannot_create_excerpt_match(tmp_path, text):
    item = files(tmp_path)
    run(item, captures=capture(item, text=text))
    row = candidate(item)
    assert row["comparison_state"] == "no_match" and row["match_count"] == 0 and row["locations"] == []


@pytest.mark.parametrize("completeness", ["unknown", "partial", "complete"])
def test_capture_completeness_never_approves_article_identity(tmp_path, completeness):
    item = files(tmp_path)
    run(item, captures=capture(item, completeness=completeness))
    row = candidate(item)
    assert row["comparison_state"] == "unique_excerpt"
    review_values = set(value for value in values(read_json(item["out"] / "review.json")) if isinstance(value, str))
    assert "pending" in review_values
    assert not review_values.intersection({"confirmed", "supported", "approved", "publish"})


def test_repeated_excerpt_in_legacy_input_requires_an_explicit_original_offset(tmp_path):
    item = files(tmp_path, text=EXCERPT + "\n" + EXCERPT)
    with pytest.raises(ValueError):
        run(item)
    assert not item["out"].exists()
    run(item, excerpt_start=len(EXCERPT) + 1)
    assert item["out"].is_dir()


@pytest.mark.parametrize("start", [-1, True, 1, 10000])
def test_invalid_explicit_excerpt_offset_cannot_bind_another_input_slice(tmp_path, start):
    item = files(tmp_path)
    with pytest.raises(ValueError):
        run(item, excerpt_start=start)
    assert not item["out"].exists()


def test_receipt_excerpt_must_exist_unchanged_in_selected_input(tmp_path):
    item = files(tmp_path, text=EXCERPT.lower())
    with pytest.raises(ValueError):
        run(item)
    assert not item["out"].exists()


def test_selected_input_id_must_be_present_and_unambiguous(tmp_path):
    item = files(tmp_path)
    with pytest.raises(ValueError):
        write_source_review_packet(item["source"], "missing", item["lookup"], item["out"])
    write_csv(item["source"], [("2", "100", EXCERPT), ("2", "200", EXCERPT)])
    with pytest.raises(ValueError):
        run(item)
    assert not item["out"].exists()


@pytest.mark.parametrize(("key", "value"), [
    ("tool", "different_tool"),
    ("policy_version", "unrecognized-policy"),
    ("excerpt_sha256", "0" * 64),
    ("identity_verified", True),
    ("status", "disabled"),
    ("status", "unavailable"),
])
def test_invalid_or_approved_lookup_receipt_is_rejected_before_output(tmp_path, key, value):
    item = files(tmp_path)
    item["receipt"][key] = value
    put_json(item["lookup"], item["receipt"])
    with pytest.raises(ValueError):
        run(item)
    assert not item["out"].exists()


def test_older_receipt_without_identity_flag_stays_pending(tmp_path):
    item = files(tmp_path)
    del item["receipt"]["identity_verified"]
    put_json(item["lookup"], item["receipt"])
    run(item)
    assert "pending" in set(value for value in values(read_json(item["out"] / "review.json")) if isinstance(value, str))


def wrapped_receipt(item):
    start = item["original"].index(EXCERPT)
    return {
        "legacy_input": {
            "source_file_sha256": sha(item["source"].read_bytes()),
            "legacy_input_id": "2", "legacy_article_id": "100",
            "original_text_sha256": sha(item["original"].encode("utf-8")),
            "excerpt": EXCERPT, "excerpt_start": start, "excerpt_end": start + len(EXCERPT),
        },
        "result": item["receipt"], "source_data_unchanged": True,
    }


def test_existing_lookup_wrapper_retains_original_bytes_and_binds_legacy_descriptor(tmp_path):
    item = files(tmp_path)
    put_json(item["lookup"], wrapped_receipt(item))
    original_receipt = item["lookup"].read_bytes()
    run(item)
    assert (item["out"] / "lookup_receipt.json").read_bytes() == original_receipt
    assert candidate(item)["comparison_state"] == "not_captured"


@pytest.mark.parametrize(("key", "value"), [
    ("source_file_sha256", "0" * 64),
    ("legacy_input_id", "other-input"),
    ("legacy_article_id", "another-article"),
    ("original_text_sha256", "0" * 64),
    ("excerpt", EXCERPT.lower()),
    ("excerpt_start", 0),
    ("excerpt_end", 1),
])
def test_wrapper_descriptor_cannot_rebind_search_to_different_legacy_source(tmp_path, key, value):
    item = files(tmp_path)
    wrapper = wrapped_receipt(item)
    wrapper["legacy_input"][key] = value
    put_json(item["lookup"], wrapper)
    with pytest.raises(ValueError):
        run(item)
    assert not item["out"].exists()


def test_unresolved_lookup_with_no_candidates_is_a_valid_unresolved_packet(tmp_path):
    item = files(tmp_path, candidates=[])
    item["receipt"]["status"] = "unresolved"
    put_json(item["lookup"], item["receipt"])
    run(item)
    assert read_json(item["out"] / "packet.json")["candidates"] == []


@pytest.mark.parametrize("url", [
    "file:///C:/private/article.txt", "javascript:alert(1)", "http://127.0.0.1/secret",
    "http://10.0.0.1/secret", "http://localhost/secret", "https://user:password@example.org/article",
    "https://example.org/with space",
])
def test_unsafe_candidate_url_cannot_enter_review_html(tmp_path, url):
    item = files(tmp_path, candidates=[{"url": url}])
    with pytest.raises(ValueError):
        run(item)
    assert not item["out"].exists()


def test_candidate_count_is_bounded_by_existing_discovery_contract(tmp_path):
    item = files(tmp_path, candidates=[{"url": f"https://example.org/{i}"} for i in range(6)])
    with pytest.raises(ValueError):
        run(item)
    assert not item["out"].exists()


@pytest.mark.parametrize("mutation", ["duplicate_key", "nonfinite", "float_overflow"])
def test_ambiguous_receipt_json_is_rejected(tmp_path, mutation):
    item = files(tmp_path)
    raw = item["lookup"].read_text(encoding="utf-8")
    if mutation == "duplicate_key":
        raw = raw.replace('"status": "ok"', '"status": "ok", "status": "unresolved"')
    elif mutation == "nonfinite":
        raw = raw.rstrip().removesuffix("}") + ', "cost_usd": NaN}\n'
    else:
        raw = raw.rstrip().removesuffix("}") + ', "cost_usd": 1e999}\n'
    item["lookup"].write_bytes(raw.encode("utf-8"))
    with pytest.raises(ValueError):
        run(item)
    assert not item["out"].exists()


def test_capture_must_belong_to_a_recorded_candidate_url(tmp_path):
    item = files(tmp_path)
    manifest = capture(item, candidate_url=SECOND_URL)
    with pytest.raises(ValueError):
        run(item, captures=manifest)
    assert not item["out"].exists()


def test_duplicate_capture_for_same_candidate_is_rejected(tmp_path):
    item = files(tmp_path)
    manifest = capture(item)
    item["capture_value"]["captures"].append(dict(item["capture_value"]["captures"][0]))
    put_json(manifest, item["capture_value"])
    with pytest.raises(ValueError):
        run(item, captures=manifest)
    assert not item["out"].exists()


@pytest.mark.parametrize("path", ["../input.csv", "/outside.txt", "C:\\private\\capture.txt"])
def test_capture_text_path_cannot_escape_manifest_directory(tmp_path, path):
    item = files(tmp_path)
    manifest = capture(item)
    item["capture_value"]["captures"][0]["text_file"] = path
    put_json(manifest, item["capture_value"])
    with pytest.raises(ValueError):
        run(item, captures=manifest)
    assert not item["out"].exists()


@pytest.mark.parametrize(("key", "value"), [
    ("completeness", "verified"),
    ("captured_at", "2026-09-30T13:00:00"),
    ("capture_method", ""),
    ("final_url", "http://127.0.0.1/private"),
])
def test_invalid_capture_metadata_is_not_promoted_to_source_evidence(tmp_path, key, value):
    item = files(tmp_path)
    manifest = capture(item)
    item["capture_value"]["captures"][0][key] = value
    put_json(manifest, item["capture_value"])
    with pytest.raises(ValueError):
        run(item, captures=manifest)
    assert not item["out"].exists()


def test_capture_requires_strict_utf8_instead_of_silent_character_replacement(tmp_path):
    item = files(tmp_path)
    manifest = capture(item)
    item["capture_text"].write_bytes(b"Original capture \xff changed bytes.")
    with pytest.raises(ValueError):
        run(item, captures=manifest)
    assert not item["out"].exists()


def test_html_escapes_candidate_and_captured_titles(tmp_path):
    payload = '<script>alert("untrusted title")</script>'
    item = files(tmp_path, candidates=[{"url": URL, "title": payload}])
    run(item, captures=capture(item, title=payload))
    html = (item["out"] / "review.html").read_bytes().decode("utf-8")
    assert payload not in html
    assert "&lt;script&gt;" in html


def test_prior_packet_and_its_review_are_never_overwritten(tmp_path):
    item = files(tmp_path)
    run(item)
    before = {path.relative_to(item["out"]).as_posix(): path.read_bytes()
              for path in item["out"].rglob("*") if path.is_file()}
    with pytest.raises(ValueError):
        run(item)
    assert before == {path.relative_to(item["out"]).as_posix(): path.read_bytes()
                      for path in item["out"].rglob("*") if path.is_file()}


def test_packet_can_be_built_with_no_network_client_or_database(monkeypatch, tmp_path):
    import socket

    import psycopg

    from observatory.claims_source_search import ClaimsSourceSearch

    def forbidden(*args, **kwargs):
        raise AssertionError("Source review packet must remain offline")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(ClaimsSourceSearch, "call", forbidden)
    item = files(tmp_path)
    run(item, captures=capture(item))
    assert candidate(item)["comparison_state"] == "unique_excerpt"


def test_selected_csv_text_belongs_to_frozen_bytes_even_if_later_reopen_sees_another_version(monkeypatch, tmp_path):
    item = files(tmp_path)
    original_bytes = item["source"].read_bytes()
    another_version = io.StringIO(newline="")
    writer = csv.writer(another_version, lineterminator="\r\n")
    writer.writerow(["id", "article_id", "text"])
    writer.writerow(["2", "100", "Different file revision. " + EXCERPT])
    open_file = Path.open

    def reopen(path, mode="r", *args, **kwargs):
        # A writer can change and restore the CSV between the two byte reads.
        # Simulate only the intervening text open, without editing project data.
        if path == item["source"] and "r" in mode and "b" not in mode:
            return io.StringIO(another_version.getvalue(), newline="")
        return open_file(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reopen)
    run(item)
    source_input = read_json(item["out"] / "source_input.json")
    assert source_input["text"] == item["original"]
    assert source_input["source_file_sha256"] == sha(original_bytes)


def test_offline_cli_bypasses_settings_and_database_construction(monkeypatch, capsys, tmp_path):
    import sys

    from observatory import cli

    item = files(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline source review CLI must not initialize settings or a database")

    monkeypatch.setattr(cli.Settings, "from_env", forbidden)
    monkeypatch.setattr(cli, "Database", forbidden)
    monkeypatch.setenv("OBS_MONTHLY_BUDGET_USD", "invalid-unneeded-setting")
    monkeypatch.setattr(sys, "argv", [
        "observatory", "claims-source-review-packet", "--input-csv", str(item["source"]),
        "--input-id", "2", "--lookup", str(item["lookup"]), "--out", str(item["out"]),
    ])
    cli.main()
    summary = json.loads(capsys.readouterr().out)
    assert summary["candidate_count"] == 1
    assert summary["network_calls"] == summary["model_calls"] == summary["database_writes"] == 0
    assert (item["out"] / "packet_manifest.json").is_file()
