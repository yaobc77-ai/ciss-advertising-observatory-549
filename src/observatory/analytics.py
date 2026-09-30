"""Deterministic views of the currently filtered records; no inferred themes.

Sponsor names here are display aliases only. They do not change source values,
merge entities, or decide which sponsor types belong in a client's company count.
"""

from __future__ import annotations

import csv
from collections import Counter
from datetime import date
from io import StringIO

UNKNOWN = "(Unknown)"

_SPONSOR_NAMES = {
    "totalenergies": "TotalEnergies",
    "exxonmobil": "ExxonMobil",
    "api": "API",
    "afpm": "AFPM",
    "ptt": "PTT",
    "southern company": "Southern Company",
    "cera": "CERAWeek",
    "statoil": "Statoil",
    "petronas": "PETRONAS",
    "bp": "BP",
    "shell": "Shell",
    "chevron": "Chevron",
    "enbridge": "Enbridge",
    "the williams companies, inc.": "The Williams Companies, Inc.",
    "williams": "Williams",
    "williams companies": "Williams Companies",
    "nextera energy": "NextEra Energy",
    "eni": "Eni",
    "exelon": "Exelon",
}

CERA_NOTE = (
    "CERA is displayed as CERAWeek, a conference/event, not an energy company. "
    "It remains a separate source sponsor category; inclusion in company-level "
    "analysis awaits client confirmation."
)
LABEL_NOTE = (
    "Historical automated CLAIMS labels, not verified themes or factual verdicts. "
    "A record can have several labels, so label counts and percentages do not "
    "sum to the record total. No labels does not establish that a theme is absent; "
    "the current data cannot distinguish missing annotation from no positive labels."
)


def _value(value):
    return str(value).strip() if value is not None and str(value).strip() else UNKNOWN


def sponsor_display(value):
    """Return a display label while preserving spelling for unrecognized names."""
    value = _value(value)
    return _SPONSOR_NAMES.get(value.casefold(), value)


def sponsor_metadata(value):
    """Expose provisional scope explicitly without relabeling stored entities."""
    value = _value(value)
    key = value.casefold()
    entity_type = (
        "conference/event"
        if key == "cera"
        else "industry association"
        if key in {"api", "afpm"}
        else "unknown"
        if value == UNKNOWN
        else "sponsor"
    )
    return {
        "value": value,
        "label": sponsor_display(value),
        "entity_type": entity_type,
        "note": CERA_NOTE if key == "cera" else "",
    }


def _current_records(rows):
    """Count one current row per record; reject conflicting duplicate versions."""
    seen = {}
    result = []
    for row in rows:
        identity = (row.get("dataset"), row.get("record_id"))
        if row.get("record_id") and identity in seen:
            if seen[identity] != row:
                raise ValueError("Conflicting rows for the same current record")
            continue
        if row.get("record_id"):
            seen[identity] = row
        result.append(row)
    return result


def sponsor_publisher_matrix(rows):
    """All filtered sponsors × outlets, including zeroes and marginal totals.

    ``matrix`` is ordered by ``sponsors`` (metadata dictionaries) and publisher
    strings. ``table_columns`` / ``table_rows`` are ready for AG Grid. The final
    table row is a totals row; ``is_total`` can style it. Unknown sponsors remain
    visible. Source aliases such as Williams and Williams Companies stay separate.
    """
    rows = _current_records(rows)
    counts = Counter(
        (_value(row.get("sponsor")), _value(row.get("publisher"))) for row in rows
    )
    return sponsor_publisher_matrix_from_counts(
        {"sponsor": sponsor, "publisher": publisher, "count": count}
        for (sponsor, publisher), count in counts.items()
    )


