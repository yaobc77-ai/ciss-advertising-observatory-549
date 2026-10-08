"""Independent school/workshop SQL fixtures; require an explicitly private PG."""

import json
from collections import Counter
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from postgres_fixtures import configured_test_url, verified_test_connection
from psycopg.types.json import Jsonb

from observatory.date_inference import apply_inferences, propose_url_inferences
from observatory.db import Database
from observatory.models import Filters, ImportBatch, RecordInput
from observatory.research_tools import ToolCatalog
from observatory.service import Service


def fixture_rows():
    """Expected selected dates below are declared by fixture author, not SQL."""
    rows = [
        ("school-01", "native", "2016-01-01", None, "Compass Desk", "School Herald", None, True, True, True),
        ("school-02", "native", "2016-12-31", None, "Compass Desk", "School Herald", "", False, True, True),
        ("school-03", "native", "2017-01-01", None, "Oak Library", None, None, True, True, True),
        ("school-04", "native", None, "2017-06-20", "Oak Library", "", None, False, True, True),
        ("school-05", "native", "", None, "", "School Herald", "", True, True, True),
        ("school-withdrawn", "native", "2018-01-01", None, "Compass Desk", "School Herald", None, True, False, True),
        ("school-ineligible", "native", "2017-01-02", None, "Oak Library", "School Herald", None, True, True, False),
        ("school-unknown-eligibility", "native", "2017-01-02", None, "Oak Library", "School Herald", None, True, True, None),
        ("workshop-01", "social", "2016-04-01", None, "Compass Desk", "Campus Network", "Campus Service", True, True, True),
        ("workshop-02", "social", "2017-04-01", None, "Compass Desk", "Campus Network", None, True, True, True),
        ("workshop-03", "social", None, "2016-06-15", "Oak Library", "Campus Network", "", False, True, True),
        ("workshop-04", "social", "2017-10-10", None, "Oak Library", "Campus Network", "Workshop Crew", False, True, True),
        ("workshop-withdrawn", "social", "2017-10-10", None, "Oak Library", "Campus Network", "Workshop Crew", True, False, True),
        ("workshop-ineligible", "social", "2017-10-10", None, "Oak Library", "Campus Network", "Workshop Crew", True, True, False),
    ]
    return [dict(zip(("record_id", "dataset", "source_day", "selected_inference", "sponsor", "publisher", "account", "retrievable", "active", "countable"), row)) for row in rows]


def independent_members(filters):
    """Enumerate explicit memberships, using date objects instead of SQL text."""
    output = []
    for row in fixture_rows():
        if not row["active"] or row["countable"] is not True:
            continue
        if filters.dataset != "all" and row["dataset"] != filters.dataset:
            continue
        if filters.record_ids and row["record_id"] not in filters.record_ids:
            continue
        if any(values and (row[field] or "(Unknown)") not in values for field, values in (
            ("publisher", filters.publishers), ("sponsor", filters.sponsors), ("account", filters.accounts),
        )):
            continue
        day = row["source_day"] or (row["selected_inference"] if filters.include_inferred_dates else None)
        parsed_day = date.fromisoformat(day) if day else None
        if filters.date_presence == "known" and parsed_day is None:
            continue
        if filters.date_presence == "missing" and parsed_day is not None:
            continue
        if parsed_day is None:
            if not filters.include_unknown_dates:
                continue
        elif (filters.date_from and parsed_day < filters.date_from) or (filters.date_to and parsed_day > filters.date_to):
            continue
        output.append({**row, "effective_day": parsed_day})
    return output


