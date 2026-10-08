"""Service statistics answers count eligible records without evidence/model calls."""

import json
from collections import Counter
from datetime import date
from types import SimpleNamespace

import pytest

from observatory.config import Settings
from observatory.models import Answer, Filters
from observatory.service import Service, summarize

PRIVATE = "private://credential@local/raw-source"
PUBLIC_FIELDS = {
    "record_id", "version_id", "dataset", "title", "date", "publisher", "sponsor",
    "url", "archive_url", "retrievable", "date_basis", "inferred_date", "inferred_tier",
    "account", "platform", "effective_date", "source_date",
}

def record(identity, dataset, publisher, sponsor, *, when="2021-03-04", retrievable=True, **extra):
    return {
        "record_id": identity, "version_id": f"version-{identity}", "dataset": dataset,
        "title": f"Article {identity}", "date": when, "publisher": publisher,
        "sponsor": sponsor, "platform": "", "keyword": "energy", "labels": [],
        "url": "https://example.org/article", "archive_url": "https://archive.org/article",
        "retrievable": retrievable, "body": PRIVATE, "raw": {"secret": PRIVATE},
        "provenance": [{"source": PRIVATE}], "issues": [{"detail": PRIVATE}],
        **extra,
    }


class StatisticsOnlyDB:
    def __init__(self, rows):
        self.rows = rows
        self.facet_calls = []
        self.dashboard_calls = []
        self.forbidden_calls = []
        self.version = "source-v1"

    def health(self):
        return {"status": "ok", "data_version": self.version,
                "record_counts": dict(Counter(row["dataset"] for row in self.rows))}

    def facets(self, dataset):
        self.facet_calls.append(dataset)
        rows = self.rows if dataset == "all" else [row for row in self.rows if row["dataset"] == dataset]
        return {
            "publishers": sorted({row["publisher"] for row in rows}),
            "sponsors": sorted({row["sponsor"] for row in rows}),
        }

    def dashboard(self, filters, offset=0, limit=20, sort_by="date", descending=True):
        self.dashboard_calls.append(filters.model_copy(deep=True))
        rows = []
        for original in self.rows:
            if filters.dataset != "all" and original["dataset"] != filters.dataset:
                continue
            if not original.get("countable", True):
                continue
            if filters.publishers and original["publisher"] not in filters.publishers:
                continue
            if filters.sponsors and original["sponsor"] not in filters.sponsors:
                continue
            if filters.keywords and original["keyword"] not in filters.keywords:
                continue
            if filters.record_ids and original["record_id"] not in filters.record_ids:
                continue
            when = date.fromisoformat(original["date"]) if original["date"] else None
            if when is None and not filters.include_unknown_dates:
                continue
            if when is not None and (
                (filters.date_from and when < filters.date_from)
                or (filters.date_to and when > filters.date_to)
            ):
                continue
            rows.append(dict(original))
        return {"stats": summarize(rows), "page": {"rows": rows[offset:offset + limit]}}

    def save_answer(self, question, filters, result, data_version):
        # Every final answer is written once to the audit log; it is never read back as a cache.
        self.saved_answers = getattr(self, "saved_answers", []) + [result]

    def __getattr__(self, name):
        if name.startswith("__") or name == "saved_answers":
            raise AttributeError(name)
        self.forbidden_calls.append(name)
        raise AssertionError(f"Statistics route must not use {name}")


class NoPaidCalls:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        self.calls.append(name)
        raise AssertionError(f"Statistics route must not use paid {name}")


@pytest.fixture
def rows():
    return [
        record("n1", "native", "The New York Times", "exxonmobil"),
        record("n2", "native", "The New York Times", "totalenergies", retrievable=False),
        record("n3", "native", "The Washington Post", "exxonmobil", when=None),
        record("n4", "native", "The Washington Post", "cera", when="2022-04-02"),
        record("n5", "native", "The New York Times", "exxonmobil", countable=False),
        record("s1", "social", "The New York Times", "exxonmobil"),
        record("s2", "social", "The New York Times", "exxonmobil", retrievable=False),
    ]


def statistics_service(rows, *, links=True):
    db, rag = StatisticsOnlyDB(rows), NoPaidCalls()
    return Service(Settings(show_source_links=links), db=db, rag=rag), db, rag


@pytest.mark.parametrize("question,kind,expected", [
    ("How many native ads are from the New York Times?", "count", 2),
    ("Which publishers is ExxonMobil working with?", "list_publishers", 2),
    ("Which fossil fuel companies has Washington Post worked with?", "list_sponsors", 2),
])
def test_customer_questions_use_complete_record_statistics(question, kind, expected, rows):
    service, db, rag = statistics_service(rows)
    answer = service.answer(question, Filters(), "visitor")
    assert answer.status == "answered"
    assert answer.answer_mode == "statistics"
    assert answer.structured_result["kind"] == kind
    assert answer.structured_result["collections"][0]["total"] == expected
    assert answer.structured_result["data_version"] == db.version
    assert answer.cost_usd == 0
    assert not answer.evidence and not answer.citations
    assert not db.forbidden_calls and not rag.calls
    assert len(db.dashboard_calls) == 1


