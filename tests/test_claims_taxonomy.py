from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from observatory.claims_taxonomy import (
    TAXONOMY_FILES,
    ClaimsTaxonomyError,
    load_taxonomy_bundle,
)


def _entry(text="A preserved definition"):
    return {
        "current_text": text,
        "history": [{
            "timestamp": "2026-04-22T17:13:08.462500",
            "action": "created",
            "text": text,
            "source_article_id": "17",
            "article_url": "unknown",
            "source_snippet": "An upstream source snippet.",
            "metadata": {"article_id": 9, "prior_ids": [1, 2]},
        }],
    }


def _bundle(root, *, subclaims=None, superclaims=None, mapping=None, history=None):
    root.mkdir(parents=True, exist_ok=True)
    values = {
        TAXONOMY_FILES[0]: subclaims if subclaims is not None else {"NC_1": "A preserved definition"},
        TAXONOMY_FILES[1]: superclaims if superclaims is not None else {"SC_1": "A superclaim"},
        TAXONOMY_FILES[2]: mapping if mapping is not None else {"NC_1": "SC_1"},
        TAXONOMY_FILES[3]: history if history is not None else {
            "claims": {"NC_1": _entry()},
            "last_updated": "2026-04-22T18:30:41.779887",
        },
    }
    for filename, value in values.items():
        (root / filename).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return root


def test_reader_preserves_definitions_history_and_unapproved_status(tmp_path):
    root = _bundle(tmp_path / "bundle")
    bundle = load_taxonomy_bundle(root)
    assert bundle.authority_status == "candidate_not_approved"
    assert bundle.subclaims == {"NC_1": "A preserved definition"}
    assert bundle.superclaims == {"SC_1": "A superclaim"}
    assert bundle.claim_superclaim_map == {"NC_1": "SC_1"}
    assert bundle.history["NC_1"]["history"][0]["article_url"] == "unknown"
    assert bundle.history["NC_1"]["history"][0]["metadata"]["prior_ids"] == (1, 2)
    manifest = bundle.to_manifest()
    assert manifest["structurally_valid"]
    assert manifest["mapping_complete"]
    assert manifest["history_consistent"]
    assert manifest["issues"] == []
    assert manifest["counts"]["history_events"] == 1
    assert "unknown" not in json.dumps(manifest)
    with pytest.raises(TypeError):
        bundle.subclaims["NC_1"] = "Changed definition"
    with pytest.raises(TypeError):
        bundle.history["NC_1"]["history"][0]["metadata"]["article_id"] = 2


def test_fingerprints_bind_exact_file_bytes_and_not_directory(tmp_path):
    left = _bundle(tmp_path / "left")
    right = _bundle(tmp_path / "right")
    before = {name: (left / name).read_bytes() for name in TAXONOMY_FILES}
    left_bundle = load_taxonomy_bundle(left)
    right_bundle = load_taxonomy_bundle(right)
    assert left_bundle.bundle_fingerprint == right_bundle.bundle_fingerprint
    assert left_bundle.file_hashes == {
        name: hashlib.sha256(data).hexdigest() for name, data in before.items()
    }
    assert before == {name: (left / name).read_bytes() for name in TAXONOMY_FILES}
    path = right / TAXONOMY_FILES[0]
    path.write_bytes(path.read_bytes() + b"\n")
    changed = load_taxonomy_bundle(right)
    assert changed.subclaims == left_bundle.subclaims
    assert changed.bundle_fingerprint != left_bundle.bundle_fingerprint
    assert changed.file_hashes[TAXONOMY_FILES[0]] != left_bundle.file_hashes[TAXONOMY_FILES[0]]


def test_dangling_mappings_are_quarantined_and_unmapped_claims_reported(tmp_path):
    root = _bundle(
        tmp_path / "bundle",
        subclaims={"NC_1": "A preserved definition", "NC_2": "Second", "NC_3": "Third"},
        mapping={"NC_1": "SC_1", "NC_2": "SC_9", "NC_9": "SC_1"},
    )
    bundle = load_taxonomy_bundle(root)
    assert bundle.raw_claim_superclaim_map == {"NC_1": "SC_1", "NC_2": "SC_9", "NC_9": "SC_1"}
    assert bundle.claim_superclaim_map == {"NC_1": "SC_1"}
    issues = bundle.to_manifest()["issues"]
    assert {"code": "dangling_mapping_superclaim", "subclaim_id": "NC_2", "superclaim_id": "SC_9"} in issues
    assert {"code": "dangling_mapping_subclaim", "subclaim_id": "NC_9", "superclaim_id": "SC_1"} in issues
    assert {"code": "unmapped_subclaim", "subclaim_id": "NC_2"} in issues
    assert {"code": "unmapped_subclaim", "subclaim_id": "NC_3"} in issues
    assert not bundle.to_manifest()["mapping_complete"]
    assert not bundle.to_manifest()["mapping_references_valid"]


def test_history_references_and_text_mismatch_are_explicit(tmp_path):
    root = _bundle(tmp_path / "bundle", history={
        "claims": {"NC_1": _entry("Different wording"), "NC_7": _entry()},
        "last_updated": "2026-04-22T18:30:41",
    })
    manifest = load_taxonomy_bundle(root).to_manifest()
    assert not manifest["history_consistent"]
    assert {"code": "history_definition_mismatch", "subclaim_id": "NC_1"} in manifest["issues"]
    assert {"code": "dangling_history_subclaim", "subclaim_id": "NC_7"} in manifest["issues"]