@pytest.fixture(scope="module")
def independent_db():
    dsn = configured_test_url()
    schema = "obs_sql_statistics_" + uuid4().hex

    class ScopedDatabase(Database):
        def connect(self, vector=False):
            assert vector is False
            return verified_test_connection(self.url, schema)

    db = ScopedDatabase(dsn)
    with verified_test_connection(dsn) as conn:
        conn.execute("CREATE SCHEMA " + schema)
    db.initialize()
    current_records = []
    for row in fixture_rows():
        if row["countable"] is None:
            continue  # The production import contract rejects unknown eligibility.
        current_records.append(RecordInput(
            record_id=row["record_id"], dataset=row["dataset"],
            title=row["record_id"], url="https://example.invalid/" + row["record_id"],
            published_at=date.fromisoformat(row["source_day"]) if row["source_day"] else None,
            sponsor=row["sponsor"] or "", publisher=row["publisher"] or "", account=row["account"] or "",
            platform="bulletin", keyword="equipment", body="A synthetic school equipment or workshop notice.",
            retrievable=row["retrievable"], countable=row["countable"],
            raw={"preserved_observations": [{"id": "copy-a"}, {"id": "copy-b"}, {"id": "copy-c"}]},
            annotations=[{"version": "claims-calibrated", "labels": ["equipment", "equipment"]}] if row["record_id"] == "school-01" else [],
        ))
    # Import an old version first, then the current snapshot through the real path.
    old = current_records[0].model_copy(update={"published_at": date(2018, 1, 1), "sponsor": "Obsolete Office", "annotations": [{"version": "claims-calibrated", "labels": ["obsolete_unit"]}]})
    db.import_batch(ImportBatch(records=[old]))
    db.import_batch(ImportBatch(records=current_records))
    with db.connect() as conn:
        versions = {row["record_id"]: row["current_version"] for row in conn.execute("SELECT record_id,current_version FROM records").fetchall()}
        for row in fixture_rows():
            if row["countable"] is None:
                continue
            version_id = versions[row["record_id"]]
            if not row["active"]:
                conn.execute("UPDATE records SET active=false WHERE record_id=%s", (row["record_id"],))
            if row["selected_inference"]:
                tier = "A" if row["dataset"] == "social" else "B"
                method = "url_path" if tier == "A" else "archive_first_capture"
                conn.execute("INSERT INTO date_inferences(inference_id,record_id,version_id,method,tier,inferred_date,precision,review_state,evidence) VALUES(%s,%s,%s,%s,%s,%s,'day','unreviewed',%s)", ("independent-" + row["record_id"], row["record_id"], version_id, method, tier, date.fromisoformat(row["selected_inference"]), Jsonb({})))
            if row["record_id"] == "school-05":
                conn.execute("INSERT INTO date_inferences(inference_id,record_id,version_id,method,tier,inferred_date,precision,review_state,evidence) VALUES(%s,%s,%s,'url_path','A','2016-03-01','day','rejected','{}'::jsonb)", ("independent-rejected", row["record_id"], version_id))
    db.audit_schema = schema
    db.audit_records = {record.record_id: record for record in current_records}
    report = Path(__file__).resolve().parents[1] / ".runtime/sql_accuracy_20261007/statistics_audit"
    report.mkdir(parents=True, exist_ok=True)
    (report / ("schema_" + schema + ".json")).write_text(json.dumps({
        "schema": schema, "synthetic_only": True, "preserved": True,
        "eligible_records": {"native": 5, "social": 4}, "source_path": str(Path(__file__).resolve()),
    }, indent=2), encoding="utf-8")
    return db  # Preserve the owned private schema for review.


def test_independent_fixture_hand_totals():
    # Five current eligible school records, four workshop records; six source dates.
    rows = independent_members(Filters(dataset="all"))
    assert Counter(row["dataset"] for row in rows) == {"native": 5, "social": 4}
    assert sum(row["effective_day"] is None for row in rows) == 3
    assert Counter(row["effective_day"].year for row in rows if row["effective_day"]) == {2016: 3, 2017: 3}


