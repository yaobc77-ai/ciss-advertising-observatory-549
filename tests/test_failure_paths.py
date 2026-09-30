"""Real PostgreSQL failure-path checks with a fully local, injected SDK double."""

import json
import os
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import APITimeoutError
from psycopg.conninfo import conninfo_to_dict

from observatory import rag as rag_module
from observatory.budget import price
from observatory.config import Settings
from observatory.db import Database
from observatory.models import Filters, ImportBatch, RecordInput
from observatory.rag import Rag
from observatory.service import Service

pytestmark = pytest.mark.integration

QUESTION = "What does the biogas project propose?"
SECRET_CANARY = "FAKE_TEST_SECRET_must_not_appear_in_public_output"
FILTERS = Filters(
    dataset="native",
    publishers=["Fixture Daily"],
    sponsors=["FixtureCo"],
    date_from=date(2022, 1, 1),
    date_to=date(2022, 12, 31),
    include_unknown_dates=False,
)
TABLES = "generation_outputs,answer_runs,usage_ledger,embeddings,imports,annotations,chunks,record_versions,records"


def embedding_response(*, dimensions=1536):
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=11,
            model_dump=lambda: {"prompt_tokens": 11, "total_tokens": 11},
        ),
        data=[SimpleNamespace(index=0, embedding=[1.0] + [0.0] * (dimensions - 1))],
    )


def timeout():
    return APITimeoutError(
        request=httpx.Request("POST", "https://api.openai.com/v1/responses")
    )


def fake_client(*, embedding_error=None, response_error=None, dimensions=1536):
    return SimpleNamespace(
        embeddings=SimpleNamespace(
            create=Mock(
                return_value=embedding_response(dimensions=dimensions),
                side_effect=embedding_error,
            )
        ),
        responses=SimpleNamespace(
            parse=Mock(
                side_effect=response_error
                or AssertionError(
                    "Unexpected generation dispatch in a local failure test"
                ),
            )
        ),
    )


