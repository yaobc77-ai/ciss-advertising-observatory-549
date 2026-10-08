"""Independent social source-state membership and public MCP arithmetic.

Synthetic current rows only; no database, source import, model or network.
The membership oracle stores all thirteen explicit states separately from the
production validator/SQL so False and Unknown cannot be inferred from positives.
"""

import asyncio
import hashlib
import json
from collections import Counter

import pytest
from pydantic import ValidationError
from test_social_accounts_tools import AccountDB
from test_social_annotation_sources import PRIVATE, source_row

from observatory.config import Settings
from observatory.models import Filters
from observatory.research_tools import FiltersRequest, ScopeConflict, ToolCatalog
from observatory.service import Service, _plain_scope, summarize
from observatory.social_annotations import (
    NOTE,
    SCHEME,
    SOCIAL_STATES,
    STATUS,
    social_label_metadata,
    social_state_id,
    social_state_options,
)
from observatory.structured_queries import validate_share_scope

TRUE = social_state_id("green_binary", "source_true")
FALSE = social_state_id("green_binary", "source_false")
UNKNOWN = social_state_id("green_binary", "unknown")


class StateDB(AccountDB):
    """Full-scope membership arithmetic independently known from four rows."""

    def __init__(self):
        super().__init__()
        one = source_row()
        two = source_row(all_false=True)
        two.update(record_id="s2", version_id="v-s2", platform="YouTube")
        three = source_row()
        three.update(record_id="s3", version_id="v-s3", annotations=[], date=None,
                     date_basis="missing", account="Other Name")
        native = source_row()
        native.update(record_id="n1", dataset="native", labels=["green.claim"],
                      publisher="The New York Times", annotations=[])
        self.rows = [one, two, three, native]
        for row, states in zip(self.rows[:3], [
            {item["key"]: "source_true" if item["key"] in {"green_binary", "renewable_energy"}
             else "source_false" for item in social_label_metadata()},
            dict.fromkeys([item["key"] for item in social_label_metadata()], "source_false"),
            dict.fromkeys([item["key"] for item in social_label_metadata()], "unknown"),
        ], strict=True):
            row["oracle_states"] = states
        self.state_reads = []
        self.dashboard_reads = []
        self.drift_after_first = False
        self.bad_fingerprint = None
        self.bad_distribution = None

    def health(self):
        result = super().health()
        result["countable_record_counts"] = dict(Counter(
            row["dataset"] for row in self.rows
            if row.get("active", True) and row.get("countable", True)))
        return result

    @staticmethod
    def selected(rows, filters):
        members = AccountDB.selected(rows, filters.model_copy(update={"labels": []}))
        if not filters.labels:
            return members
        return [row for row in members if set(filters.labels) & (
            {social_state_id(key, state) for key, state in row["oracle_states"].items()}
            if row["dataset"] == "social" else set(row["labels"]))]

    def facets(self, dataset):
        result = super().facets(dataset)
        result["labels"] = [item["value"] for item in social_state_options()] if dataset == "social" else ["green.claim"]
        return result

    @staticmethod
    def fingerprint(rows):
        safe = sorted((row["record_id"], row["version_id"], row["oracle_states"])
                      for row in rows if row["dataset"] == "social")
        return hashlib.sha256(json.dumps(safe, sort_keys=True).encode()).hexdigest()

    def social_source_state_version(self, filters):
        assert filters.dataset == "social"
        self.state_reads.append(filters.model_copy(deep=True))
        digest = self.fingerprint(self.selected(self.rows, filters))
        if self.drift_after_first and len(self.state_reads) == 1:
            # Same record/body version and health marker; only annotation values change.
            self.rows[0]["oracle_states"]["renewable_energy"] = "source_false"
            payload = self.rows[0]["annotations"][0]["payload"]
            payload["values"]["renewable_energy"] = False
            payload["labels"].remove("renewable_energy")
        return self.bad_fingerprint if self.bad_fingerprint is not None else digest

    def dashboard(self, filters, offset=0, limit=20, sort_by="date", descending=True):
        self.dashboard_reads.append(filters.model_copy(deep=True))
        rows = self.public_rows(filters)
        social = [row for row in rows if row["dataset"] == "social"]
        valid = sum(row["oracle_states"]["green_binary"] != "unknown" for row in social)
        distribution = {"scheme": SCHEME, "status": STATUS, "note": NOTE, "total": len(social),
            "valid_annotation_records": valid, "unknown_annotation_records": len(social) - valid,
            "source_state_version": self.fingerprint(social), "private": PRIVATE,
            "items": [{**item, **{state: sum(row["oracle_states"][item["key"]] == state
                                          for row in social) for state in SOCIAL_STATES}, "raw": PRIVATE}
                      for item in social_label_metadata()]}
        if self.bad_distribution:
            self.bad_distribution(distribution)
        return {"stats": summarize(rows), "page": {"rows": rows[offset:offset + limit],
                "total": len(rows), "offset": offset}, "social_historical_labels": distribution}