@pytest.mark.integration
@pytest.mark.parametrize("filters", [
    Filters(dataset="all"),
    Filters(dataset="all", include_inferred_dates=True),
    Filters(dataset="native", date_from=date(2016, 1, 1), date_to=date(2016, 12, 31)),
    Filters(dataset="all", date_from=date(2016, 1, 1), date_to=date(2016, 12, 31), include_unknown_dates=False),
    Filters(dataset="all", date_presence="missing"),
    Filters(dataset="all", date_presence="missing", include_inferred_dates=True),
    Filters(dataset="all", date_presence="known", include_inferred_dates=True),
    Filters(dataset="native", publishers=["(Unknown)"]),
    Filters(dataset="all", sponsors=["Compass Desk"]),
    Filters(dataset="social", accounts=["(Unknown)"]),
])
def test_sql_membership_totals_and_date_groups_match_independent_enumeration(independent_db, filters):
    members = independent_members(filters)
    expected_ids = {row["record_id"] for row in members}
    actual = independent_db.research_statistics(filters)
    assert {row["record_id"] for row in actual["records"]} == expected_ids
    for collection in actual["collections"]:
        selected = [row for row in members if row["dataset"] == collection["dataset"]]
        assert collection["total"] == len(selected)
        assert collection["retrievable"] == sum(row["retrievable"] for row in selected)
        assert collection["unknown_dates"] == sum(row["effective_day"] is None for row in selected)
        assert collection["source_unknown_dates"] == sum(not row["source_day"] for row in selected)
    expected_groups = Counter((row["dataset"], str(row["effective_day"].year)) for row in members if row["effective_day"])
    assert {(row["dataset"], row["name"]): row["count"] for row in actual["groups"]} == dict(expected_groups)


@pytest.mark.integration
def test_highest_year_returns_every_tie_per_collection(independent_db):
    result = independent_db.research_statistics(Filters(dataset="all", include_inferred_dates=True), ranking="highest")
    # Native 2016/2017 each 2; social 2016/2017 each 2 after declared inferences.
    assert {(row["dataset"], row["name"], row["count"]) for row in result["groups"]} == {
        ("native", "2016", 2), ("native", "2017", 2), ("social", "2016", 2), ("social", "2017", 2),
    }


@pytest.mark.integration
def test_named_periods_are_inclusive_and_exclude_unknown_dates(independent_db):
    periods = [{"label": str(year), "filters": Filters(dataset="all", date_from=date(year, 1, 1), date_to=date(year, 12, 31), include_unknown_dates=False)} for year in (2016, 2017)]
    result = independent_db.research_statistics(Filters(dataset="all"), group_by="none", periods=periods)
    assert [{row["dataset"]: row["total"] for row in period["collections"]} for period in result["periods"]] == [{"native": 2, "social": 1}, {"native": 1, "social": 2}]


@pytest.mark.integration
def test_dashboard_null_groups_accounts_and_label_duplicates_keep_record_denominators(independent_db):
    result = independent_db.dashboard(Filters(dataset="all"))
    stats = result["stats"]
    assert stats["total"] == 9 and stats["unknown_dates"] == 3
    assert {row["name"]: row["count"] for row in stats["accounts"]} == {"Campus Service": 1, "Workshop Crew": 1, "(Unknown)": 2}
    assert {row["name"]: row["percent"] for row in stats["accounts"]} == {"Campus Service": 25, "Workshop Crew": 25, "(Unknown)": 50}
    assert result["labels"]["total"] == 5
    assert result["labels"]["items"] == [{"name": "equipment", "count": 1, "percent": 20}]
    assert result["social_historical_labels"]["total"] == 4
    assert result["social_historical_labels"]["unknown_annotation_records"] == 4
    assert all(item["unknown"] == 4 and item["source_true"] == item["source_false"] == 0 for item in result["social_historical_labels"]["items"])


def share_result(db, numerator, denominator):
    service = Service(SimpleNamespace(show_source_links=True), db=db, rag=object())
    service.health = lambda: {"status": "ok", "countable_record_counts": {"native": 5, "social": 4}}
    plan = SimpleNamespace(filters=numerator, denominator_filters=denominator, group_by=None, scope_notes=[], trusted_filters=denominator)
    return service._share_statistics_answer(plan).structured_result


