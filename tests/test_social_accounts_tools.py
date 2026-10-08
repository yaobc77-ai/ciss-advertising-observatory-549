"""Exact social account scopes on independent rows and offline SQL transports.

No business database, model, credentials, or network is accessed. SQL transport
assertions establish query boundaries, not PostgreSQL execution or social admission.
"""

import hashlib
import json
from collections import Counter
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from observatory.config import Settings
from observatory.db import Database
from observatory.models import Evidence, Filters
from observatory.research_tools import FiltersRequest, ScopeConflict, ToolCatalog
from observatory.service import Service, _plain_scope, summarize
from observatory.structured_queries import QuestionPlan, validate_share_scope


def post(record_id, account, platform="X", *, dated="2020-03-01", body="Source emissions text."):
    return {"record_id": record_id, "version_id": "v-" + record_id, "dataset": "social",
            "title": record_id, "account": account, "platform": platform, "publisher": "",
            "sponsor": "Company affiliation", "keyword": "energy", "date": dated,
            "date_basis": "source" if dated else "missing", "retrievable": bool(body),
            "body": body, "body_hash": hashlib.sha256(body.encode()).hexdigest(),
            "url": "https://example.org/" + record_id, "archive_url": "file:///private/archive",
            "sponsor_basis": "company_affiliation_not_verified_paid_sponsor",
            "labels": [], "raw": {"private": "never-public"}, "countable": True}


class AccountDB:
    """Independent membership oracle; source names are never normalized."""

    def __init__(self):
        self.rows = [post("s1", "Shared Name"), post("s2", "Shared Name", "YouTube", body=""),
                     post("s3", "Other Name"), post("s4", "", dated=None),
                     {**post("n1", "", ""), "dataset": "native", "publisher": "The New York Times"},
                     {**post("not-admitted", "Excluded"), "countable": False},
                     {**post("withdrawn", "Excluded"), "active": False}]
        self.scopes, self.transactions = [], []
        self.version = "offline-current-version"
        self.countable_counts = None
        self.bad_row = None
        self.bad_evidence = None
        self.reads = []
        self.searches = []

    def health(self):
        result = {"status": "ok", "record_counts": dict(Counter(
            row["dataset"] for row in self.rows if row.get("active", True))), "data_version": self.version}
        if self.countable_counts is not None:
            result["countable_record_counts"] = self.countable_counts
        return result

    @staticmethod
    def selected(rows, filters):
        result = []
        for row in deepcopy(rows):
            if not row.get("active", True) or not row.get("countable", True):
                continue
            if filters.dataset not in ("all", row["dataset"]):
                continue
            fields = {"publishers": "publisher", "sponsors": "sponsor", "platforms": "platform",
                      "accounts": "account", "keywords": "keyword"}
            if any(getattr(filters, dimension) and (row.get(field) or "(Unknown)") not in getattr(filters, dimension)
                   for dimension, field in fields.items()):
                continue
            if filters.record_ids and row["record_id"] not in filters.record_ids:
                continue
            if filters.date_presence == "known" and not row["date"] or filters.date_presence == "missing" and row["date"]:
                continue
            if row["date"]:
                when = date.fromisoformat(row["date"])
                if filters.date_from and when < filters.date_from or filters.date_to and when > filters.date_to:
                    continue
            elif not filters.include_unknown_dates:
                continue
            result.append(row)
        return result

    def public_rows(self, filters):
        self.reads.append(filters.model_copy(deep=True))
        return self.selected(self.rows, filters)

    def facets(self, dataset):
        rows = self.selected(self.rows, Filters(dataset=dataset))
        result = {dimension: sorted({row.get(field) or "(Unknown)" for row in rows})
                  for dimension, field in {"publishers": "publisher", "sponsors": "sponsor",
                    "platforms": "platform", "keywords": "keyword"}.items()}
        result.update(accounts=sorted({row.get("account") or "(Unknown)" for row in rows
                                       if row["dataset"] == "social"}), labels=[])
        return result

    def dashboard(self, filters, offset=0, limit=20, sort_by="date", descending=True):
        rows = self.public_rows(filters)
        return {"stats": summarize(rows), "page": {"rows": rows[offset:offset + limit],
                "total": len(rows), "offset": offset}}

    def versioned_record(self, filters, record_id):
        if self.bad_row:
            return deepcopy(self.bad_row)
        rows = self.public_rows(filters)
        return next((row for row in rows if row["record_id"] == record_id), None)

    def search_report(self, question, filters, limit=5):
        self.searches.append(filters.model_copy(deep=True))
        rows = [row for row in self.public_rows(filters) if row["retrievable"]][:limit]
        evidence = [Evidence(evidence_id="e-" + row["record_id"], record_id=row["record_id"],
                    version_id=row["version_id"], dataset=row["dataset"], title=row["title"],
                    text=row["body"], start=0, end=len(row["body"])) for row in rows]
        if self.bad_evidence:
            evidence = [self.bad_evidence]
        return {"evidence": evidence, "diagnostics": {"status": "ok"}}

    def search(self, query, filters, **kwargs):
        return self.search_report(query, filters, kwargs.get("limit", 5))["evidence"]

    def _public_query(self, filters):
        # Exercise the production SQL scope validator before the fake snapshot.
        Database.where(filters)
        index = len(self.scopes)
        self.scopes.append(filters.model_copy(deep=True))
        return f"SELECT * FROM offline_scope_{index}", [index]

    def connect(self):
        db, captured, queries = self, deepcopy(self.rows), []
        self.transactions.append(queries)

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                queries.append((sql, params))
                if sql.startswith("SET TRANSACTION"):
                    return None
                assert "JOIN denominator d USING(record_id,version_id)" in sql
                denominator = db.selected(captured, db.scopes[params[0]])
                keys = {(row["record_id"], row["version_id"]) for row in denominator}
                numerator = [row for row in db.selected(captured, db.scopes[params[1]])
                             if (row["record_id"], row["version_id"]) in keys]
                if "count(*) AS numerator" in sql:
                    return SimpleNamespace(fetchone=lambda: {"numerator": len(numerator),
                        "denominator": len(denominator), "retrievable": sum(r["retrievable"] for r in numerator),
                        "unknown_dates": sum(not r["date"] for r in numerator)})
                return SimpleNamespace(fetchall=lambda: numerator[:10])

        return Connection()