def sponsor_publisher_matrix_from_counts(relationships):
    """Build the same matrix from SQL aggregates without expanding record rows."""
    counts = Counter()
    for row in relationships:
        count = row["count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("Relationship counts must be nonnegative integers")
        if count:
            counts[(_value(row.get("sponsor")), _value(row.get("publisher")))] += count
    total = sum(counts.values())
    sponsor_totals = Counter()
    publisher_totals = Counter()
    for (sponsor, publisher), count in counts.items():
        sponsor_totals[sponsor] += count
        publisher_totals[publisher] += count
    sponsor_values = sorted(
        sponsor_totals,
        key=lambda value: (
            -sponsor_totals[value],
            sponsor_display(value).casefold(),
            value,
        ),
    )
    publishers = sorted(
        publisher_totals,
        key=lambda value: (-publisher_totals[value], value.casefold(), value),
    )
    sponsors = [sponsor_metadata(value) for value in sponsor_values]
    matrix = [
        [counts[(sponsor, outlet)] for outlet in publishers]
        for sponsor in sponsor_values
    ]
    outlet_fields = [f"outlet_{index}" for index in range(len(publishers))]
    columns = [
        {"field": "sponsor_display", "headerName": "Sponsor / organization"},
        {"field": "entity_type", "headerName": "Entity type"},
        *[
            {"field": field, "headerName": outlet}
            for field, outlet in zip(outlet_fields, publishers, strict=True)
        ],
        {"field": "total", "headerName": "Total"},
    ]
    table = [
        {
            "sponsor": sponsor["value"],
            "sponsor_display": sponsor["label"],
            "entity_type": sponsor["entity_type"],
            "note": sponsor["note"],
            **dict(zip(outlet_fields, values, strict=True)),
            "total": sum(values),
            "is_total": False,
        }
        for sponsor, values in zip(sponsors, matrix, strict=True)
    ]
    table.append(
        {
            "sponsor": "",
            "sponsor_display": "Total",
            "entity_type": "",
            "note": "",
            **dict(
                zip(
                    outlet_fields,
                    (publisher_totals[p] for p in publishers),
                    strict=True,
                )
            ),
            "total": total,
            "is_total": True,
        }
    )
    return {
        "publishers": publishers,
        "sponsors": sponsors,
        "matrix": matrix,
        "row_totals": [sponsor_totals[value] for value in sponsor_values],
        "column_totals": [publisher_totals[value] for value in publishers],
        "total": total,
        "table_columns": columns,
        "table_rows": table,
        "notes": [CERA_NOTE]
        if any(value.casefold() == "cera" for value in sponsor_values)
        else [],
    }


def csv_safe_cell(value):
    """Keep source-controlled labels, including headers, from becoming formulas."""
    if isinstance(value, str):
        candidate = value.lstrip()
        if candidate.startswith(("=", "+", "-", "@")) or value.startswith(
            ("\t", "\r", "\n")
        ):
            return "'" + value
    return value


def sponsor_publisher_csv(rows):
    """Export the full matrix with source keys and an explicit CERA scope note."""
    result = sponsor_publisher_matrix(rows)
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            csv_safe_cell(value)
            for value in [
                "Sponsor / organization",
                "Source sponsor value",
                "Entity type",
                "Scope note",
                *result["publishers"],
                "Total",
            ]
        ]
    )
    fields = [f"outlet_{index}" for index in range(len(result["publishers"]))]
    for row in result["table_rows"]:
        writer.writerow(
            [
                csv_safe_cell(value)
                for value in [
                    row["sponsor_display"],
                    row["sponsor"],
                    row["entity_type"],
                    row["note"],
                    *(row[field] for field in fields),
                    row["total"],
                ]
            ]
        )
    return output.getvalue()


def historical_label_distribution(rows):
    """Count each historical label once per record with its actual denominator."""
    rows = _current_records(rows)
    counts = Counter()
    labeled = 0
    for row in rows:
        # The public schema has list[str]; missing values mean no recorded labels.
        labels = {
            str(label).strip()
            for label in row.get("labels") or []
            if str(label).strip()
        }
        labeled += bool(labels)
        counts.update(labels)
    total = len(rows)
    return {
        "items": [
            {
                "name": label,
                "count": count,
                "percent": 100 * count / total if total else 0,
            }
            for label, count in sorted(
                counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "total": total,
        "labeled_records": labeled,
        "unlabeled_records": total - labeled,
        "note": LABEL_NOTE,
    }


def yearly_timeline(rows):
    """Yearly article counts plus an explicit unknown/invalid date bucket."""
    counts = Counter()
    for row in _current_records(rows):
        value = row.get("date")
        try:
            year = str(date.fromisoformat(str(value)).year)
        except (ValueError, TypeError):
            year = "Unknown"
        counts[year] += 1
    return [{"year": year, "count": counts[year]} for year in sorted(counts)]
