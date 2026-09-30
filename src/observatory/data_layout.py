"""Data workspace views; components stay mounted to preserve filters and pagination."""

import dash_ag_grid as dag
from dash import dcc, html

from .collection_graph_ui import collection_graph_panel
from .historical_theme_ui import historical_theme_panel
from .knowledge_ui import knowledge_panel
from .network import network_figure
from .research_explorer import explorer_panel


def record_sidebar(prefix, title):
    return html.Aside([
        html.Div([html.H3(title), html.Button("Clear", id=f"{prefix}-reset", n_clicks=0,
                                            className="button button-quiet")], className="section-heading"),
        html.Div(id=f"{prefix}-records", className="network-records", **{"aria-live": "polite"}),
        html.Div([
            html.Button("Previous records", id=f"{prefix}-prev", n_clicks=0, disabled=True, className="button button-quiet"),
            html.Button("Next records", id=f"{prefix}-next", n_clicks=0, disabled=True, className="button button-quiet"),
        ], className="pager"),
        dcc.Store(id=f"{prefix}-selection"), dcc.Store(id=f"{prefix}-offset", data=0),
    ], className="record-sidebar", **{"aria-label": title})


def network_panel():
    return html.Section([
        html.H2("Follow the advertising relationships"),
        html.P("Choose a sponsor, an outlet or a numbered link to inspect its source records.", className="chart-note"),
        html.Div([
            html.Div([html.Label("Focus on an organization or outlet", htmlFor="network-focus"),
                      dcc.Dropdown(id="network-focus", options=[], placeholder="All relationships", clearable=True)], className="network-focus"),
            html.Div([html.Label("Links shown", htmlFor="network-limit"),
                      dcc.Dropdown(id="network-limit", options=[12, 24, 60], value=24, clearable=False, searchable=False)], className="network-limit"),
        ], className="network-toolbar"),
        html.P(id="network-coverage", className="scope-note", role="status"),
        html.Div([
            html.Div([
                html.Div(dcc.Graph(id="network-graph", figure=network_figure([])[0],
                                  config={"displayModeBar": False, "responsive": True, "scrollZoom": False}), className="network-canvas"),
                html.P("Links count source records, not ownership or financial ties. Unknown fields remain separate categories.", className="scope-note"),
            ], className="network-visual"),
            record_sidebar("network", "Source records"),
        ], className="exploration-split network-split"),
    ], className="network-panel", id="relationship-network")


