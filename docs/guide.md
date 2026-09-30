# Setup and user guide

## Explore the dashboard

- Open **Data** and select a collection. Filter by sponsor, news outlet, and publication date.
- Use **Knowledge graph** to select a company, publisher, connection, or article. The side panel shows the named relationship, record counts, shares, and supporting articles. Selecting a distribution row or chart segment narrows the supporting articles.
- Use **Overview** for the company–outlet matrix, annual counts, and historical label distributions. Unknown dates are reported separately.
- Use **Records** to open article details, stored text, and available original or archived sources. Export the selected records or grouped counts as CSV.
- Open **Query** for keyword search or generated answers. Generated answers can use database tools for statistics and retrieved passages for content questions. Check the cited sources when interpreting an advertising claim.

The social collection remains unavailable until its client dataset is imported. CLAIMS2 import and read-only views are implemented; real results still await reviewed publication. Historical labels record earlier annotations and remain separate from CLAIMS2 evidence.

Maintainers can run a [read-only CLAIMS source audit](claims_source_audit.md) against the supplied saved results before importing classifications. This command does not publish new labels.

## Install and configure

Follow the commands in the [README](../README.md). PostgreSQL must support pgvector. Keep `.env` and database backups private.

`OBS_DATABASE_URL` specifies the database connection. `OBS_COOKIE_SECRET` should be a random, stable value. `OPENAI_API_KEY` enables model requests. Model names, API budget, and request limits are configured in `.env.example`. `OBS_SHOW_SOURCE_LINKS=false` hides original advertisement links.

## Load data

Native CSV/XLSX inputs are validated against the source files referenced by `config/native_admissions.json`, `config/native_body_ranges.json`, and `config/native_body_recoveries.json`. Obtain those exact source files before using the native importer.

```sh
uv run observatory import-native --root /path/to/source-bundle
```

For social CSV exports, map the client fields using `config/social_mapping.example.json`:

```sh
uv run observatory import-social /path/to/posts.csv --mapping config/social_mapping.example.json
```

The import commands update records by default. A complete replacement requires the explicit `--mode snapshot` option. The alternative `import-records` command accepts validated JSONL records; `src/observatory/models.py` defines their fields, and `--dry-run` validates without changing the database.

```sh
uv run observatory index
```

Indexing may make paid embedding requests. Without embeddings, keyword search remains available.

## How the application works

Imports validate metadata and retain article text and versions in PostgreSQL. Dashboard filters, counts, and exports share the same record selection. Search combines keyword and vector retrieval. Generated content answers select references to stored passages, and the application checks their source versions and text locations.

The graph contains articles, source-listed sponsor names, publishers, and their record-supported relationships. Missing source fields stay explicit. A source relationship is not independent evidence of payment, and a located quotation does not by itself establish the truth of a claim.

## Deploy and maintain

Build the provided Docker image or connect the repository to Railway. Configure the database URL, cookie secret, and optional API key through the platform's variables. Use `OBS_HOST=0.0.0.0`; Railway supplies `PORT`. For HTTPS hosting, set `OBS_SECURE_COOKIES=true` and configure the trusted proxy for the deployment.

Apply migrations with `observatory migrate` before serving the application. Use `/healthz` to check availability and the application version. Back up the database before upgrades and retain source inputs separately. Import and indexing commands can be repeated when data changes.

Reviewed PDFs and previews are deployed separately using a [private record asset bundle](record_assets.md). [Current handoff instructions](current_handoff.md) cover complete database recovery, source inputs, and remaining acceptance work.

The GitHub Pages workflow publishes only `site/`. It provides the static project description and links to the running application; it does not contain the database or execute RAG.