@pytest.mark.parametrize("claim_id", ["NC_01", "NC_0", "NC_-1", "SC_1", "nc_1", "NC_1 "])
def test_subclaim_ids_are_not_silently_repaired(tmp_path, claim_id):
    root = _bundle(tmp_path / "bundle", subclaims={claim_id: "Definition"})
    with pytest.raises(ClaimsTaxonomyError, match="invalid identifier"):
        load_taxonomy_bundle(root)


@pytest.mark.parametrize("definition", [None, True, 1, {}, [], "", " \n\t"])
def test_nontext_or_empty_definitions_are_rejected(tmp_path, definition):
    root = _bundle(tmp_path / "bundle", subclaims={"NC_1": definition})
    with pytest.raises(ClaimsTaxonomyError, match="expected nonempty string"):
        load_taxonomy_bundle(root)


@pytest.mark.parametrize("mapping", [{"NC_1": ["SC_1"]}, {"NC_1": 1}, {"NC_1": "SC_01"}, {"SC_1": "SC_1"}])
def test_malformed_mapping_values_or_ids_fail(tmp_path, mapping):
    root = _bundle(tmp_path / "bundle", mapping=mapping)
    with pytest.raises(ClaimsTaxonomyError, match="invalid identifier"):
        load_taxonomy_bundle(root)


@pytest.mark.parametrize("content, message", [
    ('{"NC_1":"first","NC_1":"second"}', "Duplicate JSON key"),
    ('{"NC_1":NaN}', "Nonstandard JSON constant"),
    ('["NC_1"]', "top-level value must be an object"),
    ('{"NC_1":"\\ud800"}', "invalid Unicode string"),
    ('{', "greenwashing_codebook.json"),
])
def test_invalid_json_cannot_hide_behind_last_key_or_coercion(tmp_path, content, message):
    root = _bundle(tmp_path / "bundle")
    (root / TAXONOMY_FILES[0]).write_text(content, encoding="utf-8")
    with pytest.raises(ClaimsTaxonomyError, match=message):
        load_taxonomy_bundle(root)


@pytest.mark.parametrize("change, message", [
    (lambda h: h.update({"extra": True}), "expected claims and last_updated"),
    (lambda h: h.update({"last_updated": "not-a-date"}), "expected ISO timestamp"),
    (lambda h: h.update({"last_updated": "2026-04-22"}), "expected ISO timestamp"),
    (lambda h: h.update({"claims": []}), "expected object"),
    (lambda h: h["claims"]["NC_1"].update({"history": {}}), "expected list"),
    (lambda h: h["claims"]["NC_1"]["history"][0].pop("source_snippet"), "unsupported event structure"),
    (lambda h: h["claims"]["NC_1"]["history"][0].update({"metadata": []}), "expected object"),
    (lambda h: h["claims"]["NC_1"]["history"][0].update({"source_article_id": 17}), "expected string"),
    (lambda h: h["claims"]["NC_1"]["history"][0].update({"action": ""}), "expected nonempty string"),
])
def test_malformed_history_is_rejected(tmp_path, change, message):
    history = {"claims": {"NC_1": _entry()}, "last_updated": "2026-04-22T18:30:41"}
    change(history)
    root = _bundle(tmp_path / "bundle", history=history)
    with pytest.raises(ClaimsTaxonomyError, match=message):
        load_taxonomy_bundle(root)


def test_missing_required_file_is_a_domain_error(tmp_path):
    root = _bundle(tmp_path / "bundle")
    (root / TAXONOMY_FILES[2]).unlink()
    with pytest.raises(ClaimsTaxonomyError, match="could not be read"):
        load_taxonomy_bundle(root)


def test_supplied_paragraph_bundle_is_read_without_modification():
    root = Path("D:/549/ml-ciss-native-ads-main/CLAIMS_2.0_model/src/data")
    if not root.is_dir():
        pytest.skip("Private supplied CLAIMS bundle is not installed on this machine")
    before = {name: (root / name).read_bytes() for name in TAXONOMY_FILES}
    bundle = load_taxonomy_bundle(root)
    manifest = bundle.to_manifest()
    assert manifest["counts"] == {
        "subclaims": 502, "superclaims": 57, "raw_mapping_entries": 497,
        "valid_mapping_entries": 497, "history_claims": 502, "history_events": 756,
    }
    assert manifest["authority_status"] == "candidate_not_approved"
    assert manifest["mapping_references_valid"] and manifest["history_consistent"]
    assert len(manifest["issues"]) == 5
    assert {issue["code"] for issue in manifest["issues"]} == {"unmapped_subclaim"}
    assert bundle.subclaims["NC_1"] == "Natural gas reduces emissions"
    assert bundle.superclaims["SC_1"] == "Natural gas is a transition fuel"
    assert bundle.file_hashes[TAXONOMY_FILES[0]] == "36ce5904f2c45002b3e91608f2e68c578a7d27253594e8fce6243fed1144cac7"
    assert before == {name: (root / name).read_bytes() for name in TAXONOMY_FILES}
