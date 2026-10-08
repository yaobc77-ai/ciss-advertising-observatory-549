"""Source-column compatibility checks using independent fictional records."""

import pytest

from observatory.original_metadata import metadata_origins_sql, project_record_metadata


def _row(values):
    return {
        "record_id": "fictional:fern-journal",
        "dataset": "native",
        "version_id": "fictional-text-v1",
        "body_hash": "a" * 64,
        "metadata_origins": [{
            "origin_kind": "original_metadata", "priority": 0,
            "provenance": [{"row": 3, "sha256": "b" * 64}],
            "cells": [{
                "field": "disclosure_location", "present": True,
                "value": value, "payload_field_path": ["raw", "metadata", column],
            } for column, value in values.items()],
        }],
    }


def _project(values):
    return project_record_metadata(
        _row(values), ["disclosure_location"], public_url=lambda value: value,
    )


def test_sql_projects_the_original_description_column():
    sql = metadata_origins_sql()
    assert "disclosure description" in sql
    assert "disclosure location" in sql


def test_description_retains_exact_source_wording_and_cell_path():
    result = _project({"disclosure description": "Below the headline; another note in the footer."})
    field = result["original_fields"]["disclosure_location"]
    assert field["status"] == "recorded"
    assert field["value"] == "Below the headline; another note in the footer."
    assert field["payload_field_path"] == ["raw", "metadata", "disclosure description"]
    assert result["source_refs"][0]["version_id"] == "fictional-text-v1"
    assert result["source_refs"][0]["body_hash"] == "a" * 64
    assert result["display_fields"]["disclosure_location"] is None
    assert result["online_truth"] == "not_established"


@pytest.mark.parametrize("blank", [None, "", "  ", "N/A", "unknown"])
def test_missing_alternate_cell_does_not_hide_a_recorded_description(blank):
    result = _project({"disclosure location": blank, "disclosure description": "Above the photograph."})
    field = result["original_fields"]["disclosure_location"]
    assert field["status"] == "recorded"
    assert field["value"] == "Above the photograph."
    assert len(field["stored_sources"]) == 2
    assert len(result["source_refs"]) == 2
    assert result["review_required"] is False


def test_differing_recorded_columns_require_review():
    result = _project({"disclosure location": "At the top.", "disclosure description": "At the bottom."})
    field = result["original_fields"]["disclosure_location"]
    assert field["status"] == "needs_review"
    assert field["value"] is None
    assert result["review_required"] is True


def test_equal_columns_do_not_duplicate_the_selected_value():
    result = _project({"disclosure location": "Under the title.", "disclosure description": "Under the title."})
    field = result["original_fields"]["disclosure_location"]
    assert field["status"] == "recorded"
    assert field["value"] == "Under the title."
    assert len(field["stored_sources"]) == 2


def test_missing_description_is_unknown_not_absent_on_the_page():
    result = _project({"disclosure description": "None"})
    assert result["original_fields"]["disclosure_location"]["status"] == "unknown"
    assert result["online_truth"] == "not_established"


def test_no_position_cells_remains_not_recorded():
    result = _project({})
    assert result["original_fields"]["disclosure_location"]["status"] == "not_recorded"


def test_description_does_not_alias_unrelated_fields():
    result = project_record_metadata(
        _row({"disclosure description": "Footer note."}),
        ["disclosure_language"], public_url=lambda value: value,
    )
    assert result["original_fields"]["disclosure_language"]["status"] == "not_recorded"


def test_unsupported_description_is_not_rendered_as_a_location():
    result = _project({"disclosure description": {"location": "invented"}})
    field = result["original_fields"]["disclosure_location"]
    assert field["status"] == "unsupported_value"
    assert field["value"] is None
