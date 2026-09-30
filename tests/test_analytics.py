import csv
from io import StringIO

import pytest

from observatory.analytics import (
    CERA_NOTE,
    historical_label_distribution,
    sponsor_display,
    sponsor_metadata,
    sponsor_publisher_csv,
    sponsor_publisher_matrix,
    yearly_timeline,
)


def record(record_id, sponsor="exxonmobil", publisher="Forbes", **updates):
    return {
        "record_id": record_id,
        "dataset": "native",
        "version_id": f"v-{record_id}",
        "title": "Example advertisement",
        "sponsor": sponsor,
        "publisher": publisher,
        "keyword": "ExxonMobil",
        "date": "2022-04-05",
        "labels": [],
        "retrievable": True,
        **updates,
    }


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("totalenergies", "TotalEnergies"),
        ("exxonmobil", "ExxonMobil"),
        ("api", "API"),
        ("ptt", "PTT"),
        ("southern company", "Southern Company"),
        ("cera", "CERAWeek"),
        ("unverifiedName", "unverifiedName"),
        (None, "(Unknown)"),
    ],
)
def test_display_aliases_do_not_require_source_edits(source, expected):
    assert sponsor_display(source) == expected


def test_entity_scope_is_explicit_without_claiming_every_sponsor_is_a_company():
    assert sponsor_metadata("cera") == {
        "value": "cera",
        "label": "CERAWeek",
        "entity_type": "conference/event",
        "note": CERA_NOTE,
    }
    assert sponsor_metadata("api")["entity_type"] == "industry association"
    assert sponsor_metadata("New Organization")["entity_type"] == "sponsor"


def test_complete_matrix_reconciles_zeroes_and_totals_with_filtered_rows():
    rows = [
        record("1", "cera", "CNBC"),
        record("2", "exxonmobil", "CNBC"),
        record("3", "exxonmobil", "Forbes"),
        record("4", "", "Forbes"),
    ]
    result = sponsor_publisher_matrix(rows)
    assert result["total"] == 4
    assert result["publishers"] == ["CNBC", "Forbes"]
    assert result["row_totals"] == [2, 1, 1]
    assert result["column_totals"] == [2, 2]
    by_source = {
        sponsor["value"]: cells
        for sponsor, cells in zip(result["sponsors"], result["matrix"], strict=True)
    }
    assert by_source == {"exxonmobil": [1, 1], "cera": [1, 0], "(Unknown)": [0, 1]}
    assert sum(map(sum, result["matrix"])) == len(rows)
    assert result["table_rows"][-1]["total"] == 4
    assert result["table_rows"][-1]["is_total"]
    assert result["notes"] == [CERA_NOTE]
    assert rows[0]["sponsor"] == "cera"


def test_matrix_does_not_truncate_sponsors_or_merge_unreviewed_aliases():
    values = ["williams", "williams companies", "the williams companies, inc."]
    rows = [record(str(index), value) for index, value in enumerate(values)]
    rows.extend(record(f"other-{index}", f"Sponsor {index}") for index in range(12))
    result = sponsor_publisher_matrix(rows)
    assert len(result["sponsors"]) == 15
    assert set(values).issubset({item["value"] for item in result["sponsors"]})
    assert result["total"] == 15
    assert not result["notes"]


def test_export_uses_full_matrix_and_neutralizes_labels_and_header_formulas():
    rows = [
        record("1", "cera", "CNBC"),
        record("2", "=Evil()", "  +Malicious()"),
    ]
    exported = list(csv.reader(StringIO(sponsor_publisher_csv(rows))))
    header, *data = exported
    assert "'+Malicious()" in header
    assert data[-1][0] == "Total" and data[-1][-1] == "2"
    cera = next(row for row in data if row[0] == "CERAWeek")
    assert cera[1:4] == ["cera", "conference/event", CERA_NOTE]
    evil = next(row for row in data if row[0] == "'=Evil()")
    assert evil[1] == "'=Evil()"
    assert all(len(row) == len(header) for row in data)


def test_labels_are_record_counts_with_multilabel_and_missing_denominators():
    label_a = "green_labels.viable_solutions"
    label_b = "ff_labels.infrastructure_and_production"
    rows = [
        record("1", labels=[label_a, label_b, label_a]),
        record("2", labels=[label_b]),
        record("3", labels=[]),
        record("4", labels=None),
    ]
    result = historical_label_distribution(rows)
    assert result["items"] == [
        {"name": label_b, "count": 2, "percent": 50.0},
        {"name": label_a, "count": 1, "percent": 25.0},
    ]
    assert result["total"] == 4
    assert result["labeled_records"] == 2
    assert result["unlabeled_records"] == 2
    assert "cannot distinguish missing annotation" in result["note"]
    assert "ExxonMobil" not in str(result["items"])


def test_yearly_counts_preserve_unknown_dates_and_sum_to_total():
    rows = [
        record("1", date="2022-01-01"),
        record("2", date="2022-12-31"),
        record("3", date="2020-08-03"),
        record("4", date=None),
        record("5", date="(Unknown)"),
        record("6", date="2020-02-31"),
    ]
    result = yearly_timeline(rows)
    assert result == [
        {"year": "2020", "count": 1},
        {"year": "2022", "count": 2},
        {"year": "Unknown", "count": 3},
    ]
    assert sum(item["count"] for item in result) == len(rows)


def test_empty_selection_is_exportable_without_fake_categories():
    matrix = sponsor_publisher_matrix([])
    assert matrix["total"] == 0
    assert matrix["matrix"] == matrix["sponsors"] == matrix["publishers"] == []
    assert matrix["table_rows"][-1]["total"] == 0
    assert list(csv.reader(StringIO(sponsor_publisher_csv([]))))[-1][-1] == "0"
    labels = historical_label_distribution([])
    assert labels["items"] == []
    assert (
        labels["labeled_records"] == labels["unlabeled_records"] == labels["total"] == 0
    )
    assert yearly_timeline([]) == []


@pytest.mark.parametrize(
    "aggregate",
    [sponsor_publisher_matrix, historical_label_distribution, yearly_timeline],
)
def test_identical_join_duplicates_count_record_once_but_version_conflicts_fail(
    aggregate,
):
    row = record("same", labels=["green_labels.green_binary"])
    assert aggregate([row, dict(row)]) == aggregate([row])
    with pytest.raises(ValueError, match="Conflicting rows"):
        aggregate([row, dict(row, version_id="different-version")])
