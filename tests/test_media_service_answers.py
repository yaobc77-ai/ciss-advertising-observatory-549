"""Fictional mounted material, replayed answers; no database or provider call."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_research_fallback import Agent, Web

from observatory.config import Settings
from observatory.media_evidence import (
    ImageLocation,
    MediaAsset,
    MediaBundle,
    TimedText,
    image_evidence,
    sha256_bytes,
    sha256_text,
    video_evidence,
)
from observatory.models import Answer, Evidence, Filters
from observatory.rag import GroundedClaim, ModelAnswer, validate_answer
from observatory.research_agent import ResearchRun
from observatory.service import Service


@pytest.fixture
def setup_media(tmp_path):
    assets, artifacts, units, rows = [], [], [], {}
    for sponsor, kind in (("fixture-a", "image"), ("fixture-b", "video")):
        body = "A fictional article discusses carbon capture."
        rid, version = sponsor + "-record", sha256_text(sponsor + "-version")
        raw, path = (sponsor + "-fictional-bytes").encode(), tmp_path / (sponsor + ".fixture")
        path.write_bytes(raw)
        asset = MediaAsset(asset_id=sponsor, record_id=rid, version_id=version,
                           dataset="native", media_type=kind, title="Fixture " + sponsor,
                           local_path=path.name, sha256=sha256_bytes(raw), identity_bound=True,
                           retrievable=True, duration_ms=10000 if kind == "video" else None)
        text = "The supplied fixture description discusses carbon capture."
        if kind == "image":
            artifact, found = image_evidence(asset, text, origin="human_description", producer="fixture",
                                            location=ImageLocation(screenshot_sha256=asset.sha256, page_number=2,
                                                                   region=[0.0, 0.0, 1.0, 1.0]))
        else:
            artifact, found = video_evidence(asset, [TimedText(text=text, start_ms=1000, end_ms=4000)],
                                            origin="publisher_caption", producer="fixture")
        assets.append(asset)
        artifacts.append(artifact)
        units.extend(found)
        rows[rid] = {"record_id": rid, "version_id": version, "dataset": "native",
                     "body": body, "body_hash": sha256_text(body), "title": asset.title,
                     "sponsor": sponsor, "url": "https://example.test/" + sponsor,
                     "retrievable": True, "active": True}
    bundle = MediaBundle(assets=assets, text_artifacts=artifacts, evidence=units)
    bundle_path = tmp_path / "bundle.json"
    raw = bundle.model_dump_json().encode()
    bundle_path.write_bytes(raw)
    settings = Settings(media_bundle_path=str(bundle_path), media_bundle_sha256=sha256_bytes(raw),
                        media_asset_root=str(tmp_path), web_search_enabled=True)

    class DB:
        version = "fixture-index-v1"
        source_reads, saved, searches = [], [], []
        body_hits = False

        def health(self):
            return {"status": "ok", "data_version": self.version, "record_counts": {"native": 2}}

        def public_rows(self, _filters):
            return []

        def versioned_record(self, filters, rid):
            self.source_reads.append((filters.model_copy(deep=True), rid))
            row = rows.get(rid)
            if row is None or filters.sponsors and row["sponsor"] not in filters.sponsors:
                return None
            return deepcopy(row)

        def search(self, query, filters, **kwargs):
            self.searches.append((query, filters.model_copy(deep=True), kwargs))
            return [Evidence(evidence_id="body-" + rid, record_id=rid, version_id=row["version_id"],
                             dataset="native", title=row["title"], text=row["body"], start=0, end=len(row["body"]))
                    for rid, row in rows.items() if self.body_hits
                    and (not filters.sponsors or row["sponsor"] in filters.sponsors)]

        def save_answer(self, *args):
            self.saved.append(args)

    db, web, generation, charges = DB(), Web(), [], []
    budget = SimpleNamespace(reserve=lambda *args: charges.append(args) or "fixture-reservation",
                             cancel_unsent=lambda _rid: None, reservation_cost=lambda _rid: 0.001)

    def generate(question, evidence, visitor, **kwargs):
        media = kwargs.get("media_evidence", [])
        generation.append((question, evidence, media, kwargs))
        assert kwargs["validate_media"](media)
        claims = [GroundedClaim(text="The supplied material describes carbon capture.",
                                evidence_id=item.evidence_id, quote=item.evidence_text) for item in media]
        return validate_answer(ModelAnswer(status="answered", claims=claims), evidence, media_evidence=media)

    rag = SimpleNamespace(generate=generate, embed=lambda queries, **kwargs: [[0.0] for _ in queries], budget=budget)
    service = Service(settings, db=db, rag=rag, web_research=web)
    return SimpleNamespace(service=service, db=db, web=web, generation=generation, charges=charges,
                           rows=rows, root=tmp_path, path=bundle_path, bundle=bundle)


def groups():
    return [{"label": name, "query": "carbon capture", "filters": Filters(sponsors=[name]).model_dump(mode="json")}
            for name in ("fixture-a", "fixture-b")]


def test_media_only_comparison_has_two_independent_targets_and_citations(setup_media):
    state = setup_media
    stages = []
    result = state.service._answer_evidence("Compare the fixture companies", Filters(), "fixture",
                                            retrieval_groups=groups(), progress=stages.append)
    assert result.status == "answered" and not result.evidence
    assert {c.evidence_type for c in result.citations} == {"image", "video"}
    assert {c.origin for c in result.citations} == {"human_description", "publisher_caption"}
    assert [(g["passages"], g["evidence_units"], g["cited_records"]) for g in result.structured_result["groups"]] == [(0, 1, 1), (0, 1, 1)]
    assert not state.web.calls and len(state.generation) == 1
    assert "media" in stages and result.research_trace["media_retrieval"]["recognition_calls"] == 0
    assert {tuple(filters.sponsors) for filters, _ in state.db.source_reads} == {("fixture-a",), ("fixture-b",)}


def test_retrieved_but_uncited_target_is_a_partial_comparison(setup_media):
    state = setup_media
    original = state.service.rag.generate

    def one_target(*args, **kwargs):
        result = original(*args, **kwargs)
        result.citations = result.citations[:1]
        return result

    state.service.rag.generate = one_target
    result = state.service._answer_evidence("Compare fixture companies", Filters(), "fixture", retrieval_groups=groups())
    assert result.status == "insufficient_evidence"
    assert [g["evidence_units"] for g in result.structured_result["groups"]] == [1, 1]
    assert [g["cited_records"] for g in result.structured_result["groups"]] == [1, 0]
    assert state.web.calls[0][0]["missing_topics"] == ["fixture-b"]


@pytest.mark.parametrize("change", ["asset", "bundle", "pause", "source_version"])
def test_media_change_during_generation_withholds_all_candidates_without_web(setup_media, change):
    state = setup_media
    state.db.body_hits = True
    original = state.service.rag.generate

    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "asset":
            (state.root / "fixture-a.fixture").write_bytes(b"Changed fixture asset")
        elif change == "bundle":
            state.path.write_bytes(state.path.read_bytes() + b"\n")
        elif change == "pause":
            state.rows["fixture-a-record"]["retrievable"] = False
        else:
            state.rows["fixture-a-record"]["version_id"] = sha256_text("revised")
        return result

    state.service.rag.generate = mutate
    result = state.service._answer_evidence("Describe carbon capture", Filters(), "fixture")
    assert result.status == "service_unavailable" and result.failure_reason == "media_evidence_mismatch"
    assert not result.evidence and not result.media_evidence and not result.citations
    assert not state.web.calls and len(state.generation) == 1
    state.service._ensure_answer("Describe carbon capture", Filters(), "fixture", result)
    assert not state.web.calls


def test_unverifiable_config_stops_before_budget_or_generation(setup_media):
    state = setup_media
    state.service.settings.media_bundle_sha256 = "0" * 64
    result = state.service._answer_evidence("Describe carbon capture", Filters(), "fixture")
    assert result.failure_reason == "media_evidence_mismatch"
    assert not state.charges and not state.generation and not state.web.calls


def test_media_trace_survives_tool_interpretation_audit(setup_media):
    state = setup_media
    state.service.settings.research_agent_enabled = True
    state.service.research_agent = Agent(ResearchRun(route="evidence", result={
        "filters": Filters(include_inferred_dates=True).model_dump(mode="json"), "search_query": "carbon capture",
    }))
    result = state.service.answer("Describe carbon capture", Filters(), "fixture")
    assert result.status == "answered"
    assert result.research_trace["media_retrieval"]["recognition_calls"] == 0
    assert result.research_trace["media_retrieval"]["groups"][0]["status"] == "ok"
    assert not state.web.calls


def test_missing_type_is_material_gap_not_negative_advertisement(setup_media):
    state = setup_media
    state.rows["fixture-b-record"]["retrievable"] = False
    result = state.service._answer_evidence("Compare fixture companies", Filters(), "fixture", retrieval_groups=groups())
    coverage = result.structured_result["groups"]
    assert result.status == "insufficient_evidence" and coverage[1]["media_status"] == "missing_material"
    assert coverage[1]["evidence_units"] == 0
    assert state.web.calls[0][0]["missing_topics"] == ["fixture-b"]


@pytest.mark.parametrize("failure", ["provider_incomplete", "answer_language_mismatch"])
def test_deferred_web_revalidates_media_and_keeps_cost_audit_when_asset_changes(setup_media, failure):
    state = setup_media
    state.service.rag.generate = lambda _q, body, _visitor, **kwargs: Answer(
        status="service_unavailable", answer="Fictional provider failure.", evidence=body,
        media_evidence=kwargs["media_evidence"], failure_reason=failure,
    )
    original = state.web.call

    def web_mutation(*args, **kwargs):
        result = original(*args, **kwargs)
        (state.root / "fixture-a.fixture").write_bytes(b"Changed while researching the web")
        return result

    state.web.call = web_mutation
    result = state.service._answer_evidence("Describe carbon capture", Filters(), "fixture")
    # An operational failure is not an evidence shortfall: no paid web lookup is made,
    # so nothing can change during one, and the original failure is preserved.
    assert result.status == "service_unavailable" and result.failure_reason == failure
    assert not result.external_research and not state.web.calls
    state.service._ensure_answer("Describe carbon capture", Filters(), "fixture", result)
    assert not state.web.calls


def test_one_record_in_two_groups_does_not_share_different_evidence_citations(setup_media):
    state = setup_media
    data = state.bundle.model_dump(mode="json")
    common = state.rows["fixture-a-record"]
    for asset in data["assets"]:
        asset.update(record_id=common["record_id"], version_id=common["version_id"])
    for index, artifact in enumerate(data["text_artifacts"]):
        text = "Alpha illustration description." if index == 0 else "Beta supplied captions."
        artifact.update(body=text, body_sha256=sha256_text(text))
        data["evidence"][index].update(record_id=common["record_id"], version_id=common["version_id"],
                                       text=text, derived_start=0, derived_end=len(text))
    bundle = MediaBundle.model_validate_json(json.dumps(data))
    raw = bundle.model_dump_json().encode()
    state.path.write_bytes(raw)
    state.service.settings.media_bundle_sha256 = sha256_bytes(raw)
    original = state.service.rag.generate

    def one_excerpt(*args, **kwargs):
        result = original(*args, **kwargs)
        result.citations = result.citations[:1]
        return result

    state.service.rag.generate = one_excerpt
    targets = [{"label": label, "query": query, "filters": Filters(sponsors=["fixture-a"]).model_dump(mode="json")}
               for label, query in (("Illustration", "alpha"), ("Captions", "beta"))]
    result = state.service._answer_evidence("Compare illustration and captions", Filters(), "fixture", retrieval_groups=targets)
    assert result.status == "insufficient_evidence"
    assert [g["evidence_units"] for g in result.structured_result["groups"]] == [1, 1]
    assert [g["cited_records"] for g in result.structured_result["groups"]] == [1, 0]
    assert state.web.calls[0][0]["missing_topics"] == ["Captions"]


