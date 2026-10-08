# Operations

## Install and configure

Use Python 3.12 or 3.13, uv, and PostgreSQL with pgvector.

```sh
uv sync --frozen --extra test --extra mcp
```

Copy `.env.example` to `.env`. Set the database connection and a stable random cookie secret. Add the API key only when generated answers are required. Model names, request limits, and budget settings are server-side.

Code installation does not import source records or attachment files.

## Migrate and start

Back up the target database before an upgrade. Inspect migrations, apply them, and start:

```sh
uv run observatory migration-status
uv run observatory migrate
uv run observatory serve
```

Open `http://127.0.0.1:8050`. Use `/healthz` for service availability. Health does not establish data completeness or answer quality.

## Import and index

Validate supplied records before import. Native and social source formats have separate preparation paths. Canonical JSONL can be checked without a database:

```sh
uv run observatory import-records /path/to/records.jsonl --dataset native --dry-run --out /path/to/new-validation.json
```

Use a new output file and retain the input hash. Importing records and generating embeddings are separate actions. Embeddings and generated answers can incur API charges; do not run indexing automatically on every application startup.

See [the input contract](PROTOTYPE_DATA_PIPELINE.md) for record requirements and [record assets](record_assets.md) for PDFs and previews.

## Deploy

The Docker image contains the installed application. Railway supplies runtime environment variables and the service port. Run migrations in the target environment before serving an updated application.

Attach persistent storage separately for source materials. Deploying GitHub source does not publish those private files. Configure HTTPS cookies and stable secrets for hosted use.

GitHub Pages serves the static project page; the Python application and PostgreSQL require a separate host.

## Maintain

- Preserve original inputs, hashes, and version history.
- Back up the database before migrations or bulk imports.
- Retain prior attachment bundles until new file responses are verified.
- Record model failures and budget uncertainty separately from missing evidence.
- Check the deployment identity, schema, collection scope, and attachments after an update.
- Keep credentials and private review material out of Git.

## Development checks

Run the permitted development gate with a fresh report directory:

```sh
uv run --no-sync python scripts/run_project_checks.py --report-dir .runtime/project-checks-new
```

The gate records its scope and protects evaluation-only material. Live model checks are separate. Database integration uses a private test instance rather than the application database.
