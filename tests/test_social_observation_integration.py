"""Real PostgreSQL checks in freshly created, owned obs_test databases only."""

import json
from contextlib import contextmanager
from copy import deepcopy
from uuid import uuid4

import pytest
from postgres_fixtures import (
    OBSERVATION_DATABASE_PREFIX,
    configured_private_admin_url,
    configured_test_url,
    validate_owned_observation_database,
    verified_private_admin_connection,
    verified_private_application_owner,
    verified_test_connection,
)
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from observatory.db import Database, ImportPreconditionFailed
from observatory.indexing import (
    EMBEDDING_MODEL,
    SENTENCE_PROFILE,
    SOCIAL_OBSERVATION_PROFILE,
    IndexManager,
)
from observatory.models import Filters, ImportBatch, RecordInput
from observatory.quality import body_hash
from observatory.social_admission import _admit_group
from observatory.social_source_retrieval import (
    SCHEMA_VERSION,
    source_policy_enabled,
    source_version_id,
    validated_observations,
)

pytestmark = pytest.mark.integration


@contextmanager
def owned_database(record_testsuite_property, label=""):
    configured = configured_test_url()
    configured_private_admin_url()
    name = validate_owned_observation_database(OBSERVATION_DATABASE_PREFIX + uuid4().hex[:12])
    with verified_test_connection(configured) as app:
        owner = verified_private_application_owner(app)
    created = False
    try:
        with verified_private_admin_connection(autocommit=True) as admin:
            assert admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone() is None
            admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(owner)))
            created = True
        connection = make_conninfo(configured, dbname=name)
        with verified_private_admin_connection(name, autocommit=True) as admin:
            assert admin.execute("SELECT current_database() AS name").fetchone()["name"] == name
            admin.execute("CREATE EXTENSION vector")
        result = Database(connection)
        with result.connect() as conn:
            assert conn.execute("SELECT current_database() AS name").fetchone()["name"] == name
            assert verified_private_application_owner(conn) == owner
        result.initialize()
        record_testsuite_property(label + "isolated_database", name)
        record_testsuite_property(label + "main_database_connections", 0)
        record_testsuite_property(label + "paid_model_calls", 0)
        record_testsuite_property(label + "application_role_restricted", True)
        yield result
    finally:
        if created:
            validate_owned_observation_database(name)
            with verified_private_admin_connection(autocommit=True) as admin:
                actual = admin.execute(
                    "SELECT pg_get_userbyid(datdba) AS owner FROM pg_database WHERE datname=%s", (name,),
                ).fetchone()
                assert actual is not None and actual["owner"] == owner
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
                assert admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone() is None
            record_testsuite_property(label + "isolated_database_dropped", True)


@pytest.fixture(scope="module")
def fresh_database(record_testsuite_property):
    with owned_database(record_testsuite_property) as db:
        yield db


def saved_post(index, bodies, reason):
    url = f"https://twitter.com/example/status/{90000 + index}"
    sources = []
    for variant, body in enumerate(bodies):
        identifier = str(1000 + index * 3 + variant)
        sources.append((index * 3 + variant + 1, RecordInput(
            record_id="junkipedia:" + identifier, dataset="social", url=url, platform="Twitter",
            account="Synthetic account", sponsor="Synthetic company", body=body,
            countable=False, retrievable=False,
            raw={"body_sha256": body_hash(body), "source_row": {"id": identifier, "post_text": body}},
        )))
    record, _ = _admit_group(sources, url, "b" * 64, {
        "data_sha256": "c" * 64, "selections": {"D02": {"id": "confirmed-D02"}, "D03": {"id": "confirmed-D03"}},
    })
    record.retrievable = False
    record.raw["social_admission"]["retrieval_status"] = reason
    return record


def enabled(record):
    old = record.model_dump(mode="json")
    observations = validated_observations(old)
    new = deepcopy(old)
    new["retrievable"] = True
    new["raw"]["social_source_retrieval"] = {
        "schema_version": SCHEMA_VERSION, "source_preservation": True,
        "status": "enabled_source_observations", "reason": old["raw"]["social_admission"]["retrieval_status"],
        "expected_old_version_id": source_version_id(old), "expected_old_body_hash": body_hash(old["body"]),
        "input_batch_sha256": "d" * 64,
        "source_observation_versions": {o["source_observation_id"]: o["source_version_id"] for o in observations},
        "semantic_completeness_verified": False, "paid_ad_status": "unknown",
    }
    assert source_policy_enabled(new)
    return RecordInput.model_validate_json(json.dumps(new))


