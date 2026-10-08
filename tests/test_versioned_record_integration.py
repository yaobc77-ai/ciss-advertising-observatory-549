"""Opt-in real source reads in one owned, disposable loopback obs_test schema.

OBS_RUN_VERSIONED_RECORD_INTEGRATION=1 enables these synthetic checks. Reuse the
guarded-import target verification, never the shared-table truncation fixture.
No .env, model, API, extension install or service start is used.
"""

import hashlib
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from postgres_fixtures import validate_test_target
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from test_import_preconditions_integration import (
    _public_relations,
    benchmark,
    local_test_setting,
    verified_connection,
)

from observatory.config import Settings
from observatory.db import Database, digest
from observatory.models import Filters, ImportBatch, RecordInput
from observatory.research_tools import ToolCatalog
from observatory.service import Service
from observatory.social_annotations import (
    SCHEME,
    SOCIAL_LABELS,
    STATUS,
    social_state_id,
)

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]
RECEIPT_DIR = ROOT / ".runtime" / "content_publication_20261006" / "sql_check"
CHECKS = {
    "full_filters", "native_social_all_details", "admission_withdrawal",
    "current_version_annotations", "source_privacy_affiliation", "mixed_collection_retrieval",
    "stale_or_corrupt_evidence", "retrieval_ranges_and_hash", "date_basis_and_missing",
}
TABLES = (
    "schema_migrations", "records", "record_versions", "annotations", "chunks",
    "retrieval_profiles", "chunk_profile_membership", "retrieval_state",
    "retrieval_preparations", "retrieval_publications", "embeddings", "imports",
    "usage_ledger", "answer_runs", "generation_outputs", "date_inferences",
    "claims2_taxonomies", "claims2_runs", "claims2_imports", "claims2_results",
    "claims2_reviews", "claims2_retractions",
)
SOURCE_FILES = (
    "src/observatory/db.py", "src/observatory/research_tools.py", "src/observatory/service.py",
    "src/observatory/models.py", "src/observatory/chunking.py", "src/observatory/indexing.py",
    "src/observatory/migrations.py", "src/observatory/knowledge_graph.py",
    "scripts/benchmark_dashboard.py", "scripts/local_postgres.py",
    "tests/test_import_preconditions_integration.py", "tests/test_versioned_record_integration.py",
)


def fingerprints():
    paths = [*SOURCE_FILES, *(path.relative_to(ROOT).as_posix()
                             for path in sorted((ROOT / "src/observatory/migrations").glob("*.sql")))]
    return {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}


class RecordedConnection:
    def __init__(self, connection, receipt):
        self.connection = connection
        self.receipt = receipt

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, params=None):
        result = self.connection.execute(statement, params)
        if isinstance(statement, str) and statement.startswith("SELECT p.*,v.body,v.body_hash"):
            self.receipt["versioned_sql_executions"] += 1
            shape = hashlib.sha256(statement.encode()).hexdigest()
            if shape not in self.receipt["versioned_sql_sha256"]:
                self.receipt["versioned_sql_sha256"].append(shape)
        if isinstance(statement, str) and statement.startswith("SELECT selected.*,v.body,v.body_hash,"):
            self.receipt["content_source_sql_executions"] += 1
            shape = hashlib.sha256(statement.encode()).hexdigest()
            if shape not in self.receipt["content_source_sql_sha256"]:
                self.receipt["content_source_sql_sha256"].append(shape)
        return result


class IsolatedDatabase(Database):
    def __init__(self, setting, schema, receipt):
        super().__init__(setting)
        self.schema = schema
        self.receipt = receipt

    def connect(self, vector=False):
        if vector:
            raise ValueError("These source checks do not use vector search")
        return RecordedConnection(verified_connection(self.url, self.schema), self.receipt)

    def checked(self, name, **details):
        self.receipt["checks"].append({"name": name, "status": "passed", **details})


def directory_metadata(conn):
    # PostgreSQL catalogs only: no shared table contents or filesystem inspection.
    namespaces = conn.execute("SELECT oid,nspname FROM pg_namespace ORDER BY oid").fetchall()
    return {"public_relations": _public_relations(conn), "namespaces": namespaces}


def exact_test_setting():
    # Freeze the numeric address before connecting. libpq/psycopg can otherwise
    # fill an omitted hostaddr from PGHOSTADDR before post-connect verification.
    setting = local_test_setting()
    target = benchmark.validate_target(setting)
    return make_conninfo(setting, hostaddr=target["host"])