def catalog(base=None):
    db = AccountDB()
    service = Service(Settings(show_source_links=True), db=db, rag=object())
    return ToolCatalog(service, base or Filters(dataset="social")), db, service


def test_old_filter_json_defaults_to_empty_accounts_and_new_scope_roundtrips():
    legacy = Filters(dataset="social").model_dump(mode="json")
    legacy.pop("accounts")
    assert Filters.model_validate(legacy).accounts == []
    scoped = Filters(dataset="social", accounts=["Shared Name"])
    assert Filters.model_validate_json(scoped.model_dump_json()) == scoped
    assert json.dumps(legacy, sort_keys=True) != json.dumps(scoped.model_dump(mode="json"), sort_keys=True)


@pytest.mark.parametrize("dataset", ["native", "all"])
def test_non_social_account_filters_rejected_even_after_copy_bypasses_validation(dataset):
    with pytest.raises(ValidationError, match="social collection"):
        Filters(dataset=dataset, accounts=["(Unknown)"])
    forged = Filters(dataset=dataset).model_copy(update={"accounts": ["Shared Name"]})
    with pytest.raises(ValueError, match="social collection"):
        Database.where(forged)
    with pytest.raises(ValueError, match="social collection"):
        validate_share_scope(forged, Filters(dataset=dataset))


def test_sql_account_is_parameterized_and_keeps_current_countable_scope():
    literal = "Shared Name'); DROP TABLE records; --"
    sql, params = Database("unused-offline")._public_query(Filters(dataset="social", accounts=[literal]))
    assert literal not in sql and [literal] in params
    assert "COALESCE(NULLIF(v.payload->>'account',''),'(Unknown)')=ANY(%s)" in sql
    assert "v.version_id=r.current_version" in sql and "r.active" in sql
    assert "(v.payload->>'countable')::boolean" in sql