def data_panel(dataset, links_enabled, empty_figure):
    native = dataset == "native"
    graph_config = {"displayModeBar": False, "responsive": True}

    def chart(key, title, note="", wide=False):
        return html.Section([
            html.H3(title),
            html.P(note, className="chart-note", id=f"{dataset}-{key}-note"),
            html.Div(dcc.Graph(id=f"{dataset}-{key}", figure=empty_figure("Choose a collection to explore"),
                              config=graph_config, style={"width": "100%"}),
                     className="chart-scroll" if wide else ""),
        ], className="chart-card chart-wide" if wide else "chart-card")

    columns = [
        {"field": "title", "headerName": "Article", "cellRenderer": "RecordTitle", "tooltipField": "title",
         "minWidth": 260, "flex": 3, "wrapText": True, "autoHeight": True},
        {"field": "sponsor", "headerName": "Sponsor / organization", "minWidth": 165, "flex": 1,
         "wrapText": True, "autoHeight": True,
         "headerTooltip": "Source-listed sponsor: may be a company, trade group or event."},
        {"field": "publisher" if native else "platform", "headerName": "News outlet" if native else "Platform",
         "minWidth": 165, "flex": 1, "wrapText": True},
        {"field": "date", "headerName": "Published", "width": 125, "minWidth": 125},
        {"field": "url", "headerName": "Original", "cellRenderer": "SourceLink",
         "cellRendererParams": {"enabled": links_enabled}, "width": 115, "minWidth": 115},
        {"field": "archive_status", "headerName": "Archive", "cellRenderer": "ArchiveLink",
         "cellRendererParams": {"enabled": links_enabled}, "width": 160, "minWidth": 160},
    ]
    if not native:
        columns.insert(3, {"field": "account", "headerName": "Account", "minWidth": 150, "flex": 1})

    cross_tab = html.Details([
        html.Summary("View the complete count table"),
        dag.AgGrid(id=f"{dataset}-matrix", rowData=[], columnDefs=[],
                   defaultColDef={"sortable": False, "resizable": True, "minWidth": 125},
                   dashGridOptions={"rowHeight": 40, "animateRows": False},
                   className="ag-theme-quartz observatory-grid", style={"height": "440px"}),
    ], className="matrix-table", style={} if native else {"display": "none"})

    overview = html.Div([
        explorer_panel() if native else None,
        html.Section([
            html.Div([
                html.Div([html.Span("SPONSORS & PUBLISHERS", className="eyebrow"),
                          html.H2("Who advertised, and where?")]),
                html.Button("Download counts ↓", id=f"{dataset}-matrix-export", n_clicks=0, className="button button-quiet"),
            ], className="section-heading"),
            html.P("Each cell counts eligible records in your selection. Select a cell to read the corresponding articles.", className="chart-note"),
            html.Div([
                html.Div([
                    chart("relationships-chart", "Sponsor × news outlet", "All selected sponsors and outlets · number of records", wide=True),
                    html.P("Source: the selected advertising collection. Sponsor values include companies, trade groups and events; CERAWeek is an event.", className="scope-note"),
                ], className="matrix-visual"),
                record_sidebar("matrix", "Selected articles") if native else None,
            ], className="exploration-split matrix-split"),
            cross_tab,
            dcc.Download(id=f"{dataset}-matrix-download"),
            html.Div(id=f"{dataset}-matrix-export-status", role="status"),
        ], className="matrix-section", style={} if native else {"display": "none"}),
        html.Div([
            html.H2("Explore the distribution"),
            dcc.RadioItems(id=f"{dataset}-metric", options=[{"label": "Count", "value": "count"},
                                                          {"label": "Percent", "value": "percent"}],
                           value="count", inline=True, className="metric-toggle"),
        ], className="chart-toolbar"),
        html.Div([
            chart("primary-chart", "Where were the ads published?" if native else "Which platforms appear?", "Top 10 · current selection"),
            chart("sponsors-chart", "Which sponsors appear most?", "Top 10 · current selection"),
            chart("timeline-chart", "When were the ads published?", "Annual records in this collection; unknown dates are shown separately.", wide=True),
            html.Div([
                chart("labels-chart", "Which historical labels appear?", "Earlier automated classifications; not verified themes. Select a bar to read its articles.", wide=True),
                historical_theme_panel() if native else None,
            ], className="exploration-split historical-theme-split chart-wide" if native else "chart-wide"),
        ], className="charts-grid"),
    ], id=f"{dataset}-overview", className="data-view")

    records = html.Section([
        html.Div([html.H2("Browse the source records"),
                  html.Button("Download selected records ↓", id=f"{dataset}-export", n_clicks=0, className="button button-quiet")], className="section-heading"),
        html.P(id=f"{dataset}-record-count", className="scope-note"),
        html.Div([
            html.Div([html.Label("Rows per page", htmlFor=f"{dataset}-page-size"),
                      dcc.Dropdown(id=f"{dataset}-page-size", options=[20, 50, 100], value=20, clearable=False, searchable=False)]),
            html.Div([html.Label("Sort records", htmlFor=f"{dataset}-sort"),
                      dcc.Dropdown(id=f"{dataset}-sort", options=[
                          {"label": "Newest first", "value": "date:desc"}, {"label": "Oldest first", "value": "date:asc"},
                          {"label": "Title A–Z", "value": "title:asc"}, {"label": "Sponsor A–Z", "value": "sponsor:asc"},
                      ], value="date:desc", clearable=False, searchable=False)]),
        ], className="record-page-controls"),
        html.P("Open an article title for the stored text, collection search term and available original materials.", className="scope-note"),
        dag.AgGrid(id=f"{dataset}-grid", columnDefs=columns, rowData=[],
                   defaultColDef={"sortable": False, "resizable": True, "filter": False},
                   dashGridOptions={"rowHeight": 68, "animateRows": False, "suppressCellFocus": False},
                   className="ag-theme-quartz observatory-grid", style={"height": "560px"}),
        html.Div([
            html.Button("Previous", id=f"{dataset}-page-prev", n_clicks=0, disabled=True, className="button button-quiet"),
            html.Span(id=f"{dataset}-page-label", role="status"),
            html.Button("Next", id=f"{dataset}-page-next", n_clicks=0, disabled=True, className="button button-quiet"),
        ], className="pager"),
        dcc.Store(id=f"{dataset}-page-offset", data=0), dcc.Download(id=f"{dataset}-download"),
        html.Div(id=f"{dataset}-export-status", role="status"),
    ], id=f"{dataset}-records", className="data-view records-view", hidden=True)

    return html.Section([
        dcc.Store(id=f"{dataset}-network-data"),
        html.Div(id=f"{dataset}-status", className="collection-status"),
        html.Div([
            html.Div(id=f"{dataset}-summary", className="stats-grid"),
            dcc.Tabs(id=f"{dataset}-view", value="relationships" if native else "overview", mobile_breakpoint=0,
                     className="data-tabs", parent_className="data-tabs-container", children=[
                dcc.Tab(label=label, value=value, className="data-tab", selected_className="data-tab-selected",
                        disabled=(value == "relationships" and not native))
                for label, value in ((("Knowledge graph", "relationships"), ("Overview", "overview"), ("Records", "records"))
                                     if native else (("Overview", "overview"), ("Relationships", "relationships"), ("Records", "records")))
            ]),
            overview,
            html.Div([
                collection_graph_panel(),
                html.Details([
                    html.Summary("Inspect article versions, sources and historical annotations"),
                    knowledge_panel(),
                ], id="article-provenance-disclosure", className="article-provenance-disclosure"),
                dcc.Store(id="article-provenance-open", data=False),
            ] if native else None, id=f"{dataset}-relationships", className="data-view", hidden=True),
            records,
        ], id=f"{dataset}-content", className="collection-content"),
    ], id=f"{dataset}-panel", style={} if native else {"display": "none"})