def test_count_includes_metadata_only_and_excludes_ineligible_records(rows):
    service, db, _ = statistics_service(rows)
    answer = service.answer("How many native ads from NYT?", Filters(), "visitor")
    collection = answer.structured_result["collections"][0]
    assert collection == {"dataset": "native", "total": 2, "retrievable": 1, "unknown_dates": 0}
    assert {row["record_id"] for row in answer.structured_result["records"]} == {"n1", "n2"}
    assert len(db.dashboard_calls) == 1  # Examples and counts came from one snapshot.


def test_list_returns_every_category_and_its_count(rows):
    service, _, _ = statistics_service(rows)
    answer = service.answer("Which publishers is ExxonMobil working with?", Filters(), "visitor")
    assert answer.structured_result["group_by"] == "publishers"
    assert answer.structured_result["groups"] == [
        {"dataset": "native", "name": "The New York Times", "count": 1, "display_name": "The New York Times"},
        {"dataset": "native", "name": "The Washington Post", "count": 1, "display_name": "The Washington Post"},
    ]


def test_group_list_is_not_limited_to_ten_example_records():
    rows = [record(f"n{index}", "native", f"Outlet {index:02}", "exxonmobil") for index in range(13)]
    service, _, _ = statistics_service(rows)
    answer = service.answer("Which publishers is ExxonMobil working with?", Filters(), "visitor")
    result = answer.structured_result
    assert result["collections"][0]["total"] == 13
    assert len(result["records"]) == 10
    assert len(result["groups"]) == 13
    assert sum(group["count"] for group in result["groups"]) == 13


def test_source_sponsor_categories_are_explicit_not_verified_companies(rows):
    service, _, _ = statistics_service(rows)
    answer = service.answer("Which companies has WaPo worked with?", Filters(), "visitor")
    groups = answer.structured_result["groups"]
    assert {group["name"] for group in groups} == {"cera", "exxonmobil"}
    assert {group["display_name"] for group in groups} == {"CERAWeek", "ExxonMobil"}
    assert "source-listed sponsors / organizations" in answer.answer
    assert any("not been independently verified" in note for note in answer.structured_result["scope_notes"])


def test_both_collections_keep_units_and_same_named_groups_separate(rows):
    service, db, rag = statistics_service(rows)
    answer = service.answer("Which publishers is ExxonMobil working with?", Filters(dataset="all"), "visitor")
    assert answer.status == "answered"
    assert answer.structured_result["collections"] == [
        {"dataset": "native", "total": 2, "retrievable": 2, "unknown_dates": 1},
        {"dataset": "social", "total": 2, "retrievable": 1, "unknown_dates": 0},
    ]
    same_named_groups = [group for group in answer.structured_result["groups"] if group["name"] == "The New York Times"]
    assert [(group["dataset"], group["count"]) for group in same_named_groups] == [("native", 1), ("social", 2)]
    assert [scope.dataset for scope in db.dashboard_calls] == ["native", "social"]
    assert "native ad records" in answer.answer and "company social posts" in answer.answer
    assert "social ad records" not in answer.answer
    assert not rag.calls and not db.forbidden_calls


def test_active_entity_and_date_intersections_reach_database(rows):
    service, db, _ = statistics_service(rows)
    scope = Filters(
        publishers=["The New York Times"], sponsors=["exxonmobil", "totalenergies"],
        date_from=date(2021, 2, 1), date_to=date(2023, 1, 1), keywords=["energy"],
    )
    original = scope.model_dump()
    answer = service.answer("How many ads from NYT in 2021?", scope, "visitor")
    assert answer.status == "answered"
    used = db.dashboard_calls[0]
    assert used.publishers == ["The New York Times"]
    assert used.sponsors == scope.sponsors and used.keywords == scope.keywords
    assert used.date_from == date(2021, 2, 1) and used.date_to == date(2021, 12, 31)
    assert used.include_unknown_dates is False
    assert scope.model_dump() == original


def test_explicit_date_excludes_unknown_date_record(rows):
    service, _, _ = statistics_service(rows)
    answer = service.answer("Which publishers is ExxonMobil working with in 2021?", Filters(), "visitor")
    assert answer.structured_result["collections"][0]["total"] == 1
    assert [group["name"] for group in answer.structured_result["groups"]] == ["The New York Times"]


def test_zero_scope_is_an_answer_not_missing_passage_evidence(rows):
    service, db, rag = statistics_service(rows)
    answer = service.answer("How many ads from NYT?", Filters(keywords=["absent-keyword"]), "visitor")
    assert answer.status == "answered"
    assert answer.structured_result["collections"][0]["total"] == 0
    assert not answer.structured_result["records"]
    assert "No eligible records match" in answer.answer
    assert "elsewhere" in answer.answer
    assert not db.forbidden_calls and not rag.calls


