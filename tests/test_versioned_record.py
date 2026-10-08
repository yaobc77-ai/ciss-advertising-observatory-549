"""Offline SQL-projection checks; no PostgreSQL connection or source import."""

from datetime import date

import pytest

from observatory.db import Database
from observatory.models import Filters
from observatory.social_annotations import social_state_id


class Connection:
    def __init__(self, row):
        self.row = row
        self.reads = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, sql, params):
        self.reads.append((sql, params))
        return self

    def fetchone(self):
        return self.row


@pytest.mark.parametrize("dataset", ["native", "social", "all"])
def test_current_source_is_one_parameterized_read_with_all_trusted_filters(dataset, monkeypatch):
    db = Database("")
    row = {"record_id": "junkipedia:42", "version_id": "v-current", "body": "Unchanged text."}
    connection = Connection(row)
    monkeypatch.setattr(db, "connect", lambda: connection)
    record_id = "junkipedia:42"
    selected_label = social_state_id("green_binary", "source_true") if dataset == "social" else "green_binary"
    filters = Filters(dataset=dataset, record_ids=[record_id, "other"], publishers=["outlet"],
                      sponsors=["company"], platforms=["Twitter"], keywords=["energy"],
                      labels=[selected_label], date_from=date(2020, 1, 1), date_to=date(2020, 12, 31),
                      include_unknown_dates=False, date_presence="known")
    original = filters.model_copy(deep=True)
    assert db.versioned_record(filters, record_id) is row
    assert filters == original
    assert len(connection.reads) == 1
    sql, params = connection.reads[0]
    assert "r.active" in sql and "(v.payload->>'countable')::boolean" in sql
    assert "v.version_id=r.current_version" in sql
    assert "v.version_id=p.version_id AND v.record_id=p.record_id" in sql
    assert "r.record_id=ANY(%s)" in sql and [record_id] in params
    assert "r.dataset=%s" in sql if dataset != "all" else "r.dataset=%s" not in sql
    for value in (["outlet"], ["company"], ["Twitter"], ["energy"], [selected_label],
                  date(2020, 1, 1), date(2020, 12, 31)):
        assert value in params
    assert "IS NOT NULL" in sql
    assert "'claims-calibrated'" in sql
    if dataset == "social":
        assert "->'states') ?| %s" in sql
    assert "retrieval_ranges" in sql and "retrieval_end" in sql
    assert "v.payload AS" not in sql and "v.payload->'raw'" not in sql
    assert "v.payload->'provenance'" not in sql and "disclosure" not in sql
    assert "{raw,sponsor_basis}" in sql
    assert "company_affiliation_not_verified_paid_sponsor" in sql


def test_outside_selected_ids_performs_no_read_and_missing_record_returns_none(monkeypatch):
    db = Database("")
    connection = Connection(None)
    monkeypatch.setattr(db, "connect", lambda: connection)
    assert db.versioned_record(Filters(dataset="social", record_ids=["allowed"]), "excluded") is None
    assert connection.reads == []
    assert db.versioned_record(Filters(dataset="social"), "missing") is None
    assert len(connection.reads) == 1


def test_record_identifier_and_source_names_cannot_enter_sql_text(monkeypatch):
    db = Database("")
    connection = Connection(None)
    monkeypatch.setattr(db, "connect", lambda: connection)
    supplied = "x' OR true --"
    assert db.versioned_record(Filters(dataset="all", sponsors=[supplied]), supplied) is None
    sql, params = connection.reads[0]
    assert supplied not in sql and params.count([supplied]) == 2


@pytest.mark.parametrize("inferred", [False, True])
@pytest.mark.parametrize("presence", ["any", "known", "missing"])
def test_versioned_source_uses_existing_date_basis_and_missing_date_predicates(inferred, presence, monkeypatch):
    db = Database("")
    connection = Connection(None)
    monkeypatch.setattr(db, "connect", lambda: connection)
    filters = Filters(dataset="social", include_inferred_dates=inferred, date_presence=presence,
                      date_from=date(2020, 1, 1), include_unknown_dates=True)
    db.versioned_record(filters, "post")
    sql, _ = connection.reads[0]
    where, _ = Database.where(filters.model_copy(update={"record_ids": ["post"]}))
    assert "WHERE " + where in sql
    assert filters.include_inferred_dates is inferred
