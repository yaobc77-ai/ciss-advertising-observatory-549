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
| How do counts change across dates, outlets, and sponsors? | Shared filters and annual counts, with unknown dates reported separately. |
| Which themes appear in the ads? | Historical labels and a separate CLAIMS2 evidence view. Real CLAIMS2 results await reviewed publication. |
| How can users explore social media advertising? | A separate collection view and configurable importer. Real-data analysis awaits the client export. |
| How can RAG help users explore both datasets? | Database tools answer count questions; retrieved article passages support cited content answers. Cross-dataset use awaits social data. |

Selecting a company, publisher, relationship, or article shows its specific connections and supporting records. Counts come from the database. Graph connections reflect stored source fields; they do not independently establish business relationships.

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

Dashboard browsing and keyword search do not call a language model. Generated answers use the configured OpenAI API budget, including model interpretation of count questions.

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

This repository includes application code, tests, deployment configuration, a [setup and user guide](docs/guide.md), and [current handoff instructions](docs/current_handoff.md). The [CLAIMS source audit](docs/claims_source_audit.md) checks saved results against original articles. A [reviewed-result importer](docs/claims_result_import.md) preserves source evidence and review history. The [read-only views](docs/claims_read_views.md) display published definitions and original evidence when results are available. Optional [source discovery](docs/claims_source_discovery.md) finds candidate article URLs for legacy inputs; its maintenance MCP tool is hidden by default and does not update records. The [integration plan](docs/claims_integration_plan.md) describes remaining processing work.

Real social data, complete source/archive coverage, user acceptance testing, independent answer review, and final presentation materials remain part of project completion. Historical CLAIMS labels are not verified greenwashing findings.