def test_summarize_accounts_only_social_with_unknown_and_independent_denominator():
    rows = AccountDB.selected(AccountDB().rows, Filters(dataset="all"))
    stats = summarize(rows)
    assert stats["total"] == 5
    assert stats["accounts"] == [{"name": "Shared Name", "count": 2, "percent": 50},
                                 {"name": "(Unknown)", "count": 1, "percent": 25},
                                 {"name": "Other Name", "count": 1, "percent": 25}]
    assert summarize([row for row in rows if row["dataset"] == "native"])["accounts"] == []


def test_all_scope_requires_explicit_social_narrowing_for_filter_group_and_resolution():
    tools, _, _ = catalog(Filters(dataset="all"))
    assert tools.call("record_statistics", {"filters": {"accounts": ["Shared Name"]}})["status"] == "clarify"
    assert tools.call("record_statistics", {"group_by": "accounts"})["status"] == "clarify"
    assert tools.call("resolve_entity", {"entity_type": "account", "query": "Shared Name"})["status"] == "clarify"
    scoped = tools.call("record_statistics", {"group_by": "accounts", "filters": {"dataset": "social"}})
    assert scoped["status"] == "ok" and scoped["collections"][0]["total"] == 4
    assert {row["dataset"] for row in scoped["records"]} == {"social"}
    native, _, _ = catalog(Filters())
    assert native.call("record_statistics", {"filters": {"dataset": "social", "accounts": ["Shared Name"]}})["status"] == "clarify"


def test_narrowing_preserves_account_platform_date_ids_and_refuses_disjoint_or_clear():
    base = Filters(dataset="social", accounts=["Shared Name"], platforms=["X"], record_ids=["s1", "s2"],
                   date_from="2020-01-01", date_to="2020-12-31", include_unknown_dates=False)
    tools, _, _ = catalog(base)
    base.accounts.clear()  # Catalog scope is already frozen.
    unchanged = tools.narrow(FiltersRequest(accounts=[], platforms=[]))
    assert unchanged.accounts == ["Shared Name"] and unchanged.platforms == ["X"]
    assert unchanged.record_ids == ["s1", "s2"] and unchanged.date_from == date(2020, 1, 1)
    for args in ({"accounts": ["Other Name"]}, {"accounts": ["Shared Name", "Other Name"]},
                 {"platforms": ["YouTube"]}, {"record_ids": ["s3"]}, {"date_presence": "missing"}):
        with pytest.raises(ScopeConflict):
            tools.narrow(FiltersRequest(**args))
    assert tools.call("record_statistics", {"filters": {"accounts": ["shared name"]}})["status"] == "clarify"


def test_exact_resolver_does_not_borrow_outlet_aliases_or_merge_case_variants():
    tools, db, _ = catalog()
    db.rows.extend([post("nyt", "The New York Times"), post("case", "shared name")])
    result = tools.call("resolve_entity", {"entity_type": "account", "query": "NYT"})
    assert result["candidate_count"] == 0 and result["status"] == "clarify"
    names = tools.call("resolve_entity", {"entity_type": "account", "query": "Shared Name"})
    assert names["candidate_count"] == 2 and names["status"] == "clarify"
    assert {item["source_value"] for item in names["candidates"]} == {"Shared Name", "shared name"}
    assert "alias_resolutions" not in names
    filtered, _, _ = catalog(Filters(dataset="social", accounts=["Shared Name"]))
    resolved = filtered.call("resolve_entity", {"entity_type": "account", "query": "Shared Name"})
    assert resolved["candidate_count"] == 1
    assert resolved["candidates"][0]["identity_status"] == "source_candidate_not_resolved"


def test_account_grouping_counts_every_member_beyond_examples_and_keeps_metadata():
    tools, db, service = catalog()
    db.rows = [post(f"many-{n}", f"Account {n}", body="" if n % 2 else "Source text.") for n in range(24)]
    result = tools.call("record_statistics", {"group_by": "accounts"})
    assert result["status"] == "ok" and result["kind"] == "list_accounts"
    assert len(result["groups"]) == 24 and sum(item["count"] for item in result["groups"]) == 24
    assert len(result["records"]) == 10 and result["collections"][0]["retrievable"] == 12
    assert all(row["account"] and row["platform"] == "X" for row in result["records"])
    answer = service._tool_statistics_answer(result, base_filters=tools.base_filters)
    assert "24 source-listed account names" in answer.answer
    assert "private" not in str(result) and "file:///" not in str(result)
    plan = QuestionPlan("ready", kind="list_accounts", filters=Filters(dataset="social"))
    assert plan.group_by == "accounts"


