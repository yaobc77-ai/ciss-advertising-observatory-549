from decimal import Decimal
from types import SimpleNamespace

import pytest

from observatory.budget import price
from observatory.config import Settings
from observatory.models import Answer, Evidence, Filters
from observatory.rag import (
    MAX_QUOTE_WORDS,
    GroundedClaim,
    ModelAnswer,
    Rag,
    materialize_selections,
    quote_catalog,
    selection_schema,
    validate_answer,
)
from observatory.service import Service, safe_url, summarize


def test_billing_includes_cache_writes_and_cached_reads():
    assert price(
        "gpt-5.6-luna", 1000, 100, cached_tokens=100, cache_write_tokens=800
    ) == Decimal("0.000342")
    with pytest.raises(ValueError):
        price("gpt-5.6-luna", 10, cached_tokens=20)


def evidence():
    return Evidence(
        evidence_id="e1",
        record_id="r1",
        version_id="v1",
        dataset="native",
        title="Ad",
        url="https://example.org",
        text="The company proposes capturing carbon dioxide.",
        start=0,
        end=len("The company proposes capturing carbon dioxide."),
    )


def test_counts_share_filtered_denominator_and_unknown_bucket():
    rows = [
        dict(
            publisher="One",
            sponsor="A",
            platform="",
            keyword="gas",
            date="2020-01-01",
            retrievable=True,
        ),
        dict(
            publisher="One",
            sponsor="",
            platform="",
            keyword="gas",
            date=None,
            retrievable=False,
        ),
    ]
    result = summarize(rows)
    assert result["total"] == 2 and result["unknown_dates"] == 1
    assert result["publishers"] == [{"name": "One", "count": 2, "percent": 100.0}]
    assert sum(r["count"] for r in result["timeline"]) == 2
    assert sum(r["percent"] for r in result["sponsors"]) == 100


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///C:/secret",
        "https://user:pass@example.org",
        "//evil.org",
        "https://",
    ],
)
def test_source_urls_are_http_without_credentials(url):
    assert safe_url(url) == ""


def test_source_toggle_applies_to_service_evidence():
    service = Service(
        Settings(show_source_links=False),
        db=SimpleNamespace(
            search=lambda *a, **k: [evidence()], public_rows=lambda *a: []
        ),
        rag=object(),
    )
    assert service.search("carbon", Filters())[0].url == ""


def test_invented_or_nonverbatim_citation_blocked():
    for eid, quote in [
        ("invented", "carbon"),
        ("e1", "The project is already working."),
    ]:
        with pytest.raises(ValueError):
            validate_answer(
                ModelAnswer(
                    status="answered",
                    claims=[GroundedClaim(text="Claim", evidence_id=eid, quote=quote)],
                ),
                [evidence()],
            )


def test_verbatim_citation_retains_versioned_evidence():
    result = validate_answer(
        ModelAnswer(
            status="answered",
            claims=[
                GroundedClaim(
                    text="The advertisement proposes carbon capture.",
                    evidence_id="e1",
                    quote="capturing carbon dioxide",
                )
            ],
        ),
        [evidence()],
    )
    assert result.status == "answered" and result.evidence[0].version_id == "v1"


def test_no_evidence_output_does_not_publish_model_claims():
    result = validate_answer(
        ModelAnswer(
            status="insufficient_evidence",
            claims=[GroundedClaim(text="Fabrication", evidence_id="x", quote="x")],
        ),
        [evidence()],
    )
    assert (
        result.status == "insufficient_evidence"
        and not result.citations
        and "Fabrication" not in result.answer
    )


def test_unsupported_count_question_never_calls_model():
    service = Service(Settings(), db=object(), rag=object())
    result = service.answer(
        "How many ads contain greenwashing claims?", Filters(), "test"
    )
    assert result.status == "insufficient_evidence"
    assert result.answer_mode == "clarification"
    assert "validated" in result.answer


