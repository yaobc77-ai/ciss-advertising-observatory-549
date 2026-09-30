"""Source-field relationship views. Edges count records, not inferred affiliations."""

from html import escape
from textwrap import wrap

import plotly.graph_objects as go

from .analytics import sponsor_display


def relationship_key(item):
    return (item.get("sponsor") or "(Unknown)", item.get("publisher") or "(Unknown)")


def relationship_label(kind, value):
    """Name missing fields explicitly without changing their selection keys."""
    label = sponsor_display(value) if kind == "sponsor" else value or "(Unknown)"
    return f"Unknown {'sponsor' if kind == 'sponsor' else 'outlet'}" if label == "(Unknown)" else label


def select_relationship(relationships, selection):
    """Validate a public interaction against this filtered aggregate snapshot."""
    if not isinstance(selection, dict):
        return None
    kind = selection.get("kind")
    sponsor, publisher = selection.get("sponsor"), selection.get("publisher")
    pairs = {relationship_key(item) for item in relationships}
    if kind == "edge" and (sponsor, publisher) in pairs:
        return {"kind": kind, "sponsor": sponsor, "publisher": publisher}
    if kind == "sponsor" and any(sponsor == pair[0] for pair in pairs):
        return {"kind": kind, "sponsor": sponsor}
    if kind == "publisher" and any(publisher == pair[1] for pair in pairs):
        return {"kind": kind, "publisher": publisher}
    return None


def selected_relationships(relationships, selection):
    selected = select_relationship(relationships, selection)
    if not selected:
        return list(relationships)
    return [
        item for item in relationships
        if all(relationship_key(item)[index] == selected[field]
               for index, field in enumerate(("sponsor", "publisher"))
               if field in selected)
    ]


def network_figure(relationships, selection=None, limit=24):
    """Bounded two-column network with clickable nodes and numbered edge markers."""
    selection = select_relationship(relationships, selection)
    scope = selected_relationships(relationships, selection)
    scope = sorted(scope, key=lambda item: (-item["count"], relationship_key(item)))
    shown = scope[:max(1, min(int(limit), 60))]
    figure = go.Figure()
    sponsors = sorted({relationship_key(item)[0] for item in shown})
    publishers = sorted({relationship_key(item)[1] for item in shown})

    def column(kind, values):
        # Give every complete, horizontal name room, including wrapped names.
        labels = {value: wrap(relationship_label(kind, value), width=22) for value in values}
        heights = {value: max(42, len(labels[value]) * 16 + 20) for value in values}
        total = sum(heights.values())
        positions, used = {}, 0
        for value in values:
            positions[value] = 1 - (used + heights[value] / 2) / total
            used += heights[value]
        return positions, labels, total

    left, left_labels, left_height = column("sponsor", sponsors)
    right, right_labels, right_height = column("publisher", publishers)
    line_x, line_y, edge_x, edge_y, edge_data, edge_text = [], [], [], [], [], []
    for index, item in enumerate(shown):
        sponsor, publisher = relationship_key(item)
        y1, y2 = left[sponsor], right[publisher]
        line_x.extend([0, 1, None])
        line_y.extend([y1, y2, None])
        # Stagger the selectable count markers to reduce crossings at the center.
        x = 0.35 + (index % 5) * 0.075
        edge_x.append(x)
        edge_y.append(y1 + (y2 - y1) * x)
        edge_data.append({"kind": "edge", "sponsor": sponsor, "publisher": publisher})
        edge_text.append(f"{escape(relationship_label('sponsor', sponsor))} → {escape(relationship_label('publisher', publisher))}"
                         f"<br>{item['count']:,} records · Click to inspect")
    focused_edge = bool(selection and selection["kind"] == "edge")
    figure.add_scatter(x=line_x, y=line_y, mode="lines",
                       line={"color": "#315875" if focused_edge else "#c8d2dc", "width": 2.4 if focused_edge else 1.3},
                       hoverinfo="skip", showlegend=False)
    figure.add_scatter(x=edge_x, y=edge_y, mode="markers+text",
                       marker={"size": 31 if focused_edge else 27, "color": "#edf3f7" if focused_edge else "#fff",
                               "line": {"color": "#315875" if focused_edge else "#bac8d5", "width": 2 if focused_edge else 1}},
                       text=[str(item["count"]) for item in shown], textfont={"size": 11, "color": "#34495e"},
                       customdata=edge_data, hovertext=edge_text, hovertemplate="%{hovertext}<extra></extra>",
                       showlegend=False)
    for kind, values, ys, wrapped, x, color, label in [
        ("sponsor", sponsors, left, left_labels, 0, "#a66d3b", "Sponsors"),
        ("publisher", publishers, right, right_labels, 1, "#315875", "News outlets"),
    ]:
        labels = [relationship_label(kind, value) for value in values]
        focused = [bool(selection and selection.get(kind) == value) for value in values]
        figure.add_scatter(
            x=[x] * len(values), y=[ys[value] for value in values], mode="markers+text",
            name=label, marker={"size": [18 if active else 13 for active in focused], "color": color,
                                "line": {"color": "#20384b", "width": [2.5 if active else 0 for active in focused]}},
            text=["<br>".join(escape(part) for part in wrapped[value]) for value in values],
            textfont={"size": 12, "color": "#34495e"},
            textposition="middle left" if x == 0 else "middle right",
            customdata=[{"kind": kind, kind: value} for value in values],
            hovertext=[escape(value) + "<br>Click to explore records" for value in labels],
            hovertemplate="%{hovertext}<extra></extra>",
        )
    figure.update_layout(
        template="plotly_white", height=max(380, max(left_height, right_height) + 90),
        margin={"l": 16, "r": 16, "t": 48, "b": 22}, paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)", font={"family": "Arial, sans-serif", "color": "#34495e"},
        legend={"orientation": "h", "y": 1.12, "x": 0, "itemclick": False, "itemdoubleclick": False},
        clickmode="event", dragmode=False,
        xaxis={"visible": False, "range": [-1.05, 2.05], "fixedrange": True},
        yaxis={"visible": False, "range": [-0.04, 1.04], "fixedrange": True},
    )
    if not shown:
        figure.add_annotation(text="No relationships in this selection", x=0.5, y=0.5, showarrow=False)
    return figure, {
        "shown_relationships": len(shown), "total_relationships": len(scope),
        "shown_records": sum(item["count"] for item in shown),
        "total_records": sum(item["count"] for item in scope),
    }
