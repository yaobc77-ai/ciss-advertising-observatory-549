"""Fresh fictional source fixtures: payload organization, never model accuracy."""

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from observatory import rag
from observatory.config import Settings
from observatory.db import digest
from observatory.models import Evidence, MediaAnswerEvidence


class MemoryConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args):
        return None


class MemoryDB:
    def __init__(self, valid=True):
        self.valid = valid

    def validate_evidence(self, _evidence):
        return self.valid

    def connect(self):
        return MemoryConnection()


class NoSpendBudget:
    def __init__(self):
        self.audit = None
        self.reservations = []

    def reserve(self, *_args):
        self.reservations.append("synthetic-reservation")
        return "synthetic-reservation"

    def settle(self, _reservation, _cost, usage):
        self.audit = usage

    def cancel_unsent(self, *_args):
        pass

    def uncertain(self, *_args):
        pass


class CaptureResponses:
    def __init__(self):
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            status="completed", model="synthetic-no-network",
            output_parsed=kwargs["text_format"](
                status="insufficient_evidence", claims=[], summary=[], sections=[],
            ),
            usage=SimpleNamespace(
                input_tokens=0, output_tokens=0,
                input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0),
                model_dump=lambda: {"input_tokens": 0, "output_tokens": 0},
            ),
        )


def fragment(eid, text, *, record="fictional-library", version=None, start=0,
             dataset="native", **extra):
    return Evidence(
        evidence_id=eid, record_id=record, version_id=version or digest(record),
        dataset=dataset, title="Fictional Ilex Library planning note",
        publisher="Invented district archive", sponsor="",
        url="https://example.invalid/library-note", archive_url="",
        published_at=date(2025, 9, 3), text=text, start=start, end=start + len(text),
        **extra,
    )


def make_adapter(*, valid=True):
    responses = CaptureResponses()
    adapter = rag.Rag(MemoryDB(valid), Settings(), client=SimpleNamespace(responses=responses))
    adapter.budget = NoSpendBudget()
    return adapter, responses


def capture(evidence, media=(), *, valid=True):
    adapter, responses = make_adapter(valid=valid)
    answer = adapter.generate(
        "Summarize the supplied fictional planning material.", evidence,
        "synthetic-development", media_evidence=media, validate_media=lambda _items: True,
    )
    assert answer.cost_usd == float(Decimal(0))
    responses.audit = adapter.budget.audit
    return json.loads(responses.calls[0]["input"][1]["content"]), responses


def library_fragments():
    first = "The fictional Ilex Library proposed a weekend reading room. "
    second = "Opening depends on volunteer availability; no opening date is confirmed."
    version = digest(first + second)
    return [fragment("fragment-a", first, version=version),
            fragment("fragment-b", second, version=version, start=len(first))]


def test_two_chunks_identify_one_record_and_source():
    payload, _ = capture(library_fragments())
    inventory = payload["evidence_inventory"]
    assert inventory["record_count"] == 1
    assert inventory["record_version_count"] == 1
    assert inventory["source_count"] == 1
    assert inventory["text_chunk_count"] == 2
    assert inventory["quoted_text_chunk_count"] == 2
    assert inventory["sources"][0]["evidence_ids"] == ["fragment-a", "fragment-b"]
    assert {item["source_ref"] for item in payload["evidence"]} == {"S1"}


def test_original_identity_fields_and_condition_are_supplied():
    evidence = library_fragments()
    payload, _ = capture(evidence)
    for original, supplied in zip(evidence, payload["evidence"]):
        assert supplied["record_id"] == original.record_id
        assert supplied["version_id"] == original.version_id
        assert supplied["start"] == original.start
        assert supplied["end"] == original.end
        assert supplied["url"] == original.url
        assert supplied["archive_url"] == original.archive_url
        assert supplied["published_at"] == "2025-09-03"
    assert list(payload["evidence"][1]["quote_catalog"].values()) == [evidence[1].text]
    assert payload["retrieval_scope"]["coverage"] == "retrieved_subset"
    assert payload["retrieval_scope"]["complete_matching_list"] is False
    assert payload["evidence_inventory"]["scope"] == "supplied_retrieved_evidence_only"
    assert payload["evidence_inventory"]["population_statistics"] is False