def test_explicit_article_title_does_not_mix_other_articles():
    seen = []
    rows = [{"title": "Biogas to offset air travel emissions", "record_id": "r1"}]

    def search(q, f, **kwargs):
        seen.append(f)
        return [evidence()]

    service = Service(
        Settings(),
        db=SimpleNamespace(search=search, public_rows=lambda f: rows),
        rag=object(),
    )
    service.search(
        "What does Biogas to offset air travel emissions claim?",
        Filters(sponsors=["Total"]),
    )
    assert seen[0].record_ids == ["r1"] and seen[0].sponsors == ["Total"]


def test_quote_selection_preserves_unicode_and_limits_length_without_model_copying():
    text = "CO₂ emissions — " + " ".join(f"word{i}" for i in range(60))
    ev = evidence().model_copy(
        update={"text": text, "end": len(text)}
    )
    catalog = quote_catalog([ev])
    assert all(
        c["quote"] in ev.text and len(c["quote"].split()) <= MAX_QUOTE_WORDS
        for c in catalog.values()
    )
    schema = selection_schema(catalog)
    with pytest.raises(ValueError):
        schema.model_validate(
            {
                "status": "answered",
                "claims": [{"text": "Claim", "passage_id": "invented"}],
            }
        )
    parsed = schema.model_validate(
        {"status": "answered", "claims": [{"text": "Claim", "passage_id": "Q1"}]}
    )
    grounded = materialize_selections(parsed, catalog)
    assert grounded.claims[0].quote.startswith("CO₂ emissions —")
    assert grounded.claims[0].evidence_id == ev.evidence_id


def test_citation_keeps_capacity_units_qualifiers_and_abbreviations_together():
    text = (
        "Dr. Doe says the new U.S. facility could produce up to 1 billion cubic "
        "feet a day of blue hydrogen once completed. It is a plan, not an operating result."
    )
    catalog = quote_catalog([evidence().model_copy(update={"text": text, "end": len(text)})])
    assert list(catalog.values())[0]["quote"] == text
    assert all(p["quote"] in text for p in catalog.values())


def test_local_quote_preparation_failure_releases_unsent_reservation(monkeypatch):
    def invalid_spans(_):
        raise ValueError("Sentence offsets failed source validation")

    monkeypatch.setattr("observatory.rag.quote_catalog", invalid_spans)
    cancelled = []
    rag = Rag(
        SimpleNamespace(validate_evidence=lambda e: True), Settings(), client=object()
    )
    rag.budget = SimpleNamespace(cancel_unsent=cancelled.append)
    with pytest.raises(ValueError, match="Sentence offsets"):
        rag.generate("Question", [evidence()], "visitor", reservation="r1")
    assert cancelled == ["r1"]


def test_index_change_during_generation_hides_answer_and_keeps_paid_audit():
    state = {"version": "source-v1:index-legacy"}
    saved = []
    generated = []
    cancelled = []

    def embed(texts, visitor, cost_sink):
        cost_sink.append(0.001)
        return [[0.0]]

    def generate(question, passages, visitor, reservation):
        generated.append((question, passages, reservation))
        state["version"] = "source-v1:index-sentence"
        return Answer(
            status="answered",
            answer="An answer prepared from the previous index.",
            evidence=passages,
        )

    service = Service(
        Settings(),
        db=SimpleNamespace(
            health=lambda: {"data_version": state["version"]},
            public_rows=lambda _: [],
            search=lambda *args, **kwargs: [evidence()],
            save_answer=lambda *args: saved.append(args),
        ),
        rag=SimpleNamespace(
            embed=embed,
            generate=generate,
            budget=SimpleNamespace(
                reserve=lambda *args: "reservation-1",
                cancel_unsent=cancelled.append,
                reservation_cost=lambda _: 0.01,
            ),
        ),
    )

    result = service.answer("What does the company propose?", Filters(), "visitor")

    assert len(generated) == 1 and generated[0][2] == "reservation-1"
    assert cancelled == []  # The generation was dispatched and remains chargeable.
    assert result.status == "service_unavailable"
    assert result.failure_reason == "data_changed_during_generation"
    assert not result.evidence and not result.citations
    assert "previous index" not in result.answer
    assert result.cost_usd == pytest.approx(0.011)
    assert len(saved) == 1
    assert saved[0][2] is result
    assert saved[0][3] == "source-v1:index-sentence"
