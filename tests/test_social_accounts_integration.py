"""Opt-in account reads against synthetic rows in one owned loopback schema.

No shared business rows, .env, models, external APIs, extension installation or
service lifecycle operations are used. This does not admit customer social data.
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
from pydantic import ValidationError
from test_versioned_record_integration import (
    TABLES,
    benchmark,
    directory_metadata,
    exact_test_setting,
    verified_connection,
)

from observatory.config import Settings
from observatory.db import Database, digest
from observatory.models import Filters, ImportBatch, RecordInput
from observatory.research_tools import ToolCatalog
from observatory.service import Service

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]
RECEIPT_DIR = ROOT / ".runtime" / "social_accounts_20261006" / "sql_check"
CHECKS = {"parameterized_account_scope", "complete_account_aggregates", "current_version_and_admission",
          "keyword_account_scope", "mcp_account_scope"}
SOURCE_FILES = (
    "src/observatory/models.py", "src/observatory/db.py", "src/observatory/service.py",
    "src/observatory/research_tools.py", "src/observatory/structured_queries.py",
    "src/observatory/chunking.py", "src/observatory/indexing.py", "src/observatory/migrations.py",
    "scripts/benchmark_dashboard.py", "scripts/local_postgres.py",
    "tests/test_import_preconditions_integration.py", "tests/test_versioned_record_integration.py",
    "tests/test_social_accounts_integration.py",
)
INJECTION_NAME = "x' OR true --"
FIXTURE_IDS = (
    "acct-alpha-one", "acct-alpha-two", "acct-alpha-instagram", "acct-beta", "acct-blank",
    "acct-undated", "acct-unsearchable", "acct-injection", "acct-pending", "acct-inactive",
    "acct-native",
)
ADMITTED_SOCIAL = set(FIXTURE_IDS) - {"acct-pending", "acct-inactive", "acct-native"}
ALPHA_SOCIAL = {"acct-alpha-one", "acct-alpha-two", "acct-alpha-instagram", "acct-undated", "acct-unsearchable"}


def fingerprints():
    migrations = [path.relative_to(ROOT).as_posix()
                  for path in sorted((ROOT / "src/observatory/migrations").glob("*.sql"))]
    return {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in (*SOURCE_FILES, *migrations)}


class RecordedConnection:
    def __init__(self, connection, receipt):
        self.connection, self.receipt = connection, receipt

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, params=None):
        cursor = self.connection.execute(statement, params)
        if isinstance(statement, str) and statement.lstrip().upper().startswith(("SELECT", "WITH")):
            self.receipt["read_statement_executions"] += 1
            shape = hashlib.sha256(statement.encode("utf-8")).hexdigest()
            if shape not in self.receipt["read_statement_sha256"]:
                self.receipt["read_statement_sha256"].append(shape)
        return cursor


class IsolatedDatabase(Database):
    def __init__(self, setting, schema, receipt):
        super().__init__(setting)
        self.schema, self.receipt = schema, receipt

    def connect(self, vector=False):
        if vector:
            raise ValueError("Account integration checks use keyword search only")
        return RecordedConnection(verified_connection(self.url, self.schema), self.receipt)

    def checked(self, name, **details):
        self.receipt["checks"].append({"name": name, "status": "passed", **details})


def post(record_id, **changes):
    fields = {
        "record_id": record_id, "dataset": "social", "url": f"https://example.invalid/{record_id}",
        "title": "Fabricated account integration post", "publisher": "Fixture publisher",
        "sponsor": "Company One", "platform": "Twitter", "account": "Alpha", "keyword": "fixture",
        "published_at": date(2020, 3, 1), "body": "甲😀 Synthetic carbon beacon in this fabricated post.",
        "retrievable": True, "raw": {"sponsor_basis": "company_affiliation_not_verified_paid_sponsor",
                                      "private": "PRIVATE-ACCOUNT-FIXTURE"},
    }
    return RecordInput(**(fields | changes))


def fixture_records():
    return [
        post("acct-alpha-one"), post("acct-alpha-two", published_at=date(2020, 3, 2)),
        post("acct-alpha-instagram", platform="Instagram"), post("acct-beta", account="Beta", sponsor="Company Two"),
        post("acct-blank", account="", body="", retrievable=False),
        post("acct-undated", published_at=None), post("acct-unsearchable", retrievable=False),
        post("acct-injection", account=INJECTION_NAME, sponsor="Injection Company"),
        post("acct-pending", account="Pending", countable=False, retrievable=False),
        post("acct-inactive", account="Inactive"), post("acct-native", dataset="native", platform=""),
    ]


@pytest.fixture(scope="module")
def isolated_db():
    if os.environ.get("OBS_RUN_SOCIAL_ACCOUNTS_INTEGRATION") != "1":
        pytest.skip("Explicit social-account integration opt-in is absent")
    schema = f"obs_benchmark_{uuid4().hex}"
    receipt = {
        "schema_version": "social-account-sql-integration-v1", "status": "running",
        "schema": schema, "database": "obs_test", "synthetic": True,
        "started_at": datetime.now(UTC).isoformat(), "source_sha256": fingerprints(),
        "checks": [], "schema_created": False, "schema_dropped": False,
        "shared_business_rows_read_or_written": False, "model_calls": 0, "external_api_calls": 0,
        "extension_installs": 0, "read_statement_executions": 0, "read_statement_sha256": [],
        "limits": ["Synthetic account names are display categories, not unique cross-platform identities.",
                   "Only owned-schema SQL and tool scope are checked; no real social admission or model semantics.",
                   "Catalog equality covers directory metadata, not shared table data."],
    }
    setting, created, before = None, False, None
    try:
        setting = exact_test_setting()
        target = benchmark.validate_target(setting)
        validate_test_target(setting)
        if target.get("hostaddr") != target["host"]:
            raise ValueError("Exact numeric loopback test target is not fixed")
        receipt["database"] = target["dbname"]
        receipt["connection_target"] = {key: target[key] for key in ("host", "hostaddr", "dbname")}
        with verified_connection(setting) as conn:
            before = directory_metadata(conn)
            receipt["catalog_before"] = before
            if not conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone():
                raise RuntimeError("ExistingTestVectorExtensionRequired")
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        created = receipt["schema_created"] = True
        db = IsolatedDatabase(setting, schema, receipt)
        db.initialize()
        with db.connect() as conn:
            for table in TABLES:
                resolved = conn.execute("SELECT n.nspname AS schema FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=to_regclass(%s)", (table,)).fetchone()
                if not resolved or resolved["schema"] != schema:
                    raise ValueError("Business relation is outside the owned schema")
            receipt["server_version"] = conn.execute("SHOW server_version").fetchone()["server_version"]
        imported = db.import_batch(ImportBatch(records=fixture_records()))
        assert imported["new_versions"] == len(FIXTURE_IDS)
        with db.connect() as conn:
            conn.execute("UPDATE records SET active=false WHERE record_id=%s", ("acct-inactive",))
        yield db
        receipt["status"] = "completed" if {entry["name"] for entry in receipt["checks"]} == CHECKS else "incomplete"
    except Exception as exc:
        receipt["status"] = "failed"
        receipt["failure_type"] = type(exc).__name__
        raise RuntimeError(f"Isolated account verification failed: {type(exc).__name__}") from None
    finally:
        if created:
            try:
                with verified_connection(setting) as conn:
                    owned = conn.execute("SELECT nspowner=current_user::regrole AS owned FROM pg_namespace WHERE nspname=%s", (schema,)).fetchone()
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
        (RECEIPT_DIR / f"{schema}.receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if created and not receipt["schema_dropped"]:
            raise RuntimeError("Isolated schema cleanup failed; inspect the private receipt")
        if created and (not receipt.get("public_catalog_unchanged") or not receipt.get("namespace_directory_unchanged")):
            raise RuntimeError("Test database directory metadata changed outside the owned schema")


def scoped(**changes):
    return Filters(**({"dataset": "social", "record_ids": list(FIXTURE_IDS)} | changes))


def members(db, filters):
    return {row["record_id"] for row in db.public_rows(filters)}


def catalog(db, filters):
    return ToolCatalog(Service(Settings(show_source_links=False, web_search_enabled=False), db=db, rag=object()), filters)


def test_real_parameterized_account_and_joint_scope(isolated_db):
    db = isolated_db
    filters = scoped(accounts=[INJECTION_NAME])
    statement, params = db.where(filters)
    assert INJECTION_NAME not in statement and [INJECTION_NAME] in params and "account" in statement
    assert members(db, filters) == {"acct-injection"}
    assert members(db, scoped(accounts=["Alpha"])) == ALPHA_SOCIAL
    assert members(db, scoped(accounts=["Alpha"], platforms=["Instagram"])) == {"acct-alpha-instagram"}
    base = scoped(accounts=["Alpha"], sponsors=["Company One"], platforms=["Twitter"],
                  date_from=date(2020, 3, 1), date_to=date(2020, 3, 1), include_unknown_dates=False)
    original = base.model_copy(deep=True)
    assert members(db, base) == {"acct-alpha-one", "acct-unsearchable"}
    assert base == original
    assert members(db, base.model_copy(update={"include_unknown_dates": True})) == {
        "acct-alpha-one", "acct-unsearchable", "acct-undated"}
    assert members(db, scoped(accounts=["(Unknown)"])) == {"acct-blank"}
    assert members(db, scoped(accounts=["Alpha"], date_presence="missing")) == {"acct-undated"}
    assert members(db, scoped(accounts=["Alpha"], sponsors=["Company Two"])) == set()
    for dataset in ("native", "all"):
        with pytest.raises(ValidationError):
            Filters(dataset=dataset, accounts=["Alpha"])
        # model_copy is intentionally unvalidated: SQL must still reject it.
        forged = Filters(dataset=dataset).model_copy(update={"accounts": ["Alpha"]})
        with pytest.raises(ValueError):
            db.where(forged)
    db.checked("parameterized_account_scope", parameterized=True, native_all_refused=True,
               same_name_platforms_separated=True, unknown_and_dates_checked=True)


def test_real_complete_account_facets_and_dashboard(isolated_db):
    db = isolated_db
    assert db.facets("social")["accounts"] == ["(Unknown)", "Alpha", "Beta", INJECTION_NAME]
    assert db.facets("native")["accounts"] == []
    result = db.dashboard(scoped(), limit=1)
    assert result["page"]["total"] == len(ADMITTED_SOCIAL) == 8
    assert len(result["page"]["rows"]) == 1
    groups = result["stats"]["accounts"]
    assert {item["name"]: item["count"] for item in groups} == {"Alpha": 5, "Beta": 1, "(Unknown)": 1, INJECTION_NAME: 1}
    assert sum(item["count"] for item in groups) == 8
    assert sum(item["percent"] for item in groups) == pytest.approx(100)
    assert result["stats"]["unknown_dates"] == 1 and result["stats"]["retrievable"] == 6
    assert db.dashboard(Filters(dataset="native"))["stats"]["accounts"] == []
    selected = db.dashboard(scoped(accounts=["Alpha"], platforms=["Twitter"]), limit=1)
    assert selected["stats"]["accounts"] == [{"name": "Alpha", "count": 4, "percent": 100.0}]
    assert selected["page"]["total"] == 4
    db.checked("complete_account_aggregates", all_groups_not_page_sample=True, countable_unsearchable_counted=True,
               native_accounts_empty=True, social_total=8, account_groups=4)


def test_real_current_account_version_and_unadmitted_records(isolated_db):
    db = isolated_db
    base = scoped(accounts=["Alpha"])
    assert db.versioned_record(base, "acct-alpha-one")["account"] == "Alpha"
    assert db.versioned_record(base, "acct-beta") is None
    assert db.versioned_record(scoped(accounts=["Pending"]), "acct-pending") is None
    assert db.versioned_record(scoped(accounts=["Inactive"]), "acct-inactive") is None
    identifier = "acct-versioned"
    old = post(identifier, account="Old Account")
    new = post(identifier, account="New Account", body="New synthetic carbon beacon account source.")
    db.import_batch(ImportBatch(records=[old]))
    try:
        old_row = db.versioned_record(Filters(dataset="social", accounts=["Old Account"]), identifier)
        db.import_batch(ImportBatch(records=[new]))
        assert db.versioned_record(Filters(dataset="social", accounts=["Old Account"]), identifier) is None
        current = db.versioned_record(Filters(dataset="social", accounts=["New Account"]), identifier)
        assert current["version_id"] != old_row["version_id"] and current["account"] == "New Account"
        assert current["body"] == new.body and current["body_hash"] == digest(new.body)
        with db.connect() as conn:
            assert conn.execute("SELECT count(*) AS n FROM record_versions WHERE record_id=%s", (identifier,)).fetchone()["n"] == 2
    finally:
        with db.connect() as conn:
            conn.execute("UPDATE records SET active=false WHERE record_id=%s", (identifier,))
    admitted = [record for record in fixture_records() if record.record_id in ADMITTED_SOCIAL]
    before = db.health()
    try:
        db.import_batch(ImportBatch(records=[record.model_copy(update={"countable": False, "retrievable": False})
                                             for record in admitted]))
        unavailable = db.health()
        assert unavailable["record_counts"]["social"] > 0
        assert unavailable["countable_record_counts"].get("social", 0) == 0
        assert unavailable["record_counts"]["native"] == before["record_counts"]["native"]
        assert unavailable["countable_record_counts"]["native"] == before["countable_record_counts"]["native"]
        for arguments in ({}, {"group_by": "accounts"}):
            refused = catalog(db, Filters(dataset="social")).call("record_statistics", arguments)
            assert refused["status"] == "unavailable" and "collections" not in refused
    finally:
        db.import_batch(ImportBatch(records=admitted))
    assert db.health()["countable_record_counts"]["social"] == 8
    db.checked("current_version_and_admission", old_account_not_current=True, history_retained=True,
               pending_and_inactive_excluded=True, active_unadmitted_not_loaded=True,
               native_admission_unchanged=True)


def test_real_keyword_search_preserves_account_scope(isolated_db):
    db = isolated_db
    filters = scoped(accounts=["Alpha"], platforms=["Twitter"], date_from=date(2020, 3, 1),
                     date_to=date(2020, 3, 1), include_unknown_dates=False)
    expected = {"acct-alpha-one"}
    report = db.search_report("carbon beacon", filters, limit=10)
    assert {item.record_id for item in report["evidence"]} == expected
    assert report["diagnostics"]["scope_records"] == 1
    service = Service(Settings(show_source_links=False, web_search_enabled=False), db=db, rag=object())
    public = service.search_report("carbon beacon", filters, limit=10)
    assert {item.record_id for item in public["evidence"]} == expected
    assert all(item.url == item.archive_url == "" for item in public["evidence"])
    instagram = db.search_report("carbon beacon", scoped(accounts=["Alpha"], platforms=["Instagram"]), limit=10)
    assert {item.record_id for item in instagram["evidence"]} == {"acct-alpha-instagram"}
    assert db.search_report("carbon beacon", scoped(accounts=["(Unknown)"]), limit=10)["evidence"] == []
    db.checked("keyword_account_scope", exact_members=True, bodyless_counts_not_evidence=True,
               source_links_hidden=True, cross_platform_search_separated=True)


def test_real_mcp_account_groups_and_trusted_selection(isolated_db):
    db = isolated_db
    base = scoped(accounts=["Alpha"], platforms=["Twitter"], sponsors=["Company One"],
                  date_from=date(2020, 3, 1), date_to=date(2020, 3, 1), include_unknown_dates=False)
    tools = catalog(db, base)
    group = tools.call("record_statistics", {"group_by": "accounts"})
    assert group["status"] == "ok" and group["group_by"] == "accounts"
    assert group["groups"] == [{"dataset": "social", "name": "Alpha", "count": 2, "display_name": "Alpha"}]
    assert group["collections"] == [{"dataset": "social", "total": 2, "retrievable": 1, "unknown_dates": 0}]
    assert {row["record_id"] for row in group["records"]} == {"acct-alpha-one", "acct-unsearchable"}
    assert group["filters"]["accounts"] == ["Alpha"] and tools.base_filters == base
    assert tools.call("record_statistics", {"group_by": "accounts", "filters": {"accounts": ["Beta"]}})["status"] == "clarify"
    assert tools.call("record_statistics", {"filters": {"accounts": ["Alpha", "Beta"]}})["status"] == "clarify"
    # An empty requested list cannot erase the caller's account selection.
    no_broadening = tools.call("record_statistics", {"filters": {"accounts": []}})
    assert no_broadening["status"] == "ok" and no_broadening["collections"][0]["total"] == 2
    share = tools.call("record_statistics", {"measure": "share", "filters": {"record_ids": ["acct-alpha-one"]}})
    assert share["status"] == "ok" and share["collections"][0]["numerator"] == 1
    assert share["collections"][0]["denominator"] == 2 and share["collections"][0]["percentage"] == 50.0
    assert share["denominator_filters"]["accounts"] == ["Alpha"]
    assert tools.call("get_record", {"record_id": "acct-alpha-instagram"})["status"] == "clarify"
    search = tools.call("search_records", {"query": "carbon beacon"})
    assert search["status"] == "ok" and {row["record_id"] for row in search["evidence"]} == {"acct-alpha-one"}
    assert all(ref["verification_status"] == "exact_character_match" for ref in search["source_refs"])
    for dataset in ("native", "all"):
        other = catalog(db, Filters(dataset=dataset))
        reads = db.receipt["read_statement_executions"]
        assert other.call("record_statistics", {"filters": {"accounts": ["Alpha"]}})["status"] == "clarify"
        assert db.receipt["read_statement_executions"] == reads
        refused = other.call("record_statistics", {"group_by": "accounts"})
        assert refused["status"] in {"clarify", "invalid_request"}
    mixed = catalog(db, Filters(dataset="all", record_ids=list(FIXTURE_IDS)))
    selected = mixed.call("record_statistics", {"group_by": "accounts", "filters": {"dataset": "social", "accounts": ["Alpha"], "platforms": ["Instagram"]}})
    assert selected["status"] == "ok" and selected["filters"]["dataset"] == "social"
    assert selected["groups"] == [{"dataset": "social", "name": "Alpha", "count": 1, "display_name": "Alpha"}]
    assert {row["record_id"] for row in selected["records"]} == {"acct-alpha-instagram"}
    assert mixed.base_filters.dataset == "all" and not mixed.base_filters.accounts
    unknown = catalog(db, scoped()).call("record_statistics", {"group_by": "accounts", "filters": {"accounts": ["(Unknown)"]}})
    assert unknown["status"] == "ok" and unknown["groups"][0]["name"] == "(Unknown)" and unknown["groups"][0]["count"] == 1
    rendered = json.dumps((group, search, selected, unknown), ensure_ascii=False)
    assert "PRIVATE-ACCOUNT-FIXTURE" not in rendered and "example.invalid/" not in rendered
    db.checked("mcp_account_scope", all_narrows_explicitly_to_social=True, trusted_accounts_not_broadened=True,
               statistics_and_search_members_checked=True, social_unknown_supported=True,
               account_share_denominator_checked=True)
