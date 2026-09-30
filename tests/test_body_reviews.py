import hashlib
import json

import pytest
from test_ingest import BODY, base_row, native_fixture

from observatory.body_reviews import BODY_REVIEWS_PATH, apply_body_reviews
from observatory.ingest import NATIVE_CSV, load_native
from observatory.models import ImportBatch
from observatory.quality import body_hash


def review_fixture(root):
    navigation = "For more on the subject: OTHER ARTICLE "
    suffix = "This same article explains the costs and limitations. " * 12
    body = BODY + navigation + suffix
    native_fixture(root, [base_row(article=body)])
    original = load_native(root).records[0]
    manifest = {
        "schema_version": 1,
        "review_id": "fixture-v1",
        "reviewer_type": "ai",
        "source_path": NATIVE_CSV.as_posix(),
        "source_sha256": hashlib.sha256((root / NATIVE_CSV).read_bytes()).hexdigest(),
        "decisions": [
            {
                "record_id": original.record_id,
                "url": original.url,
                "source_row": 2,
                "body_sha256": body_hash(body),
                "retained_ranges": [
                    [0, len(BODY)],
                    [len(BODY) + len(navigation), len(body)],
                ],
                "reason": "Inline related link separates same-article text.",
                "evidence_refs": ["synthetic fixture"],
            }
        ],
    }
    path = root / BODY_REVIEWS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return original, manifest, suffix


def test_review_restores_suffix_without_editing_body_or_claiming_human_acceptance(
    tmp_path,
):
    original, manifest, suffix = review_fixture(tmp_path)
    batch = load_native(tmp_path, require_body_reviews=True)
    current = batch.records[0]
    assert original.retrieval_end == len(BODY)
    assert (
        current.body == original.body
        and current.raw["baseline"] == original.raw["baseline"]
    )
    assert current.retrieval_end is None and current.retrievable
    assert current.body[slice(*current.retrieval_ranges[1])] == suffix
    assert current.raw["body_review"]["reviewer_type"] == "ai"
    assert current.raw["body_review"]["previous_retrieval_end"] == len(BODY)
    assert not any(i.code == "body_related_navigation" for i in current.issues)
    assert (
        batch.model_dump()
        == load_native(tmp_path, require_body_reviews=True).model_dump()
    )


def test_body_repair_does_not_override_explicit_metadata_only_admission(tmp_path):
    original, manifest, _ = review_fixture(tmp_path)
    original.retrievable = False
    original.raw["admission_review"] = {"body_mode": "metadata_only"}
    batch = ImportBatch(
        records=[original],
        source_hashes={
            NATIVE_CSV.as_posix(): manifest["source_sha256"],
        },
    )
    apply_body_reviews(tmp_path, batch, required=True)
    assert batch.records[0].retrieval_ranges is not None
    assert batch.records[0].countable and not batch.records[0].retrievable


@pytest.mark.parametrize(
    "mutation", ["source", "body", "row", "overlap", "duplicate", "missing"]
)
def test_invalid_review_stops_before_snapshot_publication(tmp_path, mutation):
    _, manifest, _ = review_fixture(tmp_path)
    path = tmp_path / BODY_REVIEWS_PATH
    item = manifest["decisions"][0]
    if mutation == "source":
        manifest["source_sha256"] = "0" * 64
    elif mutation == "body":
        item["body_sha256"] = "0" * 64
    elif mutation == "row":
        item["source_row"] = 50
    elif mutation == "overlap":
        item["retained_ranges"] = [[0, 100], [50, 200]]
    elif mutation == "duplicate":
        manifest["decisions"].append(dict(item))
    path.write_text(json.dumps(manifest), encoding="utf-8")
    if mutation == "missing":
        path.unlink()
    with pytest.raises(ValueError):
        load_native(tmp_path, require_body_reviews=True)