def test_unknown_accounts_are_missing_social_values_and_unsearchable_members_count():
    tools, _, _ = catalog()
    shared = tools.call("record_statistics", {"filters": {"accounts": ["Shared Name"]}})
    assert shared["collections"][0]["total"] == 2 and shared["collections"][0]["retrievable"] == 1
    unknown = tools.call("record_statistics", {"filters": {"accounts": ["(Unknown)"]}})
    assert unknown["collections"][0]["total"] == 1 and unknown["records"][0]["record_id"] == "s4"
    assert unknown["records"][0]["account"] == ""
    assert tools.call("record_statistics", {"filters": {"accounts": ["Excluded"]}})["status"] == "clarify"


def test_share_account_target_does_not_become_its_own_denominator():
    tools, db, _ = catalog()
    result = tools.call("record_statistics", {"measure": "share", "filters": {"accounts": ["Shared Name"]}})
    counts = result["collections"][0]
    assert result["status"] == "ok" and (counts["numerator"], counts["denominator"], counts["percentage"]) == (2, 4, 50)
    assert result["denominator_filters"]["accounts"] == [] and result["filters"]["accounts"] == ["Shared Name"]
    assert db.scopes[0].accounts == [] and db.scopes[1].accounts == ["Shared Name"]
    assert {(row["account"], row["platform"]) for row in result["records"]} == {("Shared Name", "X"), ("Shared Name", "YouTube")}
    assert len(db.transactions) == 1 and db.transactions[0][0][0].endswith("READ ONLY")


def test_named_account_denominator_and_platform_target_keep_trusted_account():
    tools, _, service = catalog()
    result = tools.call("record_statistics", {"measure": "share",
        "denominator_filters": {"accounts": ["Shared Name"]}, "filters": {"platforms": ["X"]}})
    assert result["status"] == "ok" and result["collections"][0]["percentage"] == 50
    assert result["filters"]["accounts"] == result["denominator_filters"]["accounts"] == ["Shared Name"]
    answer = service._tool_statistics_answer(result, base_filters=tools.base_filters)
    assert "source-listed accounts Shared Name" in answer.answer
    affiliation = Filters(dataset="social", sponsors=["Company affiliation"])
    assert "company affiliation" in _plain_scope(affiliation, Filters(dataset="social"))
    assert "sponsored by" not in _plain_scope(affiliation, Filters(dataset="social"))


def test_share_scope_cannot_remove_or_replace_account_and_all_denominator_remains_separate():
    active = Filters(dataset="social", accounts=["Shared Name"])
    for escaped in (Filters(dataset="social"), Filters(dataset="social", accounts=["Other Name"])):
        with pytest.raises(ValueError, match="widen active filters"):
            validate_share_scope(escaped, active)
    tools, _, _ = catalog(Filters(dataset="all"))
    result = tools.call("record_statistics", {"measure": "share",
        "filters": {"dataset": "social", "accounts": ["Shared Name"]}})
    assert result["status"] == "ok" and len(result["collections"]) == 1
    assert result["collections"][0]["dataset"] == "social" and result["collections"][0]["denominator"] == 4
    assert result["denominator_filters"]["dataset"] == "all"


def test_custom_statistics_adapter_cannot_clear_trusted_account_for_count():
    tools, _, service = catalog(Filters(dataset="social", accounts=["Shared Name"]))
    result = tools.call("record_statistics", {})
    assert result["status"] == "ok"
    escaped = deepcopy(result)
    escaped["filters"]["accounts"] = []
    with pytest.raises(ValueError, match="widen active filters"):
        service._tool_statistics_answer(escaped, base_filters=tools.base_filters)
    forged = deepcopy(result)
    forged["method"] = "model"
    with pytest.raises(ValueError, match="database scope"):
        service._tool_statistics_answer(forged, base_filters=tools.base_filters)