def test_shared_title_does_not_merge_records():
    payload, _ = capture([fragment("a", "An invented telescope needs repairs.", record="fictional-observatory"),
                          fragment("b", "An invented library needs volunteers.", record="fictional-library")])
    assert payload["evidence_inventory"]["record_count"] == 2
    assert payload["evidence_inventory"]["source_count"] == 2


def test_same_record_across_versions_stays_one_record_two_sources():
    payload, _ = capture([fragment("a", "The room is proposed.", version=digest("proposal")),
                          fragment("b", "The room remains unapproved.", version=digest("revision"))])
    inventory = payload["evidence_inventory"]
    assert inventory["record_count"] == 1
    assert inventory["record_version_count"] == 2
    assert inventory["source_count"] == 2
    assert inventory["records"][0]["version_ids"] == [digest("proposal"), digest("revision")]


def test_same_record_id_across_collections_is_two_records():
    payload, _ = capture([fragment("a", "A fictional observatory issued a draft.", dataset="native"),
                          fragment("b", "A fictional observatory posted a reminder.", dataset="social")])
    assert payload["evidence_inventory"]["record_count"] == 2
    assert {item["dataset"] for item in payload["evidence_inventory"]["records"]} == {"native", "social"}


def test_distinct_observations_do_not_become_distinct_records():
    sources = [fragment(
        eid, text, dataset="social", source_observation_id=observation,
        source_version_id=digest(observation), source_body_hash=digest(text),
        source_observation_count=2, source_conflicts=["body"],
    ) for eid, observation, text in [
        ("a", "capture-a", "The invented garden proposed a workday."),
        ("b", "capture-b", "The invented garden proposed a workday if it stays dry."),
    ]]
    payload, _ = capture(sources)
    inventory = payload["evidence_inventory"]
    assert inventory["record_count"] == 1
    assert inventory["record_version_count"] == 1
    assert inventory["source_count"] == 2
    assert [source["source_observation_id"] for source in inventory["sources"]] == ["capture-a", "capture-b"]
    assert all(source["source_kind"] == "article_text" for source in inventory["sources"])


def test_redundant_chunk_remains_accounted_for_without_duplicating_quotes():
    first = fragment("a", "The invented workshop plans a repair lesson.")
    second = first.model_copy(update={"evidence_id": "b"})
    payload, _ = capture([first, second])
    inventory = payload["evidence_inventory"]
    assert inventory["text_chunk_count"] == 2
    assert inventory["quoted_text_chunk_count"] == 1
    assert inventory["passage_count"] == 1
    assert inventory["sources"][0]["evidence_ids"] == ["a", "b"]
    assert inventory["sources"][0]["included_evidence_ids"] == ["a"]
    assert [item["id"] for item in payload["evidence"]] == ["a"]


def test_media_and_body_share_record_but_remain_separate_sources():
    body = fragment("body", "The fictional room is still proposed.")
    media = MediaAnswerEvidence(
        evidence_id="image-text", asset_id="fictional-image", record_id=body.record_id,
        version_id=body.version_id, dataset="native", title=body.title,
        source_url="https://example.invalid/notice", media_type="image",
        origin="human_description", locator={
            "kind": "image_region", "page_number": 1, "region": [0.0, 0.0, 1.0, 1.0],
            "screenshot_sha256": digest("fictional-image"),
        },
        evidence_text="The fictional notice says volunteers are needed.", quality_label="synthetic",
        asset_sha256=digest("fictional-image"), text_artifact_id="description-a",
        artifact_sha256=digest("fictional-description"), derived_start=0, derived_end=48,
    )
    payload, _ = capture([body], [media])
    inventory = payload["evidence_inventory"]
    assert inventory["record_count"] == 1
    assert inventory["source_count"] == 2
    assert inventory["text_chunk_count"] == 1
    assert inventory["media_evidence_count"] == 1
    supplied = payload["media_evidence"][0]
    assert supplied["source_ref"] == "S2"
    assert supplied["record_id"] == body.record_id
    assert supplied["version_id"] == body.version_id
    assert supplied["source_url"] == media.source_url
    assert supplied["quote_from_original_body"] is False