def native_snapshot(db, profile=SENTENCE_PROFILE):
    with db.connect() as conn:
        chunks = conn.execute(
            "SELECT c.* FROM chunks c JOIN chunk_profile_membership m USING(chunk_id) "
            "JOIN records r ON r.record_id=c.record_id AND r.current_version=c.version_id "
            "WHERE r.dataset='native' AND r.active AND m.profile_id=%s ORDER BY c.chunk_id", (profile,),
        ).fetchall()
        versions = conn.execute(
            "SELECT r.record_id,r.current_version,v.body,v.body_hash,v.payload FROM records r "
            "JOIN record_versions v ON v.version_id=r.current_version AND v.record_id=r.record_id "
            "WHERE r.dataset='native' ORDER BY r.record_id",
        ).fetchall()
        vectors = conn.execute("SELECT text_hash,model,embedding::text AS embedding FROM embeddings ORDER BY text_hash,model").fetchall()
    return {"chunks": chunks, "versions": versions, "vectors": vectors}


@pytest.fixture(scope="module")
def scenario(fresh_database):
    db = fresh_database
    manager = IndexManager(db)
    first = RecordInput(record_id="native:one", dataset="native", url="https://example.test/native/one",
                        title="First native source", body="Native carbon capture baseline.", retrievable=True)
    db.import_batch(ImportBatch(records=[first]))
    manager.prepare(SENTENCE_PROFILE)
    vector = "[" + ",".join(["1"] + ["0"] * 1535) + "]"
    with db.connect() as conn:
        hashes = conn.execute("SELECT DISTINCT text_hash FROM chunks WHERE record_id=%s", (first.record_id,)).fetchall()
        for item in hashes:
            conn.execute("INSERT INTO embeddings(text_hash,model,embedding) VALUES (%s,%s,%s::vector)",
                         (item["text_hash"], EMBEDDING_MODEL, vector))
    current = manager.status(SENTENCE_PROFILE)
    manager.activate(SENTENCE_PROFILE, expected_source_data_version=current["source_data_version"])
    second = RecordInput(record_id="native:two", dataset="native", url="https://example.test/native/two",
                         title="Second native source", body="Native drilling research newly imported.", retrievable=True)
    db.import_batch(ImportBatch(records=[second]))
    assert manager.status(SENTENCE_PROFILE)["missing_embeddings"] == 1
    posts = []
    for i in range(32):
        if i < 13:
            bodies, reason = (f"Warmup{i} source. ♨️Where are new district heating networks being explored?",), "paused_sentence_source_validation"
        elif i < 24:
            bodies, reason = (f"Chargingcredit{i} special offer. Terms and conditions apply.",), "paused_text_quality"
        else:
            body = f"Observationcommon{i} company report."
            bodies, reason = (body, body + f" Extra linkedtail{i} climate commitment."), "paused_body_disagreement"
        posts.append(saved_post(i, bodies, reason))
    db.import_batch(ImportBatch(records=posts))
    baseline = native_snapshot(db)
    expected_current = {r.record_id: {
        "version_id": source_version_id(r.model_dump(mode="json")), "body_sha256": body_hash(r.body),
        "url": r.url, "dataset": "social",
    } for r in posts}
    targets = [enabled(record) for record in posts]
    result = db.import_batch(ImportBatch(records=targets), expected_current=expected_current)
    assert result["new_versions"] == 32 and result["precondition_checked"] == 32
    before = db.health()
    assert before["active_profile"] == SENTENCE_PROFILE
    retry = db.import_batch(ImportBatch(records=targets), expected_current=expected_current)
    assert retry["new_versions"] == 0 and retry["unchanged"] == retry["already_applied"] == 32
    assert db.health() == before
    prepared = manager.prepare(SOCIAL_OBSERVATION_PROFILE)
    assert prepared["prepared_snapshot_matches"] and prepared["missing_embeddings"] > 0
    with pytest.raises(ValueError, match="embeddings are incomplete"):
        manager.activate(SOCIAL_OBSERVATION_PROFILE, expected_source_data_version=prepared["source_data_version"])
    assert db.health()["active_profile"] == SENTENCE_PROFILE
    active = manager.activate_social_observation_profile(
        expected_source_data_version=prepared["source_data_version"], expected_previous_profile=SENTENCE_PROFILE,
    )
    assert active["active_profile"] == SOCIAL_OBSERVATION_PROFILE
    assert native_snapshot(db, SOCIAL_OBSERVATION_PROFILE) == baseline
    return {"db": db, "manager": manager, "posts": posts, "targets": targets,
            "expected_current": expected_current, "baseline": baseline}


