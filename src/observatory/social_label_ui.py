"""Source-state chart and snapshot checks, independent of Dash callbacks."""

import hashlib
import json
import re
import textwrap
from html import escape

import plotly.graph_objects as go

from .social_annotations import (
    SCHEME,
    SOCIAL_STATES,
    STATUS,
    parse_social_state_id,
    social_label_metadata,
    social_state_id,
    social_state_options,
)

STATE_NAMES = {"source_true": "Source True", "source_false": "Source False", "unknown": "Unknown annotation"}
STATE_COLORS = {"source_true": "#003262", "source_false": "#a86b25", "unknown": "#b9c0ca"}


def filter_fingerprint(filters):
    return hashlib.sha256(json.dumps(filters, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def social_label_snapshot(distribution, filters):
    """Reject absent or partial summaries; never invent annotation coverage."""
    if (not isinstance(distribution, dict) or distribution.get("scheme") != SCHEME
            or distribution.get("status") != STATUS
            or not isinstance(distribution.get("source_state_version"), str)
            or re.fullmatch(r"[0-9a-f]{64}", distribution["source_state_version"]) is None):
        raise ValueError("Source-state distribution is unavailable")
    totals = [distribution.get(key) for key in ("total", "valid_annotation_records", "unknown_annotation_records")]
    if any(type(value) is not int or value < 0 for value in totals) or totals[1] + totals[2] != totals[0]:
        raise ValueError("Invalid source annotation coverage")
    source = distribution.get("items")
    metadata = social_label_metadata()
    if not isinstance(source, list) or len(source) != len(metadata):
        raise ValueError("Incomplete source-state distribution")
    indexed = {}
    for item in source:
        if (not isinstance(item, dict) or not isinstance(item.get("key"), str)
                or item["key"] in indexed):
            raise ValueError("Invalid source-state row")
        indexed[item.get("key")] = item
    items = []
    for code in metadata:
        item = indexed.get(code["key"], {})
        counts = [item.get(state) for state in SOCIAL_STATES]
        if (any(type(value) is not int or value < 0 for value in counts)
                or sum(counts) != totals[0] or counts[2] != totals[2]):
            raise ValueError("Invalid source-state counts")
        items.append({**code, **dict(zip(SOCIAL_STATES, counts, strict=True))})
    return {"filters": filters, "scope_fingerprint": filter_fingerprint(filters),
            "source_state_version": distribution["source_state_version"],
            "total": totals[0], "valid_annotation_records": totals[1],
            "unknown_annotation_records": totals[2], "items": items}


def social_label_figure(snapshot, metric="count"):
    figure = go.Figure()
    items = list(reversed(snapshot["items"]))
    total = snapshot["total"]
    for state in SOCIAL_STATES:
        counts = [item[state] for item in items]
        percents = [100 * count / total if total else 0 for count in counts]
        figure.add_bar(
            name=STATE_NAMES[state], y=[item["key"] for item in items],
            x=percents if metric == "percent" else counts, orientation="h",
            marker_color=STATE_COLORS[state],
            customdata=[[social_state_id(item["key"], state), snapshot["scope_fingerprint"],
                         snapshot["source_state_version"], count, percent]
                        for item, count, percent in zip(items, counts, percents, strict=True)],
            hovertemplate="%{y}<br>" + STATE_NAMES[state] + "<br>%{customdata[3]:,} selected records"
                          " · %{customdata[4]:.1f}%<extra></extra>",
        )
    figure.update_layout(template="plotly_white", barmode="stack", height=760,
                         paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                         margin={"l": 16, "r": 18, "t": 45, "b": 45},
                         legend={"orientation": "h", "y": 1.08},
                         font={"family": "Segoe UI, Arial, sans-serif", "size": 13})
    figure.update_xaxes(title="Share of selected records (%)" if metric == "percent" else "Selected records",
                        rangemode="tozero")
    figure.update_yaxes(tickmode="array", tickvals=[item["key"] for item in items],
                        ticktext=["<br>".join(escape(line) for line in textwrap.wrap(
                            f"{item['label']} ({item['level']})", width=34)) for item in items], automargin=True)
    return figure


def social_label_note(snapshot):
    return (f"{snapshot['total']:,} selected posts · {snapshot['valid_annotation_records']:,} with a usable "
            f"historical annotation · {snapshot['unknown_annotation_records']:,} unknown. "
            "Each code uses the same selected-post denominator. Codes can overlap; do not sum their counts. "
            "Source True/False are historical automated outputs, not reviewed themes, greenwashing or fact checks.")


def clicked_social_state(click, snapshot):
    points = click.get("points") if isinstance(click, dict) else None
    if not isinstance(points, list) or len(points) != 1 or not isinstance(points[0], dict):
        raise ValueError("Invalid source-state click")
    point = points[0]
    index, curve = point.get("pointNumber"), point.get("curveNumber")
    items = list(reversed(snapshot["items"]))
    if type(index) is not int or type(curve) is not int or not 0 <= index < len(items) or not 0 <= curve < 3:
        raise ValueError("Invalid source-state point")
    item, state = items[index], SOCIAL_STATES[curve]
    identifier = social_state_id(item["key"], state)
    expected = [identifier, snapshot["scope_fingerprint"], snapshot["source_state_version"],
                item[state], 100 * item[state] / snapshot["total"] if snapshot["total"] else 0]
    if point.get("y") != item["key"] or point.get("customdata") != expected or not item[state]:
        raise ValueError("Source-state click is not a rendered nonempty segment")
    return identifier


def source_state_name(value):
    return next((option["label"] for option in social_state_options() if option["value"] == value), value)


def public_social_states(row):
    """Allowlist the already-public state IDs without reading raw annotations."""
    states = row.get("social_historical_states")
    if (row.get("dataset") != "social" or row.get("social_historical_scheme") != SCHEME
            or row.get("social_historical_status") != STATUS or not isinstance(states, list)
            or len(states) != 13):
        raise ValueError("Historical source states are unavailable for this record")
    found = {}
    for value in states:
        parsed = parse_social_state_id(value)
        if parsed is None or parsed[0] in found:
            raise ValueError("Invalid public source states")
        found[parsed[0]] = value
    return [found[item["key"]] for item in social_label_metadata()]
