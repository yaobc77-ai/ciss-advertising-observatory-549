"""Independent catalog envelope checks, using no database, questions or API."""

import math

import pytest

from observatory.models import Filters
from observatory.tool_result_contract import checked_catalog_result, result_schema


def valid():
    return {"tool": "search_records", "status": "ok",
            "filters": Filters().model_dump(mode="json"), "evidence": []}


@pytest.mark.parametrize("change", [
    {"tool": "record_statistics"}, {"status": "success"}, {"status": True},
    {"filters": None}, {"filters": {}}, {"evidence": [math.nan]},
    {"filters": {"dataset": "native", "include_unknown_dates": "true"}},
    {"filters": {"dataset": "native", "include_unknown_dates": 1}},
    {"filters": {"dataset": "native", "unknown_selector": "x"}},
])
def test_invalid_or_foreign_catalog_output_is_rejected(change):
    with pytest.raises((ValueError, TypeError)):
        checked_catalog_result("search_records", {**valid(), **change})


def test_success_requires_effective_filters():
    result = valid()
    del result["filters"]
    with pytest.raises(ValueError, match="effective scope"):
        checked_catalog_result("search_records", result)


def test_correct_result_preserves_payload_without_mutation():
    result = valid()
    assert checked_catalog_result("search_records", result) == result
    checked_catalog_result("search_records", result)["evidence"].append("changed")
    assert result["evidence"] == []


def test_error_envelope_does_not_need_a_fabricated_scope():
    result = {"tool": "search_records", "status": "unavailable", "message": "Unavailable"}
    assert checked_catalog_result("search_records", result) == result


def test_advertised_schema_checks_identity_status_and_success_scope():
    # jsonschema is already installed with the optional MCP SDK in this test environment.
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(result_schema("search_records"))
    validator.check_schema(validator.schema)
    validator.validate(valid())
    invalid = {**valid(), "tool": "record_statistics"}
    assert list(validator.iter_errors(invalid))
    invalid = valid()
    del invalid["filters"]
    assert list(validator.iter_errors(invalid))