def test_guarded_32_updates_unpaid_activation_and_native_vectors_preserved(scenario):
    db = scenario["db"]
    assert db.health()["countable_record_counts"] == {"native": 2, "social": 32}
    assert native_snapshot(db, SOCIAL_OBSERVATION_PROFILE) == scenario["baseline"]
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM usage_ledger").fetchone()["n"] == 0
        assert conn.execute(
            "SELECT count(DISTINCT c.record_id) AS n FROM chunks c "
            "JOIN chunk_profile_membership m USING(chunk_id) WHERE m.profile_id=%s AND c.source_observation_id IS NOT NULL",
            (SOCIAL_OBSERVATION_PROFILE,),
        ).fetchone()["n"] == 32


def test_distinct_saved_observations_and_extra_tail_are_searchable(scenario):
    db, record = scenario["db"], scenario["targets"][24]
    filters = Filters(dataset="social", record_ids=[record.record_id])
    common = db.search("Observationcommon24", filters, chunks_per_record=8)
    sources = validated_observations(record.model_dump(mode="json"))
    assert {e.source_observation_id for e in common} == {o["source_observation_id"] for o in sources}
    assert all(e.source_conflicts == ["body"] and e.source_observation_count == 2 for e in common)
    tails = db.search("linkedtail24", filters, chunks_per_record=8)
    assert len(tails) == 1 and tails[0].source_observation_id == sources[1]["source_observation_id"]
    assert all(db.validate_evidence(e) for e in common + tails)


@pytest.mark.parametrize("index,query", [(0, "district heating"), (13, "Chargingcredit13")])
def test_unicode_and_footer_text_keep_exact_source_intervals(scenario, index, query):
    db, record = scenario["db"], scenario["targets"][index]
    found = db.search(query, Filters(dataset="social", record_ids=[record.record_id]), chunks_per_record=8)
    assert found
    bodies = {o["source_observation_id"]: o["body"] for o in validated_observations(record.model_dump(mode="json"))}
    for evidence in found:
        assert evidence.text == bodies[evidence.source_observation_id][evidence.start:evidence.end]
        assert db.validate_evidence(evidence)
    assert any("♨️" in e.text for e in found) if index == 0 else any("Terms and conditions" in e.text for e in found)


def test_current_record_projection_has_exact_public_observations(scenario):
    db, record = scenario["db"], scenario["targets"][24]
    actual = db.versioned_record(Filters(dataset="social"), record.record_id)
    assert len(actual["social_source_observations"]) == 2
    assert actual["social_admission"]["retrieval_status"] == "enabled_source_observations"
    assert "observation_payload" not in actual


@pytest.mark.parametrize("tamper", ["observation_id", "body_hash", "offset_boolean", "source_text"])
def test_database_revalidates_tampered_evidence(scenario, tamper):
    db, record = scenario["db"], scenario["targets"][24]
    evidence = db.search("Observationcommon24", Filters(dataset="social", record_ids=[record.record_id]), chunks_per_record=8)[0]
    changes = {
        "observation_id": {"source_observation_id": "junkipedia:999999"},
        "body_hash": {"source_body_hash": "e" * 64},
        "offset_boolean": {"start": False},
        "source_text": {"text": evidence.text + " fabricated"},
    }
    assert db.validate_evidence(evidence)
    assert db.validate_evidence(evidence.model_copy(update=changes[tamper])) is False


def test_in_place_payload_drift_is_rejected_then_exactly_restored(scenario):
    db, record = scenario["db"], scenario["targets"][24]
    evidence = db.search("Observationcommon24", Filters(dataset="social", record_ids=[record.record_id]), chunks_per_record=8)[0]
    with db.connect() as conn:
        original = conn.execute("SELECT payload FROM record_versions WHERE version_id=%s", (evidence.version_id,)).fetchone()["payload"]
    changed = deepcopy(original)
    changed["title"] += " changed in place"
    try:
        with db.connect() as conn:
            conn.execute("UPDATE record_versions SET payload=%s WHERE version_id=%s", (Jsonb(changed), evidence.version_id))
        assert db.validate_evidence(evidence) is False
        with pytest.raises(ValueError, match="payload version"):
            db.versioned_record(Filters(dataset="social"), record.record_id)
    finally:
        with db.connect() as conn:
            conn.execute("UPDATE record_versions SET payload=%s WHERE version_id=%s", (Jsonb(original), evidence.version_id))
    assert db.validate_evidence(evidence)


