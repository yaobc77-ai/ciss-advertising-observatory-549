# CISS Advertising Observatory

Explore fossil fuel advertising for Boston University's Fall 2026 DS 549 project. Compare company and news-outlet records, browse their relationships, and ask questions with supporting advertisements.

[Live dashboard](https://ciss-advertising-observatory-production.up.railway.app/data) · [Project page](https://yaobc77-ai.github.io/ciss-advertising-observatory-549/)

## Features

- **Data:** company–publisher counts, interactive relationships, date filters, record details, and CSV exports.
- **Query:** statistical and content questions with a summary, evidence, and stated limitations.
- **Sources:** original text, source links, and configured PDF or image-preview attachments.
- **Collections:** native advertisements and social-media company posts, with separate counting units.
- **CLAIMS integration:** result-import validation and source-bound reads of configured classifications.

Counts describe the stored collection. Company posts do not establish paid-ad identity. Historical labels are not verified greenwashing findings. Retrieved examples do not establish an exhaustive list. Source code and the live deployment may differ.

## Technology

| Part | Technology |
| --- | --- |
| Application | Python 3.12–3.13, Dash, Waitress |
| Charts and tables | Plotly, Dash Cytoscape, Dash AG Grid |
| Database and retrieval | PostgreSQL, pgvector, keyword and vector search |
| Data validation | pandas, Pandera, Pydantic |
| Models and tools | OpenAI Python SDK, Responses API, optional MCP SDK |
| Text processing | pySBD, tiktoken |
| Build and hosting | uv, Docker, Railway, GitHub Pages |
| Checks | pytest, Ruff, GitHub Actions |

See [pyproject.toml](pyproject.toml) and [uv.lock](uv.lock) for dependencies.

## Run locally

Use Python 3.12 or 3.13, uv, and PostgreSQL with pgvector.

```sh
uv sync --frozen --extra test --extra mcp
```

Copy `.env.example` to `.env`. Set `OBS_DATABASE_URL`, a random `OBS_COOKIE_SECRET`, and `OPENAI_API_KEY` for generated answers.

```sh
uv run observatory migrate
uv run observatory serve
```

Open [localhost:8050](http://127.0.0.1:8050). Datasets and attachments are supplied separately. Browsing and keyword search do not call a model; generated answers can incur API charges. Keep credentials on the server.

## Documentation

- [User guide](docs/guide.md): pages, filters, records, and answers.
- [Architecture](docs/architecture.md): components and storage.
- [Data dictionary](docs/data_dictionary.md): fields and counting units.
- [Tool reference](docs/mcp_research_tools.md): supported reads and limits.
- [Operations](docs/operations.md): imports, configuration, and deployment.
- [File index](docs/DOCUMENT_INDEX.md): the short documentation directory.