@pytest.fixture(scope="module")
def isolated_db():
    if os.environ.get("OBS_RUN_VERSIONED_RECORD_INTEGRATION") != "1":
        pytest.skip("Explicit versioned-source integration opt-in is absent")
    schema = f"obs_benchmark_{uuid4().hex}"
    receipt = {
        "schema_version": "native-content-source-sql-integration-v1", "status": "running",
        "database": "obs_test", "host": "numeric_loopback", "schema": schema,
        "started_at": datetime.now(UTC).isoformat(), "synthetic": True,
        "source_sha256": fingerprints(), "checks": [],
        "schema_created": False, "schema_dropped": False,
        "shared_business_rows_read_or_written": False,
        "model_calls": 0, "external_api_calls": 0, "extension_installs": 0,
        "versioned_sql_executions": 0, "versioned_sql_sha256": [],
        "content_source_sql_executions": 0, "content_source_sql_sha256": [],
        "limits": ["Synthetic records in a disposable local schema.",
                   "Catalog equality covers directory metadata, not shared table data.",
                   "Content-source SQL coverage does not validate reviewed membership or complete-list accuracy.",
                   "Does not validate real corpus admission, duplicate policy, model semantics or deployment."],
    }
    setting, created, before = None, False, None
    try:
        setting = exact_test_setting()
        target = benchmark.validate_target(setting)
        validate_test_target(setting)
        if target.get("hostaddr") != target["host"]:
            raise ValueError("Explicit numeric test address is not fixed before connection")
        receipt["database"] = target["dbname"]
        receipt["connection_target"] = {key: target[key] for key in ("host", "hostaddr", "dbname")}
        with verified_connection(setting) as conn:
            before = directory_metadata(conn)
            receipt["catalog_before"] = before
            if not conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone():
                raise RuntimeError("ExistingTestVectorExtensionRequired")
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        created = True
        receipt["schema_created"] = True
        db = IsolatedDatabase(setting, schema, receipt)
        db.initialize()
        with db.connect() as conn:
            for table in TABLES:
                resolved = conn.execute("SELECT n.nspname AS schema FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=to_regclass(%s)",
                    (table,)).fetchone()
                if not resolved or resolved["schema"] != schema:
                    raise ValueError("Business relation is outside the owned schema")
            receipt["server_version"] = conn.execute("SHOW server_version").fetchone()["server_version"]
        # Keep both collections available independently of individual test order.
        db.import_batch(ImportBatch(records=[synthetic("seed-native", "native"), synthetic("seed-social")]))
        yield db
        receipt["status"] = "completed" if {item["name"] for item in receipt["checks"]} == CHECKS else "incomplete"
    except Exception as exc:
        receipt["status"] = "failed"
        receipt["failure_type"] = type(exc).__name__
        raise RuntimeError(f"Isolated source verification failed: {type(exc).__name__}") from None
    finally:
        if created:
            try:
                with verified_connection(setting) as conn:
                    owned = conn.execute("SELECT nspowner=current_user::regrole AS owned "
                        "FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone()
                    if not benchmark.SCHEMA_PATTERN.fullmatch(schema) or not owned or owned["owned"] is not True:
                        raise ValueError("Disposable schema ownership is not verified")
                    conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
                    after = directory_metadata(conn)
                    receipt["catalog_after"] = after
                    receipt["public_catalog_unchanged"] = after["public_relations"] == before["public_relations"]
                    receipt["namespace_directory_unchanged"] = after["namespaces"] == before["namespaces"]
                    if conn.execute("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone():
                        raise ValueError("Disposable schema still exists")
                receipt["schema_dropped"] = True
            except Exception as exc:
                receipt["status"] = "cleanup_failed"
                receipt["cleanup_failure_type"] = type(exc).__name__
        receipt["source_sha256_after"] = fingerprints()
        receipt["source_unchanged"] = receipt["source_sha256"] == receipt["source_sha256_after"]
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
        (RECEIPT_DIR / f"{schema}.receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if created and not receipt["schema_dropped"]:
            raise RuntimeError("Isolated schema cleanup failed; inspect the private receipt")
        if created and (not receipt.get("public_catalog_unchanged") or not receipt.get("namespace_directory_unchanged")):
            raise RuntimeError("Test database directory metadata changed outside the owned schema")


def synthetic(record_id, dataset="social", **changes):
    fields = {
        "record_id": record_id, "dataset": dataset, "url": f"https://example.invalid/{record_id}",
        "title": "Fabricated source for integration checks", "publisher": "Fixture outlet",
        "sponsor": "Fixture company", "platform": "Twitter" if dataset == "social" else "",
        "account": "Fixture account" if dataset == "social" else "", "keyword": "fixture-energy",
        "published_at": date(2020, 3, 1), "body": "甲😀 Carbon capture lowers emissions in this fabricated source.",
        "retrievable": True, "archive_url": f"https://archive.example.invalid/{record_id}",
        "raw": {"sponsor_basis": "company_affiliation_not_verified_paid_sponsor",
                "secret": "private-marker", "path": "C:/private/source"},
        "provenance": [{"path": "C:/private/input"}], "disclosure": "PRIVATE-DISCLOSURE",
    }
    return RecordInput(**(fields | changes))


def catalog(db, filters=None, *, links=True):
    settings = Settings(show_source_links=links, web_search_enabled=False)
    return ToolCatalog(Service(settings, db=db, rag=object()), filters or Filters(dataset="social"))


def test_real_current_source_all_filters(isolated_db, monkeypatch):
    db = isolated_db
    rec = synthetic("filters")
    values = dict.fromkeys(SOCIAL_LABELS, False)
    values["green_binary"] = True
    rec.annotations = [{"version": SCHEME, "status": STATUS,
        "basis": "supplied_source_post_id_and_exact_body",
        "taxonomy_mapping": "source_keys_preserved_no_native_label_mapping",
        "values": values, "labels": ["green_binary"], "body_sha256": digest(rec.body),
        "source_sha256": "a" * 64, "source_row": 1}]
    db.import_batch(ImportBatch(records=[rec]))
    base = Filters(dataset="social", sponsors=[rec.sponsor], publishers=[rec.publisher],
        platforms=[rec.platform], keywords=[rec.keyword],
        labels=[social_state_id("green_binary", "source_true")], record_ids=[rec.record_id],
        date_from=rec.published_at, date_to=rec.published_at, include_unknown_dates=False, date_presence="known")
    original = base.model_copy(deep=True)
    row = db.versioned_record(base, rec.record_id)
    assert row["body"] == rec.body and row["account"] == rec.account
    assert base == original
    assert catalog(db, base).call("get_record", {"record_id": rec.record_id})["status"] == "ok"
    rejected = [
        {"dataset": "native", "labels": []}, {"sponsors": ["other"]}, {"publishers": ["other"]},
        {"platforms": ["Instagram"]}, {"keywords": ["other"]},
        {"labels": [social_state_id("green_binary", "source_false")]},
        {"record_ids": ["other"]}, {"date_from": date(2020, 3, 2)},
        {"date_to": date(2020, 2, 29)}, {"date_presence": "missing"},
    ]
    for changes in rejected:
        assert db.versioned_record(base.model_copy(update=changes), rec.record_id) is None
    with pytest.raises(ValueError, match="require the social collection"):
        db.versioned_record(base.model_copy(update={"dataset": "native"}), rec.record_id)
    # Native content membership needs the whole countable denominator, including
    # bodies that cannot be searched. This is a source read, not classification.
    body = "甲😀 excluded header.\nCarbon capture is the retained source.\nExcluded tail."
    start, end = body.index("Carbon"), body.index("\nExcluded tail")
    annotation = [{"version": "claims-calibrated", "labels": ["content_filter"]}]
    native = synthetic("content-filter", "native", platform="Fixture medium", body=body,
                       retrieval_ranges=[(start, end)], annotations=annotation)
    common = {"platform": native.platform, "annotations": annotation}
    unsearchable = synthetic("content-unsearchable", "native", retrievable=False, **common)
    missing = synthetic("content-missing", "native", body="", retrievable=False, **common)
    end_limited = synthetic("content-end", "native", retrieval_end=12, **common)
    pending = synthetic("content-pending", "native", countable=False, retrievable=False, **common)
    withdrawn = synthetic("content-withdrawn", "native", **common)
    undated = synthetic("content-undated", "native", published_at=None, retrievable=False, **common)
    social = synthetic("content-social", **common)
    selected = [native, unsearchable, missing, end_limited, pending, withdrawn, undated, social]
    db.import_batch(ImportBatch(records=selected))
    with db.connect() as conn:
        conn.execute("UPDATE records SET active=false WHERE record_id=%s", (withdrawn.record_id,))
    content_filters = Filters(dataset="native", sponsors=[native.sponsor], publishers=[native.publisher],
        platforms=[native.platform], keywords=[native.keyword], labels=["content_filter"],
        record_ids=[item.record_id for item in selected], date_from=native.published_at,
        date_to=native.published_at, include_unknown_dates=False, date_presence="known")
    unchanged = content_filters.model_copy(deep=True)
    executions = db.receipt["content_source_sql_executions"]
    rows = db.content_match_sources(content_filters)
    assert db.receipt["content_source_sql_executions"] == executions + 1
    assert content_filters == unchanged
    expected = {item.record_id: item for item in (native, unsearchable, missing, end_limited)}
    assert [row["record_id"] for row in rows] == sorted(expected)
    for row in rows:
        original_record = expected[row["record_id"]]
        serialized = json.dumps(original_record.model_dump(mode="json"), ensure_ascii=False,
                                sort_keys=True, separators=(",", ":"))
        assert row["dataset"] == "native" and row["body"] == original_record.body
        assert row["body_hash"] == digest(original_record.body)
        assert row["version_id"] == digest(serialized)
        assert row["retrievable"] is original_record.retrievable
        assert row["date"] == row["source_date"] == row["effective_date"] == "2020-03-01"
        assert row["date_basis"] == "source"
        assert not {"raw", "provenance", "disclosure", "annotations"}.intersection(row)
    source = next(row for row in rows if row["record_id"] == native.record_id)
    assert source["body"][start:end] == "Carbon capture is the retained source."
    assert source["retrieval_ranges"] == [[start, end]] and source["retrieval_end"] is None
    assert next(row for row in rows if row["record_id"] == end_limited.record_id)["retrieval_end"] == 12
    rendered = json.dumps(rows, ensure_ascii=False)
    assert all(private not in rendered for private in ("private-marker", "C:/private", "PRIVATE-DISCLOSURE"))
    rejected_content = [
        {"sponsors": ["other"]}, {"publishers": ["other"]}, {"platforms": ["other"]},
        {"keywords": ["other"]}, {"labels": ["other"]}, {"record_ids": ["absent-content"]},
        {"date_from": date(2020, 3, 2)}, {"date_to": date(2020, 2, 29)}, {"date_presence": "missing"},
    ]
    for changes in rejected_content:
        assert db.content_match_sources(content_filters.model_copy(update=changes)) == []
    for dataset in ("social", "all"):
        executions = db.receipt["content_source_sql_executions"]
        def forbidden_connection(*args, **kwargs):
            raise AssertionError("Native content request must reject before connecting")
        with monkeypatch.context() as patch:
            patch.setattr(db, "connect", forbidden_connection)
            with pytest.raises(ValueError, match="native records"):
                db.content_match_sources(content_filters.model_copy(update={"dataset": dataset}))
        assert db.receipt["content_source_sql_executions"] == executions
    with_unknown = content_filters.model_copy(update={"include_unknown_dates": True, "date_presence": "any"})
    unknown_rows = db.content_match_sources(with_unknown)
    assert {row["record_id"] for row in unknown_rows} == set(expected) | {undated.record_id}
    unknown_source = next(row for row in unknown_rows if row["record_id"] == undated.record_id)
    assert unknown_source["date"] is None and unknown_source["date_basis"] == "missing"
    assert {row["record_id"] for row in db.content_match_sources(
        with_unknown.model_copy(update={"include_unknown_dates": False}))} == set(expected)
    missing_date_scope = content_filters.model_copy(update={"date_from": None, "date_to": None,
        "date_presence": "missing", "include_unknown_dates": True})
    assert [row["record_id"] for row in db.content_match_sources(missing_date_scope)] == [undated.record_id]
    with db.connect() as conn:
        conn.execute("INSERT INTO date_inferences(inference_id,record_id,version_id,method,tier,inferred_date,precision,evidence) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            ("synthetic-content-date", undated.record_id, unknown_source["version_id"], "url_path", "A",
             native.published_at, "day", Jsonb({"synthetic": True})))
    supplemented = db.content_match_sources(content_filters.model_copy(update={"include_inferred_dates": True}))
    assert {row["record_id"] for row in supplemented} == set(expected) | {undated.record_id}
    inferred = next(row for row in supplemented if row["record_id"] == undated.record_id)
    assert inferred["date"] is None and inferred["source_date"] is None
    assert inferred["effective_date"] == "2020-03-01" and inferred["date_basis"] == "inferred:url_path"
    assert {row["record_id"] for row in db.content_match_sources(content_filters)} == set(expected)
    # A newly current payload replaces, rather than duplicates, the source row.
    updated_body = body + " More retained text."
    updated = native.model_copy(update={"title": "Current native source", "body": updated_body,
        "retrieval_ranges": [(0, len(updated_body))],
        "annotations": [{"version": "claims-calibrated", "labels": ["content_current"]}]})
    db.import_batch(ImportBatch(records=[updated]))
    assert {row["record_id"] for row in db.content_match_sources(content_filters)} == set(expected) - {native.record_id}
    current_rows = db.content_match_sources(content_filters.model_copy(update={"labels": ["content_current"]}))
    assert len(current_rows) == 1 and current_rows[0]["record_id"] == native.record_id
    current = current_rows[0]
    assert current["version_id"] != source["version_id"]
    assert current["body"] == updated.body and current["body_hash"] == digest(updated.body)
    assert current["retrieval_ranges"] == [[0, len(updated_body)]] and current["labels"] == ["content_current"]
    assert {row["record_id"] for row in db.content_match_sources(
        content_filters.model_copy(update={"labels": ["content_filter", "content_current"]}))} == set(expected)
    db.checked("full_filters", rejected_filter_variants=len(rejected),
        content_source_rejected_filter_variants=len(rejected_content), native_content_denominator=True,
        unsearchable_and_missing_in_scope=True, source_rows_current_and_unicode_bound=True,
        non_native_refused_before_read=True, source_private_fields_omitted=True,
        source_date_basis_preserved=True, current_ranges_and_labels_isolated=True)


def test_real_native_social_and_all_exact_detail(isolated_db):
    db = isolated_db
    native, social = synthetic("detail-native", "native"), synthetic("detail-social")
    db.import_batch(ImportBatch(records=[native, social]))
    for rec in (native, social):
        for dataset in (rec.dataset, "all"):
            tools = catalog(db, Filters(dataset=dataset, record_ids=[rec.record_id]))
            result = tools.call("get_record", {"record_id": rec.record_id, "body_start": 1, "body_limit": 7})
            assert result["status"] == "ok" and result["body"]["text"] == rec.body[1:8]
            assert result["source_refs"][0]["body_hash"] == digest(rec.body)
            assert result["record"]["dataset"] == rec.dataset
            assert tools.call("get_record", {"record_id": rec.record_id, "body_start": 999})["status"] == "clarify"
    assert db.versioned_record(Filters(dataset="social"), native.record_id) is None
    assert db.versioned_record(Filters(), social.record_id) is None
    graph = catalog(db, Filters()).call("get_graph_neighborhood", {"record_id": native.record_id})
    assert graph["status"] == "ok"
    assert catalog(db).call("get_graph_neighborhood", {})["status"] == "unavailable"
    db.checked("native_social_all_details", unicode_offsets=True, native_graph_preserved=True)


def test_real_pending_and_withdrawn_records_are_unreadable(isolated_db):
    db = isolated_db
    for dataset in ("native", "social"):
        pending = synthetic(f"pending-{dataset}", dataset, countable=False, retrievable=False)
        withdrawn = synthetic(f"withdrawn-{dataset}", dataset)
        db.import_batch(ImportBatch(records=[pending, withdrawn]))
        with db.connect() as conn:
            conn.execute("UPDATE records SET active=false WHERE record_id=%s", (withdrawn.record_id,))
        for rec in (pending, withdrawn):
            assert db.versioned_record(Filters(dataset="all"), rec.record_id) is None
            for tool in ("get_record", "get_record_sources"):
                assert catalog(db, Filters(dataset=dataset)).call(tool, {"record_id": rec.record_id})["status"] == "clarify"
    db.checked("admission_withdrawal", both_collections=True)


def test_real_only_current_annotation_and_body_are_read(isolated_db):
    db = isolated_db
    old = synthetic("history", annotations=[{"version": "old-fixture", "labels": ["old_label"]}])
    db.import_batch(ImportBatch(records=[old]))
    previous = db.versioned_record(Filters(dataset="social"), old.record_id)
    new = synthetic("history", body="A different fabricated emissions source.",
                    annotations=[{"version": "claims-social-export-v1", "labels": ["green_binary"],
                                  "status": "historical_automatic_unverified"}])
    db.import_batch(ImportBatch(records=[new]))
    current = db.versioned_record(Filters(dataset="social"), old.record_id)
    assert current["version_id"] != previous["version_id"] and current["body"] == new.body
    assert [a["payload"]["version"] for a in current["annotations"]] == ["claims-social-export-v1"]
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM record_versions WHERE record_id=%s",
                            (old.record_id,)).fetchone()["n"] == 2
    assert db.versioned_record(Filters(dataset="social", labels=[social_state_id("green_binary", "source_true")]), old.record_id) is None
    db.checked("current_version_annotations", prior_version_retained=True, history_not_calibrated=True)


def test_real_public_sources_preserve_affiliation_and_hide_private_fields(isolated_db):
    db = isolated_db
    rec = synthetic("sources", annotations=[{"version": "claims-social-export-v1",
        "labels": ["green_binary"], "values": {"green_binary": True}, "explanations": "private-marker"}])
    db.import_batch(ImportBatch(records=[rec]))
    row = db.versioned_record(Filters(dataset="social"), rec.record_id)
    assert not {"raw", "provenance", "disclosure"}.intersection(row)
    for links in (True, False):
        result = catalog(db, links=links).call("get_record_sources", {"record_id": rec.record_id})
        assert result["status"] == "ok" and len(result["source_artifacts"]) == (2 if links else 0)
        assert result["record"]["company_affiliation"] == rec.sponsor
        assert result["record"]["sponsor_basis"] == "company_affiliation_not_verified_paid_sponsor"
        assert result["record"]["url"] == (rec.url if links else "")
        assert result["record"]["archive_url"] == (rec.archive_url if links else "")
        assert result["annotations"] == [] and result["claims_status"] == "historical_unverified"
        rendered = json.dumps(result)
        for private in ("private-marker", "C:/private", "PRIVATE-DISCLOSURE"):
            assert private not in rendered, f"Private sentinel leaked: {private}"
        historical = result["social_historical_annotation"]
        assert historical["validation_state"] == "invalid"
        assert len(historical["values"]) == len(SOCIAL_LABELS)
        assert all(item["state"] == "unknown" and item["value"] is None for item in historical["values"])
        if not links:
            assert "example.invalid" not in rendered
    unsafe = synthetic("unsafe-sources", url="https://user:secret@example.invalid/source",
                       archive_url="file:///C:/private/source")
    db.import_batch(ImportBatch(records=[unsafe]))
    result = catalog(db).call("get_record_sources", {"record_id": unsafe.record_id})
    assert result["source_artifacts"] == [] and result["record"]["url"] == result["record"]["archive_url"] == ""
    db.checked("source_privacy_affiliation", hidden_links=True, no_paid_sponsor_inference=True)


def test_real_mixed_collection_retrieval_checks_current_source(isolated_db):
    db = isolated_db
    native, social = synthetic("search-native", "native"), synthetic("search-social")
    db.import_batch(ImportBatch(records=[native, social]))
    for dataset, ids in (("native", [native.record_id]), ("social", [social.record_id]),
                         ("all", [native.record_id, social.record_id])):
        result = catalog(db, Filters(dataset=dataset, record_ids=ids)).call("search_records", {"query": "emissions"})
        assert result["status"] == "ok" and result["rejected_evidence"] == 0
        assert {item["record_id"] for item in result["evidence"]} == set(ids)
        assert all(ref["verification_status"] == "exact_character_match" for ref in result["source_refs"])
    db.checked("mixed_collection_retrieval", actual_keyword_search=True, vector_or_model_calls=0)


def test_real_mcp_rejects_old_version_and_injected_bad_evidence(isolated_db, monkeypatch):
    db = isolated_db
    rec = synthetic("stale-search")
    db.import_batch(ImportBatch(records=[rec]))
    filters = Filters(dataset="social", record_ids=[rec.record_id])
    captured = db.search_report("emissions", filters)
    assert len(captured["evidence"]) == 1
    old = captured["evidence"][0]
    # Same body but different payload: old-version failure is independent of text/hash.
    db.import_batch(ImportBatch(records=[synthetic(rec.record_id, title="A new synthetic title")]))
    assert db.validate_evidence(old)  # historical citations are retained by the separate legacy validator
    with monkeypatch.context() as patch:
        patch.setattr(db, "search_report", lambda *a, **kw: captured)
        result = catalog(db, filters).call("search_records", {"query": "emissions"})
        assert result["evidence"] == [] and result["rejected_evidence"] == 1
    current = db.search_report("emissions", filters)
    variants = ({"dataset": "native"}, {"version_id": old.version_id}, {"start": -1},
                {"end": 999}, {"text": "fabricated wrong quotation"}, {"record_id": "seed-native"})
    for damage in variants:
        damaged = {**current, "evidence": [current["evidence"][0].model_copy(update=damage)]}
        with monkeypatch.context() as patch:
            patch.setattr(db, "search_report", lambda *a, **kw: damaged)
            result = catalog(db, filters).call("search_records", {"query": "emissions"})
        assert result["evidence"] == [] and result["rejected_evidence"] == 1
    db.checked("stale_or_corrupt_evidence", actual_current_SQL=True, injected_variants=len(variants))


def test_real_retrieval_interval_and_body_hash_guard(isolated_db, monkeypatch):
    db = isolated_db
    body = "Excluded carbon capture header.\nApproved emissions evidence.\nExcluded tail."
    start, end = body.index("Approved"), body.index("\nExcluded tail")
    rec = synthetic("ranges", body=body, retrieval_ranges=[(start, end)])
    db.import_batch(ImportBatch(records=[rec]))
    filters = Filters(dataset="social", record_ids=[rec.record_id])
    captured = db.search_report("emissions", filters)
    evidence = captured["evidence"][0]
    assert (evidence.start, evidence.end) == (start, end)
    assert catalog(db, filters).call("search_records", {"query": "emissions"})["rejected_evidence"] == 0
    outside = evidence.model_copy(update={"start": 0, "end": start, "text": body[:start]})
    with monkeypatch.context() as patch:
        patch.setattr(db, "search_report", lambda *a, **kw: {**captured, "evidence": [outside]})
        assert catalog(db, filters).call("search_records", {"query": "emissions"})["rejected_evidence"] == 1
    row = db.versioned_record(filters, rec.record_id)
    with db.connect() as conn:
        conn.execute("UPDATE record_versions SET body_hash=%s WHERE version_id=%s", ("0" * 64, row["version_id"]))
    try:
        assert catalog(db, filters).call("get_record", {"record_id": rec.record_id})["status"] == "unavailable"
        result = catalog(db, filters).call("search_records", {"query": "emissions"})
        assert result["evidence"] == [] and result["rejected_evidence"] == 1
    finally:
        with db.connect() as conn:
            conn.execute("UPDATE record_versions SET body_hash=%s WHERE version_id=%s", (digest(body), row["version_id"]))
    db.checked("retrieval_ranges_and_hash", excluded_range_rejected=True, body_hash_corruption_rejected=True)


def test_real_missing_and_inferred_dates_keep_trusted_basis(isolated_db):
    db = isolated_db
    rec = synthetic("missing-date", published_at=None)
    db.import_batch(ImportBatch(records=[rec]))
    filters = Filters(dataset="social", record_ids=[rec.record_id], date_from=date(2020, 1, 1),
                      date_to=date(2020, 12, 31), include_unknown_dates=False)
    assert db.versioned_record(filters, rec.record_id) is None
    row = db.versioned_record(Filters(dataset="social"), rec.record_id)
    with db.connect() as conn:
        conn.execute("INSERT INTO date_inferences(inference_id,record_id,version_id,method,tier,inferred_date,precision,evidence) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            ("synthetic-date", rec.record_id, row["version_id"], "url_path", "A", date(2020, 3, 1), "day", Jsonb({"synthetic": True})))
    supplemented = filters.model_copy(update={"include_inferred_dates": True})
    current = db.versioned_record(supplemented, rec.record_id)
    assert current["date"] is None and current["effective_date"] == "2020-03-01"
    assert current["date_basis"] == "inferred:url_path"
    assert catalog(db, supplemented).call("get_record", {"record_id": rec.record_id})["status"] == "ok"
    assert db.versioned_record(filters, rec.record_id) is None
    assert db.versioned_record(Filters(dataset="social", date_presence="missing"), rec.record_id) is not None
    assert db.versioned_record(Filters(dataset="social", include_inferred_dates=True, date_presence="missing"), rec.record_id) is None
    db.checked("date_basis_and_missing", source_date_unchanged=True, supplemented_scope_explicit=True)