def test_guarded_import_rejects_wrong_old_binding_without_write(scenario):
    db = scenario["db"]
    record = scenario["targets"][0].model_copy(deep=True)
    record.title += " new candidate"
    wrong = deepcopy(scenario["expected_current"][record.record_id])
    wrong["version_id"] = "f" * 64
    before = db.health()
    with pytest.raises(ImportPreconditionFailed):
        db.import_batch(ImportBatch(records=[record]), expected_current={record.record_id: wrong})
    assert db.health() == before


def test_normal_activation_gate_still_rejects_incomplete_vectors(scenario):
    manager, db = scenario["manager"], scenario["db"]
    before = db.health()
    with pytest.raises(ValueError, match="embeddings are incomplete"):
        manager.activate(SOCIAL_OBSERVATION_PROFILE, expected_source_data_version=before["source_data_version"])
    assert db.health() == before


@pytest.fixture(scope="module")
def literal_scenario(record_testsuite_property):
    with owned_database(record_testsuite_property, "literal_") as db:
        manager = IndexManager(db)
        native = RecordInput(
            record_id="native:literal", dataset="native", url="https://example.test/native/literal",
            title="Native stopword phrase", body="How about this?", retrievable=True,
        )
        keyword = RecordInput(
            record_id="native:keyword", dataset="native", url="https://example.test/native/keyword",
            title="Ordinary native keyword source", body="Ordinary carbon capture baseline.", retrievable=True,
        )
        db.import_batch(ImportBatch(records=[native, keyword]))
        manager.prepare(SENTENCE_PROFILE)
        vector = [1.0] + [0.0] * 1535
        encoded = "[" + ",".join(str(value) for value in vector) + "]"
        with db.connect() as conn:
            for row in conn.execute("SELECT DISTINCT text_hash FROM chunks").fetchall():
                conn.execute("INSERT INTO embeddings(text_hash,model,embedding) VALUES (%s,%s,%s::vector)",
                             (row["text_hash"], EMBEDDING_MODEL, encoded))
        current = manager.status(SENTENCE_PROFILE)
        manager.activate(SENTENCE_PROFILE, expected_source_data_version=current["source_data_version"])
        baseline = native_snapshot(db)
        bodies = ["How about this?", "How % about this?", "How _ about this?", "How about this!"]
        old = [saved_post(110 + i, (body,), "paused_sentence_source_validation")
               for i, body in enumerate(bodies)]
        db.import_batch(ImportBatch(records=old))
        targets = [enabled(record) for record in old]
        expected = {record.record_id: {
            "version_id": source_version_id(record.model_dump(mode="json")), "body_sha256": body_hash(record.body),
            "url": record.url, "dataset": "social",
        } for record in old}
        db.import_batch(ImportBatch(records=targets), expected_current=expected)
        prepared = manager.prepare(SOCIAL_OBSERVATION_PROFILE)
        manager.activate_social_observation_profile(
            expected_source_data_version=prepared["source_data_version"], expected_previous_profile=SENTENCE_PROFILE,
        )
        assert native_snapshot(db, SOCIAL_OBSERVATION_PROFILE) == baseline
        yield {"db": db, "targets": targets, "native": native, "keyword": keyword, "vector": vector,
               "baseline": baseline}


def test_literal_stopword_phrase_is_searchable_with_exact_validated_source(literal_scenario):
    db, record = literal_scenario["db"], literal_scenario["targets"][0]
    with db.connect() as conn:
        row = conn.execute("SELECT search_vector::text AS value FROM chunks WHERE record_id=%s "
                           "AND source_observation_id IS NOT NULL", (record.record_id,)).fetchone()
        assert row["value"] == ""
    found = db.search("How about this?", Filters(dataset="social"))
    assert len(found) == 1 and found[0].record_id == record.record_id
    evidence = found[0]
    source = validated_observations(record.model_dump(mode="json"))[0]
    assert evidence.retrieval_sources == ["literal"]
    assert evidence.source_observation_id == source["source_observation_id"]
    assert evidence.source_version_id == source["source_version_id"]
    assert evidence.source_body_hash == body_hash("How about this?")
    assert evidence.text == source["body"][evidence.start:evidence.end] == "How about this?"
    assert db.validate_evidence(evidence)


@pytest.mark.parametrize("query,index", [("HOW ABOUT THIS?", 0), ("How about this!", 3),
                                          ("How % about this?", 1), ("How _ about this?", 2)])
def test_literal_case_punctuation_and_percent_underscore_are_saved_characters(literal_scenario, query, index):
    db, record = literal_scenario["db"], literal_scenario["targets"][index]
    found = db.search(query, Filters(dataset="social"))
    assert [e.record_id for e in found] == [record.record_id]
    assert all(e.retrieval_sources == ["literal"] and db.validate_evidence(e) for e in found)