def catalog(base=None, *, links=True):
    db = StateDB()
    service = Service(Settings(show_source_links=links), db=db, rag=object())
    return ToolCatalog(service, base or Filters(dataset="social")), db, service


def distribution(tools, **arguments):
    return tools.call("record_statistics", {"group_by": "social_historical_labels", **arguments})


@pytest.mark.parametrize("dataset,label", [
    ("native", TRUE), ("all", FALSE), ("social", "green_binary"),
    ("native", SCHEME), ("all", SCHEME + ":bad:source_true"),
    ("social", SCHEME + ":green_binary:true"),
    ("social", SCHEME + ":green_binary:source_false:extra"),
])
def test_fixed_namespace_rejects_invalid_filters_and_copy_or_share_bypass(dataset, label):
    with pytest.raises(ValidationError):
        Filters(dataset=dataset, labels=[label])
    forged = Filters().model_copy(update={"dataset": dataset, "labels": [label]})
    with pytest.raises(ValueError):
        validate_share_scope(forged, Filters(dataset=dataset))
    tools, _, _ = catalog(Filters(dataset=dataset))
    assert tools.call("record_statistics", {"filters": {"labels": [label]}})["status"] == "clarify"


def test_full_distribution_includes_false_unknown_and_all_codes_beyond_page():
    tools, db, service = catalog()
    result = distribution(tools)
    assert result["status"] == "ok", result
    summary = result["distribution"]
    assert (summary["total"], summary["valid_annotation_records"], summary["unknown_annotation_records"]) == (3, 2, 1)
    assert len(summary["items"]) == 13 and len(result["records"]) == 1
    assert sum(item["source_true"] for item in summary["items"]) == 2
    assert all(item["source_true"] + item["source_false"] == 2 and item["unknown"] == 1
               for item in summary["items"])
    assert len(db.state_reads) == 2 and all(scope.dataset == "social" for scope in db.state_reads)
    assert result["historical_source_state_guard"] == {
        "filters": tools.base_filters.model_dump(mode="json"),
        "source_state_version": summary["source_state_version"],
    }
    assert PRIVATE not in json.dumps(result) and "raw" not in summary["items"][0]
    answer = service._tool_statistics_answer(result, base_filters=tools.base_filters)
    assert "3 selected social posts" in answer.answer and "2 have a valid" in answer.answer
    assert "Green messaging: True 1; False 1; unknown 1." in answer.answer
    assert "all selected posts as its denominator" in answer.answer and NOTE in answer.answer
    assert answer.answer_mode == "statistics"


def test_valid_all_false_is_not_unknown_or_unlabeled_and_zero_true_is_real_zero():
    tools, db, service = catalog(Filters(dataset="social", record_ids=["s2"]))
    result = distribution(tools)
    summary = result["distribution"]
    assert result["status"] == "ok" and summary["valid_annotation_records"] == 1
    assert summary["unknown_annotation_records"] == 0
    assert NOTE in result["scope_notes"] and not any("cannot distinguish missing annotation" in note
                                                    for note in result["scope_notes"])
    assert all((item["source_true"], item["source_false"], item["unknown"]) == (0, 1, 0)
               for item in summary["items"])
    assert "unlabeled" not in service._tool_statistics_answer(result, base_filters=tools.base_filters).answer
    # Legal state options exist even when no currently selected post has that state.
    zero = tools.call("record_statistics", {"filters": {"labels": [TRUE]}})
    assert zero["status"] == "ok" and zero["collections"][0]["total"] == 0
    assert db.state_reads[-1].record_ids == ["s2"]