def test_record_text_sources_export_and_keyword_search_keep_account_scope():
    tools, db, service = catalog(Filters(dataset="social", accounts=["Shared Name"], platforms=["X"]))
    for name in ("get_record", "get_record_sources"):
        result = tools.call(name, {"record_id": "s1"})
        assert result["status"] == "ok" and result["record"]["account"] == "Shared Name"
        assert result["record"]["platform"] == "X"
        assert result["source_refs"][0]["version_id"] == "v-s1"
        assert result["record"]["company_affiliation"] == "Company affiliation"
        assert "not proof of paid sponsorship" in result["record"]["relation_note"]
        assert tools.call(name, {"record_id": "s2"})["status"] == "clarify"
        assert tools.call(name, {"record_id": "s3"})["status"] == "clarify"
    search = tools.call("search_records", {"query": "emissions"})
    assert [item["record_id"] for item in search["evidence"]] == ["s1"]
    assert search["source_refs"][0]["verification_status"] == "exact_character_match"
    assert [row["record_id"] for row in service.browse(tools.base_filters)] == ["s1"]
    assert all(scope.accounts == ["Shared Name"] for scope in db.reads if scope.dataset == "social")


@pytest.mark.parametrize("corruption", ["other_account", "old_version", "body_hash", "range"])
def test_retrieval_rejects_scope_and_current_source_counterexamples(corruption):
    tools, db, _ = catalog(Filters(dataset="social", accounts=["Shared Name"]))
    row = deepcopy(db.rows[0])
    evidence = Evidence(evidence_id="bad", record_id="s1", version_id="v-s1", dataset="social",
                        title="s1", text=row["body"], start=0, end=len(row["body"]))
    if corruption == "other_account":
        row["account"] = "Other Name"
    elif corruption == "old_version":
        evidence.version_id = "superseded-version"
    elif corruption == "body_hash":
        row["body_hash"] = "0" * 64
    else:
        row["retrieval_ranges"] = [(0, 1)]
    db.bad_row, db.bad_evidence = row, evidence
    result = tools.call("search_records", {"query": "emissions"})
    # Every retrieved passage was rejected: fail closed, never report a database miss.
    assert result["status"] == "unavailable" and result["search_status"] == "failed"
    assert result["evidence"] == [] and result["rejected_evidence"] == 1


def test_rag_adapter_cannot_widen_account_before_any_model_or_search():
    tools, db, service = catalog(Filters(dataset="social", accounts=["Shared Name"]))
    escaped = Filters(dataset="social", accounts=["Other Name"])
    result = service._answer_evidence("Explain source text", tools.base_filters, "offline", audit=False,
        retrieval_groups=[{"query": "emissions", "filters": escaped.model_dump(mode="json")}])
    assert result.status == "service_unavailable" and db.searches == []
    assert all(scope.accounts == ["Shared Name"] for scope in db.reads)
    assert result.cost_usd == 0  # rag=object() cannot make any model call.


def test_nonadmitted_social_is_unavailable_and_legacy_health_still_supported():
    tools, db, service = catalog()
    db.countable_counts = {"native": 1, "social": 0}
    assert db.health()["record_counts"]["social"] > 0
    assert tools.call("record_statistics", {"group_by": "accounts"})["status"] == "unavailable"
    assert tools.call("search_records", {"query": "emissions"})["status"] == "unavailable"
    assert db.reads == []
    # The service share route itself also refuses a false zero count.
    plan = SimpleNamespace(kind="share", filters=Filters(dataset="social"), group_by=None,
        denominator_filters=Filters(dataset="social"), scope_notes=())
    with pytest.raises(ValueError, match="loaded collection"):
        service._share_statistics_answer(plan)
    assert db.scopes == []
    db.countable_counts = None
    assert tools.call("record_statistics", {"group_by": "accounts"})["status"] == "ok"


