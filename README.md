# CISS Advertising Observatory

A dashboard for exploring fossil fuel advertising, built for Boston University's Fall 2026 DS 549 project. It helps journalists, lawyers, and researchers compare advertising records and examine the claims in them.

[Live dashboard](https://ciss-advertising-observatory-production.up.railway.app/data) · [Ask a question](https://ciss-advertising-observatory-production.up.railway.app/query) · [Static project page](https://yaobc77-ai.github.io/ciss-advertising-observatory-549/)

## Project scope

The original project description calls for a dashboard and retrieval-augmented generation (RAG) search across two datasets: fossil fuel native advertising and fossil fuel social media advertising.

Native advertising data is connected. The social media view and importer are implemented, but the client dataset has not been loaded. CLAIMS2 import and read-only views are implemented; the 37 saved-result candidates remain on hold and no new real results have been published. Animal agriculture remains future work.

## Research questions and current features

| Research question | Current application |
| --- | --- |
| How many native ads appear by company and news outlet? | A company–outlet matrix with exact counts and CSV export. |
| Which companies sponsor ads at each outlet? | An interactive relationship graph, named sponsor lists, distribution charts, and supporting articles. |
| How do counts change across dates, outlets, and sponsors? | Shared filters, annual counts, all tied highest years, and comparisons between named date periods. Unknown dates are reported separately. |
| Which themes appear in the ads? | Historical labels and a separate CLAIMS2 evidence view. Real CLAIMS2 results await reviewed publication. |
| How can users explore social media advertising? | A separate collection view and configurable importer. Real-data analysis awaits the client export. |
| How can RAG help users explore both datasets? | Database tools answer count questions; retrieved article passages support cited content answers. Cross-dataset use awaits social data. |

Selecting a company, publisher, relationship, or article shows its specific connections and supporting records. Counts come from the database. Graph connections reflect stored source fields; they do not independently establish business relationships.

## Requirements updates

Recorded through 1 October 2026. These requirement revisions are separate from application release numbers. **Implemented** means the feature is available; customer acceptance is recorded separately.

| Revision and source | Requirement update | Current status |
| --- | --- | --- |
| **R1 — Original FA26 project description** · document date not stated | Explore native and social advertising through filtered counts, company–outlet comparisons, themes where supported, and source-grounded RAG. Deliver code, deployment, documentation, and a final demonstration. | Native exploration is implemented. Real social data, independent review, and the final dual-dataset demonstration remain pending. |
| **R2 — Meeting, 25 September 2026** | Add an interactive knowledge graph with clear source relationships. Support different years, usable exploration, maintainable migrations, and a reproducible pipeline. | Graph selection opens named relationships, distributions, and supporting articles. Import and migration mechanisms exist; large-scale performance and independent reproduction remain to be demonstrated. |
| **R3 — Michelle's feedback email** · send date not provided | Make the six research questions easy to answer, including New York Times ad counts, ExxonMobil's publishers, and Washington Post sponsors. Calculate totals from all matching records, with clear filters and record access. | Exact counts, complete relationship lists, chart drilldowns, and record browsing are implemented. Customer usability and counting-policy review remain pending. |
| **R4 — User interface requests** · request dates not recorded | Separate Query and Data. Organize data into overview, graph, and records; show numbers, readable relationships, and the selected object's articles. Keep source material close to the record, and secondary controls in a toolbox. | These views and selection drilldowns are implemented. Record details show available text and sources; complete PDF, screenshot, and archive coverage remains pending. |
| **R5 — Later customer priority, relayed by the user** · documented 30 September 2026 | Bring CLAIMS integration into the current work. Reuse the supplied implementation and saved results, link them to original articles, and expose reviewed claims with their evidence. This advances work originally deferred to a future semester. | Source auditing, result import, and read-only views are implemented. All 37 candidates remain on hold for source and result review; no new real results have been published. |
| **R6 — User query and tool requests** · consolidated 1 October 2026 | Use a consistent Summary / Evidence answer layout, model-selected data tools, annual totals and all highest-year ties, period comparisons, percentages, matching-record browsing, ambiguity handling, and optional external lookup when collection answers remain unresolved. Show actual loading stages. | These interfaces and tool paths are implemented. External findings stay separate from collection counts. Independent model-routing, evidence-support, and customer checks remain pending. |

The [client requirements review](docs/CLIENT_REVIEW_V0_4_2.md) records the six research questions and review tasks. The [CLAIMS integration plan](docs/claims_integration_plan.md) and [research tools guide](docs/mcp_research_tools.md) describe the later scope and implementation. Earlier review documents retain their original release context; use the [current handoff](docs/current_handoff.md) for current delivery status. User implementation requests are distinguished from requirements stated directly in the project description, meeting, or client email.

For each new meeting note, email, or user request, append a requirement revision with its source and date, changed tasks, owner, due date, acceptance check, and status. Keep unassigned owners and unagreed dates marked **TBD**. Mark a task accepted only when its named reviewer approves the result for the recorded application and data versions; tests and deployment alone do not close that task.

## Current technology

| Part | Technology |
| --- | --- |
| Application | Python 3.12/3.13, Dash, Waitress |
| Charts and tables | Plotly, Dash Cytoscape, Dash AG Grid |
| Data processing | pandas, Pandera, Pydantic |
| Database and search | PostgreSQL, pgvector, keyword and vector retrieval |
| Language models | OpenAI Python SDK and Responses API; configured defaults: `gpt-5.6-luna` and `text-embedding-3-small` |
| Text processing | pySBD, tiktoken |
| Installation and deployment | uv, Docker, Railway |
| Tests | pytest, Ruff, GitHub Actions |
| Optional tool interface | MCP Python SDK |

Generated answers show every cited quotation with its answer number, such as **Citation [1]**. Retrieval rank is shown separately.

Dashboard browsing and keyword search do not call a language model. Generated answers use the configured OpenAI API budget, including model interpretation of count questions.

Query also answers percentages using exact database counts. Each answer shows the matching records, denominator and percentage separately for each collection. The comparison group must stay within the current page filters; an empty group has no defined percentage.

Open **Inspect matching records** to browse all records behind a count, list or percentage. Previous, Next and First page keep the submitted selection and make no model calls. Submit again after changing the question or filters; if the collection changes, the application asks for a new query.

## Run locally

Use Python 3.12 or 3.13, uv, and a PostgreSQL database with pgvector.

```sh
uv sync --frozen --extra test --extra mcp
```

Copy `.env.example` to `.env`. Set `OBS_DATABASE_URL` and a random `OBS_COOKIE_SECRET`. Set `OPENAI_API_KEY` to enable generated answers.

```sh
uv run observatory migrate
uv run observatory serve
```

Open [localhost:8050](http://127.0.0.1:8050). Source datasets and database contents are supplied separately; installing the code does not load the live collection.

## Tests and deployment

```sh
uv run pytest -q -m "not integration and not live"
uv run ruff check src tests scripts
```

Database and paid API tests run separately. `Dockerfile` and `railway.json` provide the application deployment configuration. GitHub Pages serves the static project page; the live dashboard and RAG run on Railway.

## Documentation and remaining work

The [MCP and research tools guide](docs/mcp_research_tools.md) explains the eight shared tools, question routing, exact statistics, source evidence, knowledge graph relationships, CLAIMS reads, and optional web lookup. The website uses model function calling; an independent MCP server exposes the same tool implementation. Answers separate the summary, supporting evidence, scope and execution details. External web findings remain separate from collection records and counts.

This repository includes application code, tests, deployment configuration, a [setup and user guide](docs/guide.md), and [current handoff instructions](docs/current_handoff.md). The [CLAIMS source audit](docs/claims_source_audit.md) checks saved results against original articles. A [reviewed-result importer](docs/claims_result_import.md) preserves source evidence and review history. The [read-only views](docs/claims_read_views.md) display published definitions and original evidence when results are available. Optional [source discovery](docs/claims_source_discovery.md) finds candidate article URLs for legacy inputs; its maintenance MCP tool is hidden by default and does not update records. The [integration plan](docs/claims_integration_plan.md) describes remaining processing work.

The [private source review packet](docs/claims_source_discovery.md#create-a-private-source-review-packet) freezes a saved lookup and the original CLAIMS paragraph, with optional captured-text comparisons. It runs offline and starts every source decision as pending. The [0.4.7 maintenance receipt](reports/claims_source_review_v0_4_7_20260930.json) records five candidates from the unchanged earlier lookup and no captured pages or new paid calls; the original article URL remains unresolved.

Real social data, complete source/archive coverage, user acceptance testing, independent answer review, and final presentation materials remain part of project completion. Historical CLAIMS labels are not verified greenwashing findings.

One reviewed PDF snapshot and its first-page preview are available in the live record view. [Attachment deployment and checks](docs/record_assets.md) explain how to preserve and verify these files separately from application code.

The [local](reports/DATABASE_RESTORE_V0_4_7_20260930.en.md) and [Railway](reports/RAILWAY_DATABASE_RECOVERY_20261001.en.md) recovery checks passed for all 21 tables, vectors and histories. The Railway copy was restored to a new database on the same PostgreSQL 18.6 service; its downloaded backup remains private. Independent-machine recovery and complete project acceptance remain pending.

For the current native-data review, use the [demonstration script](deliverables/native_demo_v0_4_9.en.md). The [source input guide](docs/native_source_handoff.md) provides a checksum inventory and an offline check before import. Private inputs, database backups and reviewed attachments are transferred separately from public code.