def test_all_scope_must_explicitly_narrow_and_native_has_no_social_context():
    tools, db, _ = catalog(Filters(dataset="all"))
    assert distribution(tools)["status"] == "clarify" and db.state_reads == []
    assert tools.call("record_statistics", {"filters": {"labels": [TRUE]}})["status"] == "clarify"
    scoped = distribution(tools, filters={"dataset": "social"})
    assert scoped["status"] == "ok" and scoped["distribution"]["total"] == 3
    context = tools.entity_context()["social_historical_labels"]
    assert context["options"] == [{"value": item["value"]} for item in social_state_options()]
    assert context["match"] == "any_selected_state_OR_not_AND"
    assert context["scheme"] == SCHEME and context["status"] == STATUS
    assert context["note"] == NOTE
    assert "filters.dataset='social'" in context["required_scope"]
    assert context["states"]["source_true"] == "Source export recorded True"
    assert context["states"]["source_false"] == "Source export recorded False"
    assert "not False" in context["states"]["unknown"]
    native, native_db, _ = catalog(Filters())
    assert distribution(native)["status"] == "clarify" and native_db.state_reads == []
    assert "social_historical_labels" not in native.entity_context()
    old = native.call("record_statistics", {"filters": {"labels": ["green.claim"]}})
    assert old["status"] == "ok" and old["collections"][0]["total"] == 1
    assert native_db.state_reads == []


@pytest.mark.parametrize("extra", [
    {"measure": "share"}, {"ranking": "highest"},
    {"denominator_filters": {"dataset": "social"}},
    {"periods": [{"label": "first", "date_from": "2020-01-01", "date_to": "2020-12-31"},
                 {"label": "second", "date_from": "2021-01-01", "date_to": "2021-12-31"}]},
])
def test_distribution_rejects_unsupported_statistics_before_state_reads(extra):
    tools, db, _ = catalog()
    assert distribution(tools, **extra)["status"] in {"clarify", "invalid_request"}
    assert db.state_reads == db.dashboard_reads == []


def test_current_label_and_all_metadata_scope_survive_empty_or_disjoint_requests():
    base = Filters(dataset="social", labels=[FALSE], accounts=["Shared Name"], platforms=["YouTube"],
                   sponsors=["Company affiliation"], record_ids=["s1", "s2"],
                   date_from="2020-01-01", date_to="2020-12-31", include_unknown_dates=False)
    tools, _, _ = catalog(base)
    base.labels.clear()  # trusted scope has already been frozen
    unchanged = tools.narrow(FiltersRequest(labels=[]))
    assert unchanged.labels == [FALSE] and unchanged.accounts == ["Shared Name"]
    assert unchanged.platforms == ["YouTube"] and unchanged.record_ids == ["s1", "s2"]
    for target in ([TRUE], [FALSE, UNKNOWN]):
        with pytest.raises(ScopeConflict):
            tools.narrow(FiltersRequest(labels=target))
    result = distribution(tools, filters={"labels": []})
    assert result["status"] == "ok" and result["distribution"]["total"] == 1
    assert result["records"][0]["record_id"] == "s2"
    assert tools.call("get_record", {"record_id": "s1"})["status"] == "clarify"


def test_multiple_state_ids_use_union_not_cross_code_conjunction():
    tools, _, _ = catalog()
    renewable_false = social_state_id("renewable_energy", "source_false")
    result = tools.call("record_statistics", {"filters": {"labels": [TRUE, renewable_false]}})
    assert result["status"] == "ok" and result["collections"][0]["total"] == 2
    assert {row["record_id"] for row in result["records"]} == {"s1", "s2"}
    plain = _plain_scope(Filters(dataset="social", labels=[TRUE, renewable_false]), Filters(dataset="social"))
    assert "matching any" in plain and "True" in plain and "False" in plain


@pytest.mark.parametrize("state,record_id", [(FALSE, "s2"), (UNKNOWN, "s3")])
def test_false_unknown_detail_sources_and_search_keep_exact_scopes_and_hide_links(state, record_id):
    tools, db, _ = catalog(Filters(dataset="social", labels=[state]), links=False)
    detail = tools.call("get_record", {"record_id": record_id})
    assert detail["status"] == "ok" and detail["record"]["url"] == ""
    sources = tools.call("get_record_sources", {"record_id": record_id})
    assert sources["status"] == "ok" and not sources["source_artifacts"]
    history = sources["social_historical_annotation"]
    assert history["validation_state"] == ("bound" if state == FALSE else "missing")
    assert all(item["state"] == ("source_false" if state == FALSE else "unknown") for item in history["values"])
    search = tools.call("search_records", {"query": "renewable", "limit": 5})
    assert search["status"] == "ok" and {item["record_id"] for item in search["evidence"]} == {record_id}
    assert tools.call("get_record", {"record_id": "s1"})["status"] == "clarify"
    assert PRIVATE not in json.dumps([detail, sources, search]) and "https://" not in json.dumps([detail, sources, search])
    assert all(scope.labels == [state] for scope in db.state_reads)


