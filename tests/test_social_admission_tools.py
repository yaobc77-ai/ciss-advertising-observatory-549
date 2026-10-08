"""Paused source text cannot leak to the model through adjacent read tools."""

import json
from types import SimpleNamespace

import pytest
from test_research_tools import make_catalog, social_catalog

from observatory.config import Settings
from observatory.db import Database
from observatory.indexing import expected_chunks
from observatory.models import Evidence, Filters
from observatory.prompts import ANSWER_SYSTEM, RESEARCH_SYSTEM
from observatory.quality import body_hash
from observatory.service import Service
from observatory.social_archive import SOCIAL_LABELS


def admission(**updates):
    return {"scheme": "collected-company-posts-unique-url-v1", "scope": "collected_company_posts",
            "count_unit": "platform_canonical_original_post_url", "paid_ad_status": "unknown",
            "member_count": 2, "conflicting_fields": ["body", "sponsor", "account"],
            "retrieval_status": "paused_body_disagreement", "private_path": "C:/NEVER-PUBLIC",
            "source_variants": [{"body": "VARIANT-TEXT-NEVER-PUBLIC"}], **updates}


@pytest.mark.parametrize("tool", ["get_record", "get_record_sources"])
@pytest.mark.parametrize("retrievable", [False, None, 0])
@pytest.mark.parametrize("retrieval_status", ["paused_body_disagreement", "paused_sentence_source_validation"])
def test_paused_source_tools_return_metadata_without_body_variants_or_generated_explanations(tool, retrievable, retrieval_status):
    catalog, _, row = social_catalog()
    row["retrievable"] = retrievable
    row["social_admission"] = admission(retrieval_status=retrieval_status)
    row["body"] = "PAUSED-SOURCE-TEXT-NEVER-PUBLIC"
    row["body_hash"] = body_hash(row["body"])
    values = {key: key == "green_binary" for key in SOCIAL_LABELS}
    row["annotations"] = [{"version": "claims-social-export-v1", "status": "historical_automatic_unverified",
                           "basis": "supplied_source_post_id_and_exact_body", "source_sha256": "a" * 64,
                           "source_row": 1, "body_sha256": row["body_hash"], "values": values,
                           "labels": [key for key in SOCIAL_LABELS if values[key]],
                           "taxonomy_mapping": "source_keys_preserved_no_native_label_mapping",
                           "explanations": {"green_explanation": row["body"]}}]
    result = catalog.call(tool, {"record_id": row["record_id"]})
    assert result["status"] == "ok" and result["text_status"] == "paused"
    assert result["review_required"] is True
    assert result["record"]["record_id"] == row["record_id"]
    assert result["record"]["social_admission"]["member_count"] == 2
    assert result["record"]["social_admission"]["paid_ad_status"] == "unknown"
    assert result["record"]["social_admission"]["retrieval_status"] == retrieval_status
    for hidden in (row["body"], "VARIANT-TEXT-NEVER-PUBLIC", "NEVER-PUBLIC", "private_path", '"source_variants":'):
        assert hidden not in json.dumps(result)
    assert "body" not in result
    if tool == "get_record_sources":
        assert result["annotations"] == [] and result["social_historical_annotation"]["explanations"] == []
        assert any(item["code"] == "social_source_text_paused" for item in result["warnings"])


def test_paused_post_still_counts_once_and_has_no_index_chunks():
    catalog, _, row = social_catalog()
    row.update(retrievable=False, social_admission=admission(), countable=True)
    result = catalog.call("record_statistics", {})
    assert result["status"] == "ok"
    assert result["collections"] == [{"dataset": "social", "total": 1, "retrievable": 0, "unknown_dates": 0}]
    assert expected_chunks({"payload": {"retrievable": False}, "body": row["body"]}, {}) == []
    answer = Service._tool_statistics_answer(result, base_filters=Filters(dataset="social"))
    assert "company social posts" in answer.answer and "social ad records" not in answer.answer


def test_permitted_social_excerpt_stays_exact_and_admission_projection_is_bounded():
    catalog, _, row = social_catalog()
    row["social_admission"] = admission(conflicting_fields=[], retrieval_status="enabled_source_post_text_only")
    result = catalog.call("get_record", {"record_id": row["record_id"], "body_start": 1, "body_limit": 8})
    assert result["body"]["text"] == row["body"][1:9]
    assert result["source_refs"][0]["start"] == 1 and result["source_refs"][0]["end"] == 9
    assert set(result["record"]["social_admission"]) == {
        "scope", "count_unit", "paid_ad_status", "member_count", "conflicting_fields", "retrieval_status", "source_variants_access"}


