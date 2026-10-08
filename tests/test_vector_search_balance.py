"""Retrieval ranks each collection separately and keeps narrow scopes exact."""

from contextlib import nullcontext

from observatory.db import Database
from observatory.models import Filters


def row(record, dataset, n=0):
    return {"evidence_id": f"{record}-{n}", "record_id": record, "version_id": "v1", "dataset": dataset,
            "title": "Fictional", "text": "Harbor ferry timetable.", "start": n * 30, "end": n * 30 + 23,
            "paragraph_ids": ["p1"], "score": 1}


class Rows(list):
    def fetchall(self):
        return self

    def fetchone(self):
        return self[0] if self else None


class Connection:
    """Answers keyword, approximate and exact vector reads per collection."""

    def __init__(self, hits, ann_short=False):
        self.hits, self.ann_short, self.calls = hits, ann_short, []

    def commit(self):
        pass

    def execute(self, sql, params=None):
        self.calls.append(sql)
        if not params or "set_config" in sql:
            return Rows()
        dataset = next((p for p in params if isinstance(p, str) and p in ("native", "social")), None)
        rows = [r for r in self.hits if dataset in (None, r["dataset"])]
        if "WITH ann" in sql:
            return Rows(rows[:3] if self.ann_short else rows[:50])
        return Rows(rows[:50])


def database(connection):
    db = Database("")
    db.connect = lambda **kwargs: nullcontext(connection)
    return db


def test_all_collections_alternate_instead_of_social_crowding_out_native():
    hits = [row(f"s{i}", "social") for i in range(40)] + [row(f"n{i}", "native") for i in range(2)]
    evidence = database(Connection(hits)).search("harbor ferry", Filters(dataset="all"), limit=5)
    assert [e.record_id for e in evidence] == ["n0", "s0", "n1", "s1", "s2"]
    assert {e.dataset for e in evidence} == {"native", "social"}


def test_one_collection_keeps_the_previous_ranked_order():
    hits = [row(f"s{i}", "social") for i in range(8)]
    evidence = database(Connection(hits)).search("harbor ferry", Filters(dataset="social"), limit=5)
    assert [e.record_id for e in evidence] == ["s0", "s1", "s2", "s3", "s4"]
    assert [e.retrieval_rank for e in evidence] == [1, 2, 3, 4, 5]


def test_several_passages_stay_within_selected_records():
    hits = [row("s0", "social", 0), row("s0", "social", 1), row("s1", "social"), row("n0", "native")]
    evidence = database(Connection(hits)).search("harbor ferry", Filters(dataset="all"), limit=2,
                                                 chunks_per_record=2)
    assert [(e.record_id, e.start) for e in evidence] == [("n0", 0), ("s0", 0), ("s0", 30)]


def test_narrow_scope_falls_back_to_exact_vector_scan():
    connection = Connection([row(f"s{i}", "social") for i in range(60)], ann_short=True)
    database(connection).search("harbor ferry", Filters(dataset="social"), vector=[0.1] * 1536)
    vector_reads = [sql for sql in connection.calls if "<=>" in sql]
    assert "WITH ann" in vector_reads[0] and "JOIN embeddings e" in vector_reads[1]


def test_native_scope_is_always_exact():
    connection = Connection([row(f"n{i}", "native") for i in range(3)])
    database(connection).search("harbor ferry", Filters(dataset="native"), vector=[0.1] * 1536)
    assert not any("WITH ann" in sql for sql in connection.calls)