@pytest.fixture
def failure_env(monkeypatch):
    Settings.from_env()
    url = os.environ.get("OBS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("OBS_TEST_DATABASE_URL is not configured")
    assert conninfo_to_dict(url).get("dbname") == "obs_test", (
        "Failure tests only permit the fixed disposable obs_test database"
    )
    db = Database(url)
    with db.connect() as conn:
        assert (
            conn.execute("SELECT current_database() AS name").fetchone()["name"]
            == "obs_test"
        )
    # An accidental attempt to construct the real SDK fails before any network call.
    monkeypatch.setattr(
        rag_module,
        "OpenAI",
        Mock(
            side_effect=AssertionError(
                "Real SDK construction is forbidden in failure tests"
            )
        ),
    )
    db.initialize()

    def clear():
        with db.connect() as conn:
            assert (
                conn.execute("SELECT current_database() AS name").fetchone()["name"]
                == "obs_test"
            )
            conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")

    clear()
    base = RecordInput(
        record_id="failure-allowed",
        dataset="native",
        publisher="Fixture Daily",
        sponsor="FixtureCo",
        published_at=date(2022, 5, 1),
        title="A fixture advertisement about a proposed project",
        body="The biogas project proposes using organic waste to produce fuel. The advertisement describes a planned facility, not measured operating results.",
        retrievable=True,
        url="https://example.org/fixture-advertisement",
    )
    db.import_batch(
        ImportBatch(
            records=[
                base,
                base.model_copy(
                    update={"record_id": "failure-wrong-sponsor", "sponsor": "OtherCo"}
                ),
                base.model_copy(
                    update={
                        "record_id": "failure-wrong-publisher",
                        "publisher": "Other Daily",
                    }
                ),
                base.model_copy(
                    update={
                        "record_id": "failure-wrong-year",
                        "published_at": date(2023, 5, 1),
                    }
                ),
                base.model_copy(
                    update={
                        "record_id": "failure-social",
                        "dataset": "social",
                        "platform": "Fixture platform",
                    }
                ),
            ]
        )
    )

    def build(client, **overrides):
        settings = replace(Settings(database_url=url), **overrides)
        rag = Rag(db, settings, client=client)
        return Service(settings, db=db, rag=rag)

    try:
        yield db, build
    finally:
        clear()


def ledger(db):
    with db.connect() as conn:
        return conn.execute(
            "SELECT kind,state,actual_usd,reserved_usd,usage FROM usage_ledger ORDER BY kind"
        ).fetchall()


def assert_filtered_fallback(db, result, status="service_unavailable"):
    assert result.status == status
    assert {e.record_id for e in result.evidence} == {"failure-allowed"}
    assert all(db.validate_evidence(e) for e in result.evidence)
    assert all(
        e.dataset == "native"
        and e.publisher == "Fixture Daily"
        and e.sponsor == "FixtureCo"
        for e in result.evidence
    )
    assert SECRET_CANARY not in result.model_dump_json()
    with db.connect() as conn:
        saved = conn.execute(
            "SELECT result FROM answer_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()["result"]
        assert saved == result.model_dump(mode="json")
        assert SECRET_CANARY not in json.dumps(saved)
        assert (
            conn.execute("SELECT count(*) AS n FROM generation_outputs").fetchone()["n"]
            == 0
        )


def test_sent_generation_timeout_keeps_exposure_and_filtered_evidence(failure_env):
    db, build = failure_env
    client = fake_client(response_error=timeout())
    service = build(client)
    result = service.answer(QUESTION, FILTERS, "generation-timeout")
    assert_filtered_fallback(db, result)
    assert result.failure_reason == "APITimeoutError"
    assert client.embeddings.create.call_count == client.responses.parse.call_count == 1
    rows = {row["kind"]: row for row in ledger(db)}
    assert rows["generation"]["state"] == "uncertain"
    assert rows["generation"]["actual_usd"] is None
    assert rows["generation"]["reserved_usd"] == Decimal("0.04")
    assert rows["generation"]["usage"] == {"error_type": "APITimeoutError"}
    assert rows["embedding"]["state"] == "settled"
    expected = Decimal("0.04") + price(service.settings.embedding_model, 11)
    assert service.rag.budget.summary()[0]["exposure_usd"] == expected
    assert result.cost_usd == pytest.approx(float(expected))


def test_embedding_timeout_cancels_unsent_generation_but_retains_embedding_exposure(
    failure_env,
):
    db, build = failure_env
    client = fake_client(embedding_error=timeout())
    service = build(client)
    result = service.answer(QUESTION, FILTERS, "embedding-timeout")
    assert_filtered_fallback(db, result)
    assert client.embeddings.create.call_count == 1
    client.responses.parse.assert_not_called()
    rows = {row["kind"]: row for row in ledger(db)}
    assert (
        rows["generation"]["state"] == "cancelled"
        and rows["generation"]["actual_usd"] == 0
    )
    assert (
        rows["embedding"]["state"] == "uncertain"
        and rows["embedding"]["actual_usd"] is None
    )
    assert rows["embedding"]["reserved_usd"] > 0
    assert (
        service.rag.budget.summary()[0]["exposure_usd"]
        == rows["embedding"]["reserved_usd"]
    )
    assert result.cost_usd == pytest.approx(float(rows["embedding"]["reserved_usd"]))


def test_malformed_embedding_is_charged_but_never_dispatched_to_generation(failure_env):
    db, build = failure_env
    client = fake_client(dimensions=8)
    service = build(client)
    result = service.answer(QUESTION, FILTERS, "bad-embedding")
    assert_filtered_fallback(db, result)
    client.responses.parse.assert_not_called()
    rows = {row["kind"]: row for row in ledger(db)}
    assert (
        rows["generation"]["state"] == "cancelled"
        and rows["generation"]["actual_usd"] == 0
    )
    assert rows["embedding"]["state"] == "settled"
    assert rows["embedding"]["actual_usd"] == price(
        service.settings.embedding_model, 11
    )
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM embeddings").fetchone()["n"] == 0


def test_local_generation_preparation_failure_cancels_unsent_reservation(
    failure_env, monkeypatch
):
    db, build = failure_env
    client = fake_client()
    service = build(client)
    monkeypatch.setattr(
        rag_module, "quote_catalog", Mock(side_effect=ValueError(SECRET_CANARY))
    )
    result = service.answer(QUESTION, FILTERS, "local-preparation-failure")
    assert_filtered_fallback(db, result)
    assert result.failure_reason == "ValueError"
    client.responses.parse.assert_not_called()
    rows = {row["kind"]: row for row in ledger(db)}
    assert (
        rows["generation"]["state"] == "cancelled"
        and rows["generation"]["actual_usd"] == 0
    )
    assert rows["embedding"]["state"] == "settled"


def test_provider_exception_secrets_are_not_returned_or_saved(failure_env):
    db, build = failure_env
    client = fake_client(embedding_error=RuntimeError(SECRET_CANARY))
    result = build(client).answer(QUESTION, FILTERS, "provider-secret-error")
    assert_filtered_fallback(db, result)
    assert result.failure_reason == "RuntimeError"
    assert SECRET_CANARY not in json.dumps(ledger(db), default=str)
    client.responses.parse.assert_not_called()


@pytest.mark.parametrize(
    "reason", ["monthly_budget", "concurrency", "visitor_minute", "visitor_day"]
)
def test_admission_rejection_happens_before_any_sdk_call(failure_env, reason):
    db, build = failure_env
    client = fake_client()
    overrides = {
        "monthly_budget": {"monthly_budget_usd": Decimal("0.01")},
        "concurrency": {"concurrency": 1},
        "visitor_minute": {"requests_per_minute": 1},
        "visitor_day": {"requests_per_minute": 100, "requests_per_day": 1},
    }[reason]
    service = build(client, **overrides)
    if reason != "monthly_budget":
        previous_visitor = (
            "other-visitor" if reason == "concurrency" else "limited-visitor"
        )
        reserved = service.rag.budget.reserve(
            "0.04", previous_visitor, "generation", service.settings.generation_model
        )
        if reason.startswith("visitor_"):
            service.rag.budget.cancel_unsent(reserved)
    before = ledger(db)
    result = service.answer(QUESTION, FILTERS, "limited-visitor")
    assert_filtered_fallback(db, result, status="limited")
    client.embeddings.create.assert_not_called()
    client.responses.parse.assert_not_called()
    assert ledger(db) == before  # No embedding or generation reservation was added.
    assert result.cost_usd == 0


def test_completed_generation_records_prompt_schema_model_and_original_citation(
    failure_env,
):
    db, build = failure_env
    client = fake_client()

    def completed_response(**request):
        payload = json.loads(request["input"][1]["content"])
        first_passage = next(iter(payload["evidence"][0]["quote_catalog"]))
        parsed = request["text_format"].model_validate(
            {
                "status": "answered",
                "claims": [
                    {
                        "passage_id": first_passage,
                        "text": "The advertisement proposes using organic waste to produce fuel.",
                    }
                ],
            }
        )
        return SimpleNamespace(
            status="completed",
            output_parsed=parsed,
            model="fixture-provider-model",
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=0, cache_write_tokens=0
                ),
                model_dump=lambda: {"input_tokens": 100, "output_tokens": 20},
            ),
        )

    client.responses.parse.side_effect = completed_response
    service = build(client)
    result = service.answer(QUESTION, FILTERS, "completed-generation")
    assert result.status == "answered" and len(result.citations) == 1
    assert all(db.validate_evidence(e) for e in result.evidence)
    assert result.citations[0].quote in result.evidence[0].text
    row = next(row for row in ledger(db) if row["kind"] == "generation")
    assert row["state"] == "settled"
    assert row["actual_usd"] == price(service.settings.generation_model, 100, 20)
    audit = row["usage"]["observatory_request"]
    sent_system = client.responses.parse.call_args.kwargs["input"][0]["content"]
    assert audit["prompt_sha256"] == rag_module.digest(sent_system)
    assert audit["base_prompt_sha256"] == rag_module.digest(rag_module.SYSTEM)
    assert audit["target_language"]["code"] == "en"
    assert "Required answer language: English (en)" in sent_system
    assert len(audit["schema_sha256"]) == 64
    assert audit["provider_model"] == "fixture-provider-model"