@pytest.mark.parametrize("question,scope", [
    ("How many ads from Unlisted Outlet?", Filters()),
    ("How many ads from NYT?", Filters(publishers=["The Washington Post"])),
    ("How many ads from NYT in 2020?", Filters(date_from=date(2021, 1, 1))),
])
def test_clarification_never_queries_statistics_or_falls_back(question, scope, rows):
    service, db, rag = statistics_service(rows)
    answer = service.answer(question, scope, "visitor")
    assert answer.answer_mode == "clarification"
    assert answer.status == "insufficient_evidence"
    assert answer.structured_result is None
    assert not db.dashboard_calls and not db.forbidden_calls and not rag.calls


@pytest.mark.parametrize("failing_method", ["facets", "dashboard"])
def test_database_statistics_failure_is_sanitized_and_has_no_model_fallback(failing_method, rows):
    service, db, rag = statistics_service(rows)

    def fail(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    setattr(db, failing_method, fail)
    answer = service.answer("How many ads from NYT?", Filters(), "visitor")
    assert answer.status == "service_unavailable"
    assert answer.answer_mode == "statistics"
    assert answer.failure_reason == "statistics_unavailable"
    assert answer.structured_result is None
    assert answer.cost_usd == 0
    assert PRIVATE not in answer.model_dump_json()
    assert not db.forbidden_calls and not rag.calls


@pytest.mark.parametrize("links", [True, False])
def test_statistics_public_records_are_whitelisted_and_follow_source_toggle(links, rows):
    rows[0]["archive_url"] = "https://user:password@example.org/private"
    service, _, _ = statistics_service(rows, links=links)
    answer = service.answer("How many ads from NYT?", Filters(), "visitor")
    result = answer.structured_result
    assert PRIVATE not in json.dumps(result)
    assert all(set(row) == PUBLIC_FIELDS for row in result["records"])
    assert result["records"][0]["url"] == ("https://example.org/article" if links else "")
    assert result["records"][0]["archive_url"] == ""
    if not links:
        assert all(not row["url"] and not row["archive_url"] for row in result["records"])


def test_current_selection_count_does_not_require_entity_facets(rows):
    service, db, rag = statistics_service(rows)
    answer = service.answer("How many records match the current filters?", Filters(sponsors=["totalenergies"]), "visitor")
    assert answer.status == "answered"
    assert answer.structured_result["collections"][0]["total"] == 1
    assert not db.facet_calls and not db.forbidden_calls and not rag.calls


@pytest.mark.parametrize("during", ["facets", "dashboard"])
def test_statistics_source_change_discards_old_answer_even_if_total_is_unchanged(rows, during):
    service, db, rag = statistics_service(rows)
    original = getattr(db, during)

    def change(*args, **kwargs):
        result = original(*args, **kwargs)
        db.version = "source-v2"
        return result

    setattr(db, during, change)
    answer = service.answer("How many native ads from NYT?", Filters(), "visitor")
    assert answer.status == "service_unavailable"
    assert answer.failure_reason == "data_changed_during_statistics"
    assert "submit it again" in answer.answer
    assert answer.structured_result is None and not answer.evidence and not answer.citations
    assert not db.forbidden_calls and not rag.calls and answer.cost_usd == 0


@pytest.mark.parametrize("health", [
    {}, {"status": "unavailable", "data_version": "source-v1"},
    {"status": "ok", "data_version": ""}, {"status": "ok", "data_version": "unavailable"},
    {"status": "ok", "data_version": None},
])
def test_statistics_without_healthy_source_identity_cannot_publish(rows, health):
    service, db, rag = statistics_service(rows)
    db.health = lambda: health
    answer = service.answer("How many records?", Filters(), "visitor")
    assert answer.status == "service_unavailable" and answer.structured_result is None
    assert not db.dashboard_calls and not db.facet_calls and not rag.calls


def test_statistics_health_loss_after_query_discards_result(rows):
    service, db, rag = statistics_service(rows)
    health = iter([db.health(), {"status": "unavailable", "data_version": db.version}])
    db.health = lambda: next(health)
    answer = service.answer("How many records?", Filters(), "visitor")
    assert answer.failure_reason == "data_changed_during_statistics"
    assert answer.structured_result is None and not rag.calls


def test_semantic_answer_preserves_paid_evidence_route():
    seen = []

    def record_call(name, value=None):
        seen.append(name)
        return value

    db = SimpleNamespace(
        health=lambda: record_call("health", {"data_version": "stable"}),
        public_rows=lambda _: record_call("public_rows", []),
        search=lambda *args, **kwargs: record_call("search", []),
        save_answer=lambda *args: record_call("save_answer"),
    )
    rag = SimpleNamespace(
        embed=lambda *args, **kwargs: record_call("embed", [[0.0]]),
        generate=lambda *args, **kwargs: record_call("generate", Answer(status="insufficient_evidence", answer="No supporting article was found.")),
        budget=SimpleNamespace(
            reserve=lambda *args: record_call("reserve", "r1"),
            reservation_cost=lambda *args: 0.0,
            cancel_unsent=lambda *args: record_call("cancel"),
        ),
    )
    answer = Service(Settings(), db=db, rag=rag).answer("What does ExxonMobil say about carbon capture?", Filters(), "visitor")
    assert answer.answer_mode == "rag"
    assert answer.structured_result is None
    assert "embed" in seen and "generate" in seen and "save_answer" in seen
    assert "cancel" not in seen