@pytest.mark.integration
def test_share_keeps_native_and_social_denominators_separate(independent_db):
    result = share_result(independent_db, Filters(dataset="all", sponsors=["Compass Desk"]), Filters(dataset="all"))
    assert [(row["dataset"], row["numerator"], row["denominator"], row["percentage"]) for row in result["collections"]] == [("native", 2, 5, 40), ("social", 2, 4, 50)]


@pytest.mark.integration
def test_share_missing_date_count_uses_the_selected_effective_basis(independent_db):
    filters = Filters(dataset="native", record_ids=["school-04"], include_inferred_dates=True)
    result = share_result(independent_db, filters, filters)
    assert result["collections"][0]["unknown_dates"] == 0


@pytest.mark.integration
def test_share_examples_follow_selected_effective_date_order(independent_db):
    filters = Filters(dataset="native", include_inferred_dates=True)
    result = share_result(independent_db, filters, filters)
    assert [row["record_id"] for row in result["records"]] == ["school-04", "school-03", "school-02", "school-01", "school-05"]


@pytest.mark.integration
def test_label_filter_and_current_projection_have_consistent_support(independent_db):
    record = independent_db.audit_records["school-01"].model_copy(update={"annotations": [
        {"version": "claims-calibrated", "labels": ["equipment", "equipment"]},
        {"version": "claims-calibrated", "labels": ["routine_schedule"]},
    ]})
    independent_db.import_batch(ImportBatch(records=[record]))
    result = independent_db.dashboard(Filters(dataset="native", labels=["routine_schedule"]))
    assert result["stats"]["total"] == 1
    assert {item["name"]: item["count"] for item in result["labels"]["items"]} == {"equipment": 1, "routine_schedule": 1}


@pytest.mark.integration
@pytest.mark.parametrize("record_id,malformed", [("school-02", {"equipment": True}), ("school-03", "equipment")])
def test_nonarray_native_labels_do_not_match_record_filters(independent_db, record_id, malformed):
    record = independent_db.audit_records[record_id].model_copy(update={"annotations": [
        {"version": "claims-calibrated", "labels": malformed},
    ]})
    independent_db.import_batch(ImportBatch(records=[record]))
    # Only school-01 has a real string label array containing equipment.
    rows = independent_db.public_rows(Filters(dataset="native", labels=["equipment"], record_ids=["school-01", record_id]))
    assert {row["record_id"] for row in rows} == {"school-01"}


@pytest.mark.integration
def test_native_label_filter_does_not_count_social_annotation_objects(independent_db):
    record = independent_db.audit_records["workshop-04"].model_copy(update={"annotations": [
        {"version": "claims-calibrated", "labels": ["equipment"]},
    ]})
    independent_db.import_batch(ImportBatch(records=[record]))
    rows = independent_db.public_rows(Filters(dataset="all", labels=["equipment"], record_ids=["school-01", "workshop-04"]))
    assert {row["record_id"] for row in rows} == {"school-01"}