def test_transport_schema_accounts_are_bounded_and_unknown_fields_closed():
    tools, _, _ = catalog()
    definitions = {item["name"]: item for item in tools.mcp_definitions()}
    schema = definitions["record_statistics"]["inputSchema"]
    assert "accounts" in schema["properties"]["group_by"]["anyOf"][0]["enum"]
    filters = schema["$defs"]["FiltersRequest"]
    assert filters["additionalProperties"] is False
    def resolve(node):
        while "$ref" in node:
            assert node["$ref"].startswith("#/$defs/")
            node = schema["$defs"][node["$ref"].split("/")[-1]]
        return node

    choices = resolve(filters["properties"]["accounts"])
    names = resolve(choices["anyOf"][0])
    assert names["maxItems"] == 20
    assert resolve(names["items"])["maxLength"] == 200
    assert resolve(names["items"])["minLength"] == 1
    assert tools.call("record_statistics", {"filters": {"accounts": ["Shared Name"] * 21}})["status"] == "invalid_request"
    assert tools.call("record_statistics", {"filters": {"account": "Shared Name"}})["status"] == "invalid_request"


class SQLTransport(Database):
    """Predetermined result transport for production query/projection checks."""

    def __init__(self, results):
        super().__init__("unused-offline")
        self.results, self.queries = iter(results), []

    def connect(self):
        db = self

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                db.queries.append((sql, params))
                value = next(db.results)
                return SimpleNamespace(fetchone=lambda: value, fetchall=lambda: value)

        return Connection()


def test_sql_facets_only_social_accounts_and_native_empty_account_list():
    db = SQLTransport([[{"name": "accounts", "value": "Shared Name"},
                        {"name": "accounts", "value": "(Unknown)"}]])
    assert db.facets("all")["accounts"] == ["Shared Name", "(Unknown)"]
    assert "('accounts',account)" in db.queries[0][0]
    assert "WHERE option.name<>'accounts' OR dataset='social'" in db.queries[0][0]
    native = SQLTransport([[]])
    assert native.facets("native")["accounts"] == []
    assert native.queries[0][1] == ["native"]


def test_sql_dashboard_full_account_aggregate_and_social_only_percentage():
    from observatory.social_annotations import social_label_metadata, social_state_id

    groups = [{"name": "accounts", "value": f"Account {n}", "count": 1} for n in range(24)]
    social = {"total": 24, "valid_annotation_records": 0, "unknown_annotation_records": 24,
              "state_counts": {social_state_id(item["key"], "unknown"): 24
                               for item in social_label_metadata()}, "source_state_version": "a" * 64}
    aggregate = {"total": 30, "retrievable": 0, "unknown_dates": 30,
                 "page_offset": 0, "page_rows": [], "groups": groups,
                 "relationships": [], "months": [], "years": [], "labels": [],
                 "native_labels": {"total": 6, "count": 0}, "inferred": [], "combined": None}
    db = SQLTransport([None, aggregate, social])
    result = db.dashboard(Filters(dataset="all"), limit=1)
    assert len(result["stats"]["accounts"]) == 24
    assert sum(row["count"] for row in result["stats"]["accounts"]) == 24
    assert sum(row["percent"] for row in result["stats"]["accounts"]) == pytest.approx(100)
    group_query = db.queries[1][0]
    assert "WHERE option.name<>'accounts' OR dataset='social'" in group_query
    grouped = group_query.split("), groups AS (", 1)[1].split("), relationships AS (", 1)[0]
    assert "LIMIT" not in grouped and "OFFSET" not in grouped
    assert len(db.queries) == 3  # Aggregate and complete source states share the protected snapshot.
    assert db.queries[0][0].endswith("READ ONLY")


def test_health_keeps_active_counts_and_current_countable_counts_in_same_snapshot(monkeypatch):
    observed = []
    monkeypatch.setattr("observatory.indexing.profile_snapshot", lambda conn: observed.append(conn)
                        or {"data_version": "snapshot-version"})
    db = SQLTransport([None, [{"dataset": "native", "n": 275, "countable_n": 263},
                              {"dataset": "social", "n": 37082, "countable_n": 0}],
                          {"records": "r", "annotations": "a", "dates": "d"}])
    result = db.health()
    assert result["record_counts"] == {"native": 275, "social": 37082}
    assert result["countable_record_counts"] == {"native": 263, "social": 0}
    assert result["data_version"] == "snapshot-version" and len(observed) == 1
    sql = db.queries[1][0]
    assert "count(*) AS n" in sql and "FILTER (WHERE (v.payload->>'countable')::boolean)" in sql
    assert "LEFT JOIN record_versions v ON v.version_id=r.current_version" in sql
    assert db.queries[0][0].endswith("READ ONLY")