def test_wrong_language_is_withheld_after_settlement_without_retry(failure_env):
    db, build = failure_env
    client = fake_client()
    wrong_text = (
        "La publicidad propone utilizar residuos orgánicos para producir combustible "
        "en una instalación que todavía está en fase de planificación."
    )

    def completed_response(**request):
        payload = json.loads(request["input"][1]["content"])
        passage = next(iter(payload["evidence"][0]["quote_catalog"]))
        return SimpleNamespace(
            status="completed",
            output_parsed=request["text_format"].model_validate(
                {
                    "status": "answered",
                    "claims": [{"passage_id": passage, "text": wrong_text}],
                }
            ),
            model="fixture-provider-model",
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                input_tokens_details=SimpleNamespace(
                    cached_tokens=0, cache_write_tokens=0
                ),
                model_dump=lambda: {"input_tokens": 100, "output_tokens": 20},
            ),
        )

    client.responses.parse.side_effect = completed_response
    service = build(client)
    result = service.answer(QUESTION, FILTERS, "wrong-language")
    assert result.status == "service_unavailable"
    assert result.failure_reason == "answer_language_mismatch"
    assert result.language_check["status"] == "mismatch"
    assert result.language_check["target"]["code"] == "en"
    assert not result.citations and wrong_text not in result.answer
    assert {e.record_id for e in result.evidence} == {"failure-allowed"}
    assert all(db.validate_evidence(e) for e in result.evidence)
    assert client.responses.parse.call_count == client.embeddings.create.call_count == 1
    rows = ledger(db)
    assert all(row["state"] == "settled" for row in rows)
    expected = price(service.settings.generation_model, 100, 20) + price(
        service.settings.embedding_model, 11
    )
    assert result.cost_usd == pytest.approx(float(expected))
    with db.connect() as conn:
        raw = conn.execute("SELECT parsed FROM generation_outputs").fetchall()
        assert len(raw) == 1 and raw[0]["parsed"]["claims"][0]["text"] == wrong_text
        saved = conn.execute("SELECT result FROM answer_runs").fetchall()
        assert len(saved) == 1 and saved[0]["result"] == result.model_dump(mode="json")