@pytest.mark.integration
def test_date_inference_membership_change_invalidates_the_statistics_fingerprint(independent_db):
    record_id = "inference-race-school"
    independent_db.import_batch(ImportBatch(records=[RecordInput(
        record_id=record_id, dataset="native", title="School workshop date notice",
        url="https://example.invalid/2019/05/04/school-workshop", publisher="School Herald",
        sponsor="Compass Desk", published_at=None, countable=True, retrievable=False,
        body="An independent school workshop notice with an undated source field.",
    )]))
    with independent_db.connect() as conn:
        proposals = [item for item in propose_url_inferences(conn) if item["record_id"] == record_id]
        assert len(proposals) == 1 and proposals[0]["inferred_date"] == date(2019, 5, 4)
        assert apply_inferences(conn, proposals) == 1
    filters = Filters(dataset="native", record_ids=[record_id], date_from=date(2019, 5, 4),
                      date_to=date(2019, 5, 4), include_unknown_dates=False, include_inferred_dates=True)
    before_health = independent_db.health()
    before_members = [row["record_id"] for row in independent_db.public_rows(filters)]
    with independent_db.connect() as conn:
        changed = conn.execute("UPDATE date_inferences SET review_state='rejected' WHERE inference_id=%s",
                               (proposals[0]["inference_id"],)).rowcount
        assert changed == 1
    after_members = [row["record_id"] for row in independent_db.public_rows(filters)]
    after_health = independent_db.health()
    report = Path(__file__).resolve().parents[1] / ".runtime/sql_accuracy_20261007/statistics_audit/fingerprint"
    report.mkdir(parents=True, exist_ok=True)
    (report / (independent_db.audit_schema + ".json")).write_text(json.dumps({
        "synthetic_only": True, "schema": independent_db.audit_schema,
        "event": "URL-proposed day inference changes unreviewed to rejected in an owned private schema",
        "before_members": before_members, "after_members": after_members,
        "before_health": before_health, "after_health": after_health,
        "actual_concurrent_threads_tested": False,
        "scope": "Sequential committed change between guard snapshots; not a threaded stress test",
    }, indent=2), encoding="utf-8")
    assert before_members == [record_id] and after_members == []
    assert before_health["data_version"] == after_health["data_version"]
    assert before_health["statistics_version"] != after_health["statistics_version"], "SQL eligibility changed but statistics fingerprint did not"


@pytest.mark.integration
def test_actual_statistics_tool_rejects_an_inference_change_between_guards(independent_db, monkeypatch):
    record_id = "inference-drift-workshop"
    independent_db.import_batch(ImportBatch(records=[RecordInput(
        record_id=record_id, dataset="native", title="Workshop date notice",
        url="https://example.invalid/2019/05/04/workshop-date", publisher="School Herald",
        sponsor="Compass Desk", published_at=None, countable=True, retrievable=False,
        body="An independent workshop notice with a source date gap.",
    )]))
    with independent_db.connect() as conn:
        proposals = [item for item in propose_url_inferences(conn) if item["record_id"] == record_id]
        assert len(proposals) == 1 and apply_inferences(conn, proposals) == 1
    filters = Filters(dataset="native", record_ids=[record_id], date_from=date(2019, 5, 4),
                      date_to=date(2019, 5, 4), include_unknown_dates=False, include_inferred_dates=True)
    service = Service(SimpleNamespace(show_source_links=True), db=independent_db, rag=object())
    snapshots = []
    sql_results = []
    statistics_read = independent_db.research_statistics

    def capture_real_statistics(*args, **kwargs):
        result = statistics_read(*args, **kwargs)
        sql_results.append(result)
        return result

    monkeypatch.setattr(independent_db, "research_statistics", capture_real_statistics)

    def health_with_controlled_change():
        if len(snapshots) == 1:
            with independent_db.connect() as conn:
                assert conn.execute("UPDATE date_inferences SET review_state='rejected' WHERE inference_id=%s",
                                    (proposals[0]["inference_id"],)).rowcount == 1
        snapshot = independent_db.health()
        snapshots.append(snapshot)
        return snapshot

    service.health = health_with_controlled_change
    result = ToolCatalog(service, filters).call("record_statistics", {"group_by": "years"})
    assert len(sql_results) == 1 and sql_results[0]["collections"][0]["total"] == 1
    assert len(snapshots) >= 2
    assert snapshots[0]["data_version"] == snapshots[-1]["data_version"]
    assert snapshots[0]["statistics_version"] != snapshots[-1]["statistics_version"]
    assert result["status"] == "unavailable"
    assert "changed" in result["message"].casefold()
    report = Path(__file__).resolve().parents[1] / ".runtime/sql_accuracy_20261007/statistics_audit/fingerprint"
    report.mkdir(parents=True, exist_ok=True)
    (report / ("tool_" + independent_db.audit_schema + ".json")).write_text(json.dumps({
        "synthetic_only": True, "schema": independent_db.audit_schema,
        "actual_SQL_result_before_change": sql_results[0], "controlled_health_snapshots": snapshots,
        "actual_tool_result": result, "threaded_stress_tested": False,
    }, indent=2), encoding="utf-8")