def test_invalid_offsets_still_fail_before_dispatch():
    evidence = fragment("a", "An invented manual describes a repair.")
    evidence = evidence.model_copy(update={"end": evidence.end + 1})
    adapter, responses = make_adapter()
    with pytest.raises(ValueError, match="Evidence offsets failed source validation"):
        adapter.generate("Summarize this fictional note.", [evidence], "synthetic-development")
    assert responses.calls == []
    assert adapter.budget.reservations == []


def test_original_version_failure_still_fails_before_dispatch():
    adapter, responses = make_adapter(valid=False)
    with pytest.raises(ValueError, match="Evidence failed original-version validation"):
        adapter.generate("Summarize this fictional note.", library_fragments(), "synthetic-development")
    assert responses.calls == []
    assert adapter.budget.reservations == []


def test_audit_binds_inventory_to_the_exact_payload():
    payload, responses = capture(library_fragments())
    assert responses.audit["observatory_request"]["evidence_inventory"] == payload["evidence_inventory"]
    assert responses.audit["observatory_request"]["payload_sha256"] == digest(
        responses.calls[0]["input"][1]["content"],
    )


def test_unlocated_text_is_never_assigned_an_inferred_record():
    sources = [SimpleNamespace(evidence_id=eid, text="An invented list is tentative.")
               for eid in ("a", "b")]
    catalog = rag.answer_quote_catalog(sources)
    inventory = rag.evidence_inventory(sources, [], catalog)
    assert inventory["record_count"] == 0
    assert inventory["record_version_count"] == 0
    assert inventory["unlocated_evidence_count"] == 2
    assert inventory["source_count"] == 2
    assert all(source["record_ref"] is None for source in inventory["sources"])
    assert inventory["passage_count"] == 2


def test_missing_source_date_is_explicitly_null():
    item = fragment("a", "The fictional catalogue is undated.").model_copy(update={"published_at": None})
    payload, _ = capture([item])
    assert payload["evidence"][0]["published_at"] is None


def test_real_sdk_with_memory_transport_preserves_joint_conditional_answer():
    """Predetermined SDK response proves wiring, not model inference or accuracy."""
    calls = []
    selected = {
        "status": "answered",
        "claims": [{
            "passage_id": "Q1", "support_passage_ids": ["Q2"],
            "text": ("The fictional Ilex Library proposed a weekend reading room. "
                     "Opening depends on volunteer availability; no opening date is confirmed."),
        }],
        "summary": [{
            "text": ("The fictional Ilex Library proposes a reading room; opening depends on "
                     "volunteers, and no opening date is confirmed."),
            "citation_indices": [1],
        }],
        "sections": [{"title": "Fictional library plan", "citation_indices": [1]}],
    }

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "resp_fictional", "object": "response", "created_at": 0,
            "status": "completed", "model": "gpt-5.6-luna",
            "output": [{
                "id": "msg_fictional", "type": "message", "role": "assistant",
                "status": "completed", "content": [{
                    "type": "output_text", "text": json.dumps(selected), "annotations": [],
                }],
            }],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        })

    with rag.OpenAI(api_key="synthetic-not-a-key", base_url="https://example.invalid/v1",
                    http_client=httpx.Client(transport=httpx.MockTransport(handle)),
                    max_retries=0) as client:
        adapter = rag.Rag(MemoryDB(), Settings(), client=client)
        adapter.budget = NoSpendBudget()
        answer = adapter.generate(
            "Summarize the supplied fictional planning note.", library_fragments(), "synthetic-development",
        )
    assert len(calls) == 1
    payload = json.loads(calls[0]["input"][1]["content"])
    assert payload["evidence_inventory"]["record_count"] == 1
    assert payload["evidence_inventory"]["text_chunk_count"] == 2
    assert answer.status == "answered"
    assert answer.cited_claims[0].text == selected["claims"][0]["text"]
    assert [quote.evidence_id for quote in answer.citations] == ["fragment-a", "fragment-b"]
