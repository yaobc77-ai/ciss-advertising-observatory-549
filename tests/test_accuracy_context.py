"""Fresh synthetic checks for quote context, not a semantic accuracy score."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from observatory.media_evidence import TimeLocation
from observatory.models import Evidence, MediaAnswerEvidence
from observatory.rag import (
    MAX_QUOTE_WORDS,
    Rag,
    answer_quote_catalog,
    materialize_selections,
    quote_catalog,
    selection_schema,
    validate_answer,
)


def source(text, *, evidence_id="ferry-evidence", record_id="synthetic-ferry", start=0):
    return Evidence(
        evidence_id=evidence_id,
        record_id=record_id,
        version_id="synthetic-source-v1",
        dataset="native",
        title="Ferry equipment notice",
        publisher="Harbor Bulletin",
        sponsor="Bay Ferry",
        text=text,
        start=start,
        end=start + len(text),
    )


def ferry_sentence():
    return (
        "The ferry notice quotes harbor engineer Mara Quinn, who reviewed the draft "
        "loading procedure with the quay team during a public planning session "
        "and explained the boarding layout, gangway width, waiting area, ticket desk, "
        "crew positions, passenger signs, entry route, exit route, safety barriers, "
        "weather arrangements, inspection schedule and emergency equipment before "
        "estimating that each vessel could carry at most 120 passengers per crossing "
        "after staff complete the trial boarding exercise and record every arrival "
        "using the same counting method, including children, visitors, volunteers, "
        "wheelchair users, bicycle users, guides and accompanying adults, while "
        "also checking the ramp, ropes, gates, seats, life jackets, lights, bells, "
        "radios and marked walkways with the full crew present at the quay, "
        "only if the harbor authority approves the proposed loading plan; "
        "the notice describes a planning estimate rather than licensed capacity."
    )


def selected(catalog, passage_id, *, text="The notice describes a conditional ferry planning estimate.", supports=()):
    return selection_schema(catalog).model_validate({
        "status": "answered",
        "claims": [{"passage_id": passage_id, "text": text, "support_passage_ids": list(supports)}],
        "summary": [{"text": "The notice presents a conditional estimate.", "citation_indices": [1]}],
        "sections": [{"title": "Ferry proposal", "citation_indices": [1]}],
    })


def middle_passage(catalog):
    return next(pid for pid, item in catalog.items() if "120 passengers" in item["quote"] and "Mara Quinn" not in item["quote"])


def test_clipped_quantity_window_cannot_be_selected_without_joint_support():
    evidence = source(ferry_sentence())
    catalog = quote_catalog([evidence])
    primary = middle_passage(catalog)
    assert "licensed capacity" not in catalog[primary]["quote"]
    with pytest.raises(ValueError, match="Incomplete sentence support"):
        materialize_selections(selected(catalog, primary), catalog)


def test_catalog_exposes_exact_neighbours_and_detected_sentence_boundaries():
    catalog = quote_catalog([source(ferry_sentence(), start=37)])
    primary = middle_passage(catalog)
    context = catalog[primary]["context"]
    assert context["sentence_clipped"] is True
    assert context["starts_sentence"] is False
    assert context["ends_sentence"] is False
    assert "Mara Quinn" in catalog[context["previous_passage_id"]]["quote"]
    assert "licensed capacity" in catalog[context["next_passage_id"]]["quote"]
    assert context["context_scope"] == "retrieved_contiguous_source_view_only"
    assert set(context["sentence_context_passage_ids"]) == set(catalog)
    for item in catalog.values():
        assert len(item["quote"].split()) <= MAX_QUOTE_WORDS
        assert item["quote"] in ferry_sentence()


def test_joint_support_keeps_one_claim_and_maps_all_exact_citations():
    evidence = source(ferry_sentence())
    catalog = quote_catalog([evidence])
    primary = middle_passage(catalog)
    supports = [pid for pid in catalog if pid != primary]
    model = materialize_selections(selected(catalog, primary, supports=supports), catalog)
    assert len(model.claims) == 1
    answer = validate_answer(model, [evidence])
    refs = list(range(1, len(catalog) + 1))
    assert len(answer.citations) == len(catalog)
    assert len(answer.cited_claims) == 1
    assert answer.cited_claims[0].citation_indices == refs
    assert answer.summary[0].citation_indices == refs
    assert answer.sections[0].citation_indices == refs
    assert answer.answer.count(model.claims[0].text) == 1
    assert all(c.quote in evidence.text and len(c.quote.split()) <= MAX_QUOTE_WORDS for c in answer.citations)


def test_missing_final_qualification_support_is_rejected():
    catalog = quote_catalog([source(ferry_sentence())])
    primary = middle_passage(catalog)
    first = next(iter(catalog))
    with pytest.raises(ValueError, match="Incomplete sentence support"):
        materialize_selections(selected(catalog, primary, supports=[first]), catalog)


def test_support_cannot_be_borrowed_from_an_unrelated_school_article():
    catalog = quote_catalog([
        source(ferry_sentence()),
        source("The school installed 120 lockers during the summer.", evidence_id="school-evidence", record_id="synthetic-school"),
    ])
    primary = middle_passage(catalog)
    other = next(pid for pid, item in catalog.items() if item["evidence_id"] == "school-evidence")
    with pytest.raises(ValueError, match="Unrelated passage support"):
        materialize_selections(selected(catalog, primary, supports=[other]), catalog)


@pytest.mark.parametrize("support_mode", ["primary", "duplicate", "unknown"])
def test_support_ids_must_be_distinct_real_additional_passages(support_mode):
    catalog = quote_catalog([source(ferry_sentence())])
    primary = middle_passage(catalog)
    other = next(pid for pid in catalog if pid != primary)
    supports = {"primary": [primary], "duplicate": [other, other], "unknown": ["Q999"]}[support_mode]
    parsed = SimpleNamespace(status="answered", claims=[SimpleNamespace(passage_id=primary, text="An estimate.", support_passage_ids=supports)])
    with pytest.raises(ValueError, match="Invalid passage support"):
        materialize_selections(parsed, catalog)


def test_complete_school_sentence_keeps_the_existing_single_citation_path():
    evidence = source("The school notice quotes principal Ada Rose saying that 40 desks were ordered, pending the board's approval.")
    catalog = quote_catalog([evidence])
    pid = next(iter(catalog))
    assert catalog[pid]["context"]["sentence_clipped"] is False
    answer = validate_answer(materialize_selections(selected(catalog, pid), catalog), [evidence])
    assert len(answer.citations) == 1
    assert answer.cited_claims[0].citation_indices == [1]


def test_long_sentence_joint_support_can_keep_original_chunk_identities():
    text = ferry_sentence()
    cut = text.index("after staff")
    catalog = quote_catalog([
        source(text[:cut], evidence_id="ferry-left"),
        source(text[cut:], evidence_id="ferry-right", start=cut),
    ])
    primary = middle_passage(catalog)
    supports = [pid for pid in catalog if pid != primary]
    answer = validate_answer(materialize_selections(selected(catalog, primary, supports=supports), catalog), [
        source(text[:cut], evidence_id="ferry-left"),
        source(text[cut:], evidence_id="ferry-right", start=cut),
    ])
    assert {citation.evidence_id for citation in answer.citations} == {"ferry-left", "ferry-right"}


def test_unsupported_number_in_a_complete_quote_remains_a_semantic_limit():
    # Source matching and full-sentence coverage do not entail the paraphrase.
    evidence = source("The equipment notice says the school ordered 40 desks.")
    catalog = quote_catalog([evidence])
    pid = next(iter(catalog))
    answer = validate_answer(materialize_selections(selected(catalog, pid, text="The school ordered 400 desks."), catalog), [evidence])
    assert answer.status == "answered"
    assert "400" in answer.cited_claims[0].text


def test_more_than_six_claims_remains_rejected_with_joint_support():
    evidence = source("The equipment notice says the school ordered 40 desks.")
    catalog = quote_catalog([evidence])
    parsed = selected(catalog, next(iter(catalog)))
    parsed.claims = parsed.claims * 7
    with pytest.raises(ValueError, match="Missing or excessive claims"):
        validate_answer(materialize_selections(parsed, catalog), [evidence])


def test_support_does_not_cross_article_versions_or_uncovered_intervals():
    left = source(ferry_sentence())
    other_version = source(ferry_sentence(), evidence_id="other-version").model_copy(update={"version_id": "synthetic-v2"})
    separate_interval = source("The equipment was inspected.", evidence_id="later-interval", start=left.end + 80)
    catalog = quote_catalog([left, other_version, separate_interval])
    primary = middle_passage(catalog)
    for evidence_id in ("other-version", "later-interval"):
        support = next(pid for pid, item in catalog.items() if item["evidence_id"] == evidence_id)
        with pytest.raises(ValueError, match="Unrelated passage support"):
            materialize_selections(selected(catalog, primary, supports=[support]), catalog)


def test_model_schema_restricts_support_ids_and_size():
    catalog = quote_catalog([source(ferry_sentence())])
    schema = selection_schema(catalog).model_json_schema()
    support = schema["$defs"]["SelectedClaim"]["properties"]["support_passage_ids"]
    assert set(support["items"]["enum"]) == set(catalog)
    assert support["maxItems"] == 5


def fake_generation(*, select_supports=True):
    calls, reservations, settled, uncertain = [], [], [], []

    class FakeDB:
        def validate_evidence(self, evidence):
            return True

        @contextmanager
        def connect(self):
            yield SimpleNamespace(execute=lambda *args: None)

    def parse(**request):
        calls.append(request)
        payload = json.loads(request["input"][1]["content"])
        item = payload["evidence"][0]
        primary = next(pid for pid, quote in item["quote_catalog"].items() if "120 passengers" in quote and "Mara Quinn" not in quote)
        supports = [pid for pid in item["passage_context"][primary]["sentence_context_passage_ids"] if pid != primary] if select_supports else []
        output = request["text_format"].model_validate({
            "status": "answered",
            "claims": [{"passage_id": primary, "text": "The ferry notice quotes Mara Quinn's conditional planning estimate.", "support_passage_ids": supports}],
            "summary": [{"text": "The notice presents a conditional planning estimate.", "citation_indices": [1]}],
            "sections": [{"title": "Ferry plan", "citation_indices": [1]}],
        })
        usage = SimpleNamespace(input_tokens=200, output_tokens=90, input_tokens_details=SimpleNamespace(cached_tokens=0), model_dump=lambda: {"input_tokens": 200, "output_tokens": 90})
        return SimpleNamespace(status="completed", output_parsed=output, usage=usage, model="gpt-5.6-luna")

    rag = Rag(FakeDB(), SimpleNamespace(generation_model="gpt-5.6-luna", max_output_tokens=512), client=SimpleNamespace(responses=SimpleNamespace(parse=parse)))
    rag.budget = SimpleNamespace(
        reserve=lambda *args: reservations.append(args) or "local-fake-reservation",
        settle=lambda *args: settled.append(args),
        uncertain=lambda *args: uncertain.append(args),
        cancel_unsent=lambda *args: None,
    )
    return rag, calls, reservations, settled, uncertain


def test_fake_generation_exposes_context_without_repeating_source_text_or_extra_calls():
    rag, calls, reservations, settled, uncertain = fake_generation()
    answer = rag.generate("What does the ferry notice propose?", [source(ferry_sentence())], "synthetic-visitor")
    assert answer.status == "answered"
    assert len(calls) == len(reservations) == len(settled) == 1
    assert uncertain == []
    assert calls[0]["max_output_tokens"] == 512
    payload = json.loads(calls[0]["input"][1]["content"])
    item = payload["evidence"][0]
    assert all(isinstance(quote, str) for quote in item["quote_catalog"].values())
    assert set(item["passage_context"]) == set(item["quote_catalog"])
    assert '"_view":' not in calls[0]["input"][1]["content"]
    assert "text" not in item
    assert len(answer.cited_claims) == 1 and len(answer.citations) == 3


def test_fake_bad_selection_fails_after_one_request_with_no_automatic_retry():
    rag, calls, reservations, settled, uncertain = fake_generation(select_supports=False)
    with pytest.raises(ValueError, match="Incomplete sentence support"):
        rag.generate("What does the ferry notice propose?", [source(ferry_sentence())], "synthetic-visitor")
    assert len(calls) == len(reservations) == len(settled) == len(uncertain) == 1


def test_existing_prompt_budget_limit_stops_dispatch_before_reservation():
    rag, calls, reservations, _, _ = fake_generation()
    oversized = source(ferry_sentence()).model_copy(update={"title": "F" * 100_001})
    with pytest.raises(ValueError, match="Evidence prompt exceeds app budget"):
        rag.generate("Read the ferry notice.", [oversized], "synthetic-visitor")
    assert calls == reservations == []


def test_joint_support_keeps_media_origin_and_derived_source_separate():
    text = ferry_sentence()
    media = MediaAnswerEvidence(
        evidence_id="synthetic-video",
        asset_id="synthetic-video-asset",
        record_id="synthetic-ferry",
        version_id="1" * 64,
        dataset="native",
        title="Ferry planning recording",
        media_type="video",
        origin="transcript",
        locator=TimeLocation(start_ms=5000, end_ms=35000),
        evidence_text=text,
        quality_label="synthetic_saved_transcript",
        asset_sha256="2" * 64,
        text_artifact_id="synthetic-transcript",
        artifact_sha256="3" * 64,
        derived_start=100,
        derived_end=100 + len(text),
    )
    article = source("The equipment notice links to a ferry planning recording.")
    catalog = answer_quote_catalog([article], [media])
    primary = middle_passage(catalog)
    supports = [pid for pid in catalog[primary]["context"]["sentence_context_passage_ids"] if pid != primary]
    answer = validate_answer(materialize_selections(selected(catalog, primary, supports=supports), catalog), [article], media_evidence=[media])
    assert len(answer.citations) == 3
    assert all(c.evidence_id == media.evidence_id and c.evidence_type == "video" and c.origin == "transcript" for c in answer.citations)
    assert answer.media_evidence[0].derived_start == 100
    assert answer.media_evidence[0].quote_from_original_body is False


def test_later_claim_and_overview_references_follow_expanded_citation_numbers():
    ferry = source(ferry_sentence())
    school = source("The school notice says 40 desks were ordered.", evidence_id="school", record_id="synthetic-school")
    catalog = quote_catalog([ferry, school])
    primary = middle_passage(catalog)
    supports = [pid for pid in catalog[primary]["context"]["sentence_context_passage_ids"] if pid != primary]
    school_id = next(pid for pid, item in catalog.items() if item["evidence_id"] == "school")
    parsed = selected(catalog, primary, supports=supports)
    claim_type = type(parsed.claims[0])
    parsed.claims.append(claim_type(passage_id=school_id, text="The school ordered 40 desks.", support_passage_ids=[]))
    parsed.summary.append(type(parsed.summary[0])(text="The school ordered desks.", citation_indices=[2]))
    parsed.sections.append(type(parsed.sections[0])(title="School equipment", citation_indices=[2]))
    answer = validate_answer(materialize_selections(parsed, catalog), [ferry, school])
    assert answer.cited_claims[1].citation_indices == [4]
    assert answer.summary[1].citation_indices == [4]
    assert answer.sections[1].citation_indices == [4]
    assert answer.citations[3].evidence_id == "school"


def test_neighbour_link_retains_speaker_for_a_later_complete_pronoun_sentence():
    introduction = (
        "The school notice introduces principal Ada Rose, who reviewed the equipment "
        "order with teachers, students, technicians, librarians, custodians, office staff, "
        "families and board members while inspecting the classrooms, corridors, halls, "
        "storage rooms, library, workshop, entrance, loading area, furniture, cables, "
        "screens, desks, chairs, shelves, lights and signs."
    )
    statement = "She says 40 desks were ordered, subject to the board's approval and a successful equipment inspection."
    catalog = quote_catalog([source(introduction + " " + statement)])
    primary = next(pid for pid, item in catalog.items() if item["quote"] == statement)
    context = catalog[primary]["context"]
    assert context["sentence_clipped"] is False
    previous = catalog[context["previous_passage_id"]]
    assert previous["quote"] == introduction
    assert "Ada Rose" in previous["quote"]
