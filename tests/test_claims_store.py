import copy
import hashlib

import pytest

from observatory.claims_store import ClaimsStore, _source_check
from observatory.db import Database
from observatory.models import Filters


def original():
    body = "Opening. The project could reduce emissions. Ending."
    start = body.index("The project")
    end = body.index(" Ending.")
    source = {"record_id": "r1", "dataset": "native", "current_version": "v1", "active": True,
              "body": body, "body_hash": hashlib.sha256(body.encode()).hexdigest(),
              "payload": {"record_id": "r1", "dataset": "native", "retrievable": True, "countable": True}}
    publication = {"record_id": "r1", "version_id": "v1", "body_hash": source["body_hash"],
                   "evidence": {"start": start, "end": end, "quote": body[start:end]}}
    return source, publication


def test_source_validation_preserves_literal_quote():
    source, publication = original()
    before = copy.deepcopy(source)
    _source_check(source, publication, "native")
    assert source == before


@pytest.mark.parametrize("changes", [
    {"dataset": "social"}, {"record_id": "other"}, {"current_version": "v2"},
    {"active": False}, {"body_hash": "0" * 64}, {"body": "Changed content"},
])
def test_source_identity_and_version_changes_reject_publication(changes):
    source, publication = original()
    source.update(changes)
    with pytest.raises(ValueError):
        _source_check(source, publication, "native")


@pytest.mark.parametrize("changes", [
    {"retrievable": False}, {"countable": False}, {"record_id": "other"},
    {"retrieval_end": 10}, {"retrieval_ranges": [[0, 8], [45, 52]]},
])
def test_source_scope_and_payload_changes_reject_publication(changes):
    source, publication = original()
    source["payload"].update(changes)
    with pytest.raises(ValueError):
        _source_check(source, publication, "native")


@pytest.mark.parametrize("changes", [
    {"start": True}, {"start": -1}, {"end": 10000}, {"quote": "Invented"},
])
def test_quote_changes_are_not_repaired(changes):
    source, publication = original()
    publication["evidence"].update(changes)
    with pytest.raises(ValueError):
        _source_check(source, publication, "native")


class NoConnection:
    _page_bounds = staticmethod(Database._page_bounds)
    where = staticmethod(Database.where)

    def connect(self):
        raise AssertionError("Invalid selection must fail before opening a database")


@pytest.mark.parametrize("kwargs", [
    {"nc_ids": ["SC_1"]}, {"nc_ids": ["NC_1 OR 1=1"]}, {"sc_ids": [True]},
    {"taxonomy": "../file"}, {"review_state": "fact_checked"}, {"offset": -1},
])
def test_public_query_rejects_invalid_typed_selections(kwargs):
    with pytest.raises(ValueError):
        ClaimsStore(NoConnection()).matches(Filters(), **kwargs)


@pytest.mark.parametrize("kwargs", [
    {"candidate_key": "unknown", "reviewer": "Reviewer", "reason": "Correction", "reviewed_at": "2026-09-30T00:00:00Z"},
    {"candidate_key": "1" * 64, "reviewer": "", "reason": "Correction", "reviewed_at": "2026-09-30T00:00:00Z"},
    {"candidate_key": "1" * 64, "reviewer": "Reviewer", "reason": "", "reviewed_at": "2026-09-30T00:00:00Z"},
    {"candidate_key": "1" * 64, "reviewer": "Reviewer", "reason": "Correction", "reviewed_at": "2026-09-30T00:00:00"},
])
def test_retraction_requires_an_explicit_review_record(kwargs):
    with pytest.raises(ValueError):
        ClaimsStore(NoConnection()).retract(**kwargs)