@pytest.mark.parametrize("query", ["How % about this?", "How _ about this?", "How about this!", "How * about this?"])
def test_literal_wildcards_and_changed_punctuation_do_not_match_plain_question(literal_scenario, query):
    record = literal_scenario["targets"][0]
    assert literal_scenario["db"].search(query, Filters(dataset="social", record_ids=[record.record_id])) == []


def test_literal_honors_trusted_record_and_dataset_scopes_and_preserves_native(literal_scenario):
    db, target = literal_scenario["db"], literal_scenario["targets"][0]
    all_results = db.search("How about this?", Filters(dataset="all"))
    assert [e.record_id for e in all_results] == [target.record_id]
    assert all(e.dataset == "social" for e in all_results)
    assert db.search("How about this?", Filters(dataset="native")) == []
    assert db.search("How about this?", Filters(dataset="social", record_ids=[literal_scenario["targets"][1].record_id])) == []
    assert db.search("How about this?", Filters(dataset="all", record_ids=[literal_scenario["native"].record_id])) == []
    assert native_snapshot(db, SOCIAL_OBSERVATION_PROFILE) == literal_scenario["baseline"]


def test_literal_is_not_added_when_vector_results_exist(literal_scenario):
    found = literal_scenario["db"].search("How about this?", Filters(dataset="all"), vector=literal_scenario["vector"])
    assert found and all(e.retrieval_sources == ["vector"] for e in found)
    assert literal_scenario["native"].record_id in {e.record_id for e in found}
    assert all(literal_scenario["db"].validate_evidence(e) for e in found)


def test_literal_and_existing_keyword_diagnostics_remain_distinct(literal_scenario):
    db = literal_scenario["db"]
    report = db.search_report("  HOW ABOUT THIS?  ", Filters(dataset="social"))
    assert len(report["evidence"]) == 1
    assert report["diagnostics"]["status"] == "available"
    assert report["diagnostics"]["operator"] == "literal_phrase"
    assert report["diagnostics"]["configuration"] == "exact_saved_text"
    assert report["diagnostics"]["terms"] == ["HOW ABOUT THIS?"]
    assert report["evidence"][0].matched_terms == report["evidence"][0].literal_matched_terms == ["HOW ABOUT THIS?"]
    missing = db.search_report("How * about this?", Filters(dataset="social"))
    assert missing["evidence"] == [] and missing["diagnostics"]["status"] == "unavailable"
    assert missing["diagnostics"]["missing_from_scope"] == []
    lexical = db.search_report("carbon", Filters(dataset="native"))
    assert lexical["diagnostics"]["operator"] == "OR" and lexical["diagnostics"]["configuration"] == "english"
    assert lexical["diagnostics"]["scope_records"] == 2
    assert lexical["diagnostics"]["returned_matched_terms"] == ["carbon"]
    assert [e.record_id for e in lexical["evidence"]] == [literal_scenario["keyword"].record_id]
    assert lexical["evidence"][0].retrieval_sources == ["keyword"]


@pytest.mark.parametrize("query", ["How?", "a b", "how " * 501])
def test_literal_fallback_stays_bounded(literal_scenario, query):
    report = literal_scenario["db"].search_report(query, Filters(dataset="social"))
    assert report["evidence"] == [] and report["diagnostics"]["status"] == "unavailable"


def test_literal_source_payload_drift_is_rejected(literal_scenario):
    db, record = literal_scenario["db"], literal_scenario["targets"][0]
    evidence = db.search("How about this?", Filters(dataset="social", record_ids=[record.record_id]))[0]
    with db.connect() as conn:
        original = conn.execute("SELECT payload FROM record_versions WHERE version_id=%s", (evidence.version_id,)).fetchone()["payload"]
    changed = deepcopy(original)
    changed["title"] += " unauthorized in-place change"
    try:
        with db.connect() as conn:
            conn.execute("UPDATE record_versions SET payload=%s WHERE version_id=%s", (Jsonb(changed), evidence.version_id))
        assert db.validate_evidence(evidence) is False
        with pytest.raises(ValueError, match="enclosing record version"):
            db.search("How about this?", Filters(dataset="social", record_ids=[record.record_id]))
    finally:
        with db.connect() as conn:
            conn.execute("UPDATE record_versions SET payload=%s WHERE version_id=%s", (Jsonb(original), evidence.version_id))
    assert db.validate_evidence(evidence)