def test_native_nonretrievable_detail_behavior_remains_unchanged():
    catalog, db = make_catalog()
    row = db.rows[0]
    row["retrievable"] = False
    result = catalog.call("get_record", {"record_id": row["record_id"]})
    assert result["status"] == "ok" and result["body"]["text"] == row["body"]
    assert "text_status" not in result and "social_admission" not in result["record"]


@pytest.mark.parametrize("damage", [{"member_count": True}, {"member_count": -1}, {"scope": "verified_paid_ads"},
                                   {"conflicting_fields": ["C:/NEVER-PUBLIC"]}, {"retrieval_status": "unknown-private"}])
def test_malformed_admission_never_becomes_trusted_tool_metadata(damage):
    catalog, _, row = social_catalog()
    row["social_admission"] = admission(**damage)
    assert "social_admission" not in catalog.call("get_record", {"record_id": row["record_id"]})["record"]


@pytest.mark.parametrize("dataset", ["social", "all", "native"])
def test_verified_paid_condition_is_not_substituted_by_unreviewed_collection_totals(dataset):
    catalog, db, _ = social_catalog(Filters(dataset=dataset))
    db.dashboard = lambda *args, **kwargs: pytest.fail("No collection total may substitute for verified paid status")
    result = catalog.call("record_statistics", {"paid_ad_status": "verified_paid"})
    assert result["status"] == "clarify" and result["reason"] == "paid_ad_evidence_missing"
    assert result["available_scope"] == "collected_company_posts"
    assert "collections" not in result and "records" not in result and "groups" not in result
    assert "No paid-ad count is available" in result["message"]


def test_both_model_prompts_preserve_paid_identity_requirement():
    # The tool-using planner needs the exact filter; the answer writer (no tools)
    # keeps the identity limit itself.
    assert "paid_ad_status='verified_paid'" in RESEARCH_SYSTEM
    assert "do not substitute collection" in RESEARCH_SYSTEM
    assert "paid-ad evidence is missing" in RESEARCH_SYSTEM
    assert "A collected company post does not establish paid advertising." in ANSWER_SYSTEM


def test_current_source_sql_reads_only_named_admission_metadata(monkeypatch):
    from test_versioned_record import Connection

    db, connection = Database(""), Connection(None)
    monkeypatch.setattr(db, "connect", lambda: connection)
    db.versioned_record(Filters(dataset="social"), "social-post:abc")
    sql, _ = connection.reads[0]
    assert "AS social_admission" in sql and "{raw,social_admission,member_count}" in sql
    assert "AS collection_scope" in sql and "AS count_unit" in sql and "AS paid_ad_status" in sql
    assert "THEN 'source_record' END AS count_unit" in sql
    assert "source_variants" not in sql and "v.payload->'raw'" not in sql


@pytest.mark.parametrize("route", ["record", "sources"])
def test_service_displays_paused_metadata_without_claiming_a_content_answer(route):
    from observatory.research_agent import ResearchRun

    data = {"status": "ok", "text_status": "paused", "review_required": True,
            "filters": Filters(dataset="social").model_dump(mode="json"),
            "record": {"record_id": "social-post:abc", "dataset": "social", "retrievable": False},
            "source_refs": [], "message": "Source text is paused; inspect the preserved observations."}
    text = "The Fable cooperative reports a workshop schedule."
    evidence = Evidence(evidence_id="fable-passage", record_id="fable-record", version_id="fable-v1",
                        dataset="social", title="Fable workshop", text=text, start=0, end=len(text))
    db = SimpleNamespace(
        searches=[],
        health=lambda: {"status": "ok", "data_version": "fable-v1"},
        save_answer=lambda *args: None,
    )
    def search(*args, **kwargs):
        db.searches.append((args, kwargs))
        return [evidence]

    db.search = search
    service = Service(Settings(research_agent_enabled=True, web_search_enabled=False), db=db, rag=object(),
                      research_agent=SimpleNamespace(run=lambda *args, **kwargs: ResearchRun(route=route, result=data)))
    result = service.answer("Read this post", Filters(dataset="social"), "test")
    assert result.status == "insufficient_evidence" and result.answer_mode == "tools"
    assert result.structured_result["review_required"] is True
    assert "body" not in result.structured_result
    assert "paused" in result.answer
    assert db.searches == [] and result.cost_usd == 0