def test_share_preserves_source_state_target_and_trusted_denominator():
    tools, db, service = catalog()
    result = tools.call("record_statistics", {"measure": "share", "filters": {"labels": [TRUE]}})
    assert result["status"] == "ok", result
    counts = result["collections"][0]
    assert (counts["numerator"], counts["denominator"]) == (1, 3)
    assert result["denominator_filters"]["labels"] == [] and result["filters"]["labels"] == [TRUE]
    assert all(scope.labels == [] for scope in db.state_reads)
    assert result["historical_source_state_guard"]["filters"] == tools.base_filters.model_dump(mode="json")
    assert NOTE in service._tool_statistics_answer(result, base_filters=tools.base_filters).answer
    # A comparison source state cannot clear a currently active selection.
    scoped, _, _ = catalog(Filters(dataset="social", labels=[FALSE]))
    bad = scoped.call("record_statistics", {"measure": "share", "filters": {"record_ids": ["s2"]},
                      "denominator_filters": {"labels": [TRUE]}})
    assert bad["status"] == "clarify"


@pytest.mark.parametrize("mode", ["distribution", "count", "source", "comparison"])
def test_annotation_drift_without_health_or_body_change_is_not_published(mode):
    tools, db, _ = catalog()
    db.drift_after_first = True
    before = db.version
    if mode == "distribution":
        result = distribution(tools)
    elif mode == "count":
        result = tools.call("record_statistics", {"filters": {"labels": [TRUE]}})
    elif mode == "source":
        result = tools.call("get_record_sources", {"record_id": "s1", "filters": {"labels": [TRUE]}})
    else:
        db.rows[1]["sponsor"] = "Other company"
        result = tools.call("search_records", {"query": "renewable", "comparison_scopes": [
            {"query": "renewable", "filters": {"sponsors": ["Company affiliation"], "labels": [TRUE]}},
            {"query": "renewable", "filters": {"sponsors": ["Other company"], "labels": [FALSE]}}]})
    assert result["status"] == "unavailable", result
    assert db.version == before and len(db.state_reads) == 2
    assert "changed" in result["message"] and "distribution" not in result and "evidence" not in result


@pytest.mark.parametrize("bad", [None, "", "A" * 64, "not-a-digest"])
def test_bad_or_missing_source_fingerprint_cannot_be_claimed_current(bad):
    tools, db, _ = catalog()
    db.social_source_state_version = lambda _filters: bad
    assert distribution(tools)["status"] == "unavailable"
    assert db.dashboard_reads == []


def test_distribution_snapshot_fingerprint_must_equal_before_and_after_guard():
    tools, db, _ = catalog()
    db.bad_distribution = lambda data: data.update(source_state_version="f" * 64)
    result = distribution(tools)
    assert result["status"] == "unavailable" and "changed" in result["message"]


@pytest.mark.parametrize("change", [
    lambda d: d.update(total=True), lambda d: d.update(valid_annotation_records=-1),
    lambda d: d.update(unknown_annotation_records=0), lambda d: d.update(scheme="native"),
    lambda d: d.update(source_state_version="bad"), lambda d: d["items"].pop(),
    lambda d: d["items"].__setitem__(0, d["items"][1]),
    lambda d: d["items"][0].update(source_true=True),
    lambda d: d["items"][0].update(unknown=0), lambda d: d["items"][0].update(label="Invented meaning"),
])
def test_service_rejects_non_reconciling_or_invented_full_distributions(change):
    tools, _, service = catalog()
    result = distribution(tools)
    change(result["distribution"])
    with pytest.raises(ValueError):
        service._tool_statistics_answer(result, base_filters=tools.base_filters)


def test_not_admitted_or_stopped_collection_is_unavailable_not_zero():
    for field in ("countable", "active"):
        tools, db, _ = catalog()
        for row in db.rows:
            if row["dataset"] == "social":
                row[field] = False
        result = distribution(tools)
        assert result["status"] == "unavailable" and "distribution" not in result
        assert db.state_reads == db.dashboard_reads == []


def test_official_mcp_memory_protocol_lists_new_group_and_source_state_options():
    from mcp import Client

    from observatory.mcp_server import build_mcp_server

    tools, _, _ = catalog()

    async def check():
        async with Client(build_mcp_server(tools), mode="legacy") as client:
            listed = await client.list_tools()
            tool = next(item for item in listed.tools if item.name == "record_statistics")
            assert "social_historical_labels" in json.dumps(tool.input_schema)
            assert "OR" in json.dumps(tool.input_schema) and tool.annotations.read_only_hint
            result = await client.call_tool("record_statistics", {"group_by": "social_historical_labels"})
            assert not result.is_error and result.structured_content["distribution"]["total"] == 3
            assert len(result.structured_content["distribution"]["items"]) == 13
            assert json.loads(result.content[0].text) == result.structured_content

    asyncio.run(check())
