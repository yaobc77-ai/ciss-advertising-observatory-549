# Current project handoff

Updated 30 September 2026. Maintenance package and verified hosted application: **0.4.6**. Its health, migration and CI checks are recorded in the [publication receipt](../reports/release_v0_4_6_publication_20260930.json). Full collection-graph interaction checks were performed on **0.4.2**; 0.4.6 adds tested assignment-specific evidence reads. The [CLAIMS source audit](claims_source_audit.md) is a maintenance command; it does not publish labels.

The **0.4.5** maintenance update adds a [reviewed CLAIMS2 importer](claims_result_import.md),
[frozen evaluation inputs](frozen_evaluation.md), and a corrected source handoff archive builder.
CLAIMS2 evidence and review revisions use independent tables and a separate `claims_version`;
article versions, retrieval profiles and historical twelve-label annotations are unchanged.
The 37 located candidates remain held in a private pending review file. No real labels,
customer approvals, frozen acceptance run or model calls were added by this update.

The **0.4.6** implementation adds [read-only CLAIMS2 views](claims_read_views.md)
in Overview, article detail, Query and the article-provenance graph. Each selected
assignment keeps its own taxonomy and source evidence. [Legacy URL discovery](claims_source_discovery.md)
checks exact local text first and can make one paid web search through an explicitly
enabled maintenance MCP tool. Candidate URLs require review and do not update
articles or classifications. The [engineering receipt](../reports/claims_read_engineering_v0_4_6_20260930.json)
records 1,589 offline and 33 isolated database tests. One paid source-search sample
cost $0.0134298 and did not establish its original article URL. Both repositories'
CI passed; the hosted source collection and retrieval index remain unchanged.
The [source archives](../reports/source_handoff_v0_4_6_20260930.json) bind the tested application commits.

[Live dashboard](https://ciss-advertising-observatory-production.up.railway.app/data) · [Questions](https://ciss-advertising-observatory-production.up.railway.app/query) · [Project page](https://yaobc77-ai.github.io/ciss-advertising-observatory-549/) · [English repository](https://github.com/yaobc77-ai/ciss-advertising-observatory-549)

This guide describes the current application. Earlier reports and presentation files retain the results of their own versions. They are not the final project acceptance record.

## 1. What is available

The native collection is connected. The recorded release contains 275 active records, of which 263 are eligible for statistics and 226 have text eligible for retrieval. Its active retrieval profile is `sentence600-v1`, with 556 passages. These are different counts: missing or unsuitable text does not necessarily exclude a record from statistics.

The social importer and separate collection view exist, but the client social dataset is not connected. Real social data and customer review materials are recorded as deferred TODOs under the latest user direction. Historical labels can be explored; they are earlier automatic annotations, not independently verified greenwashing findings. The original project description placed CLAIMS integration in future work; the latest customer priority now schedules that integration as project work. Reviewed import and read-only record, graph and MCP interfaces are implemented; all 37 real candidates await reviewed publication. Retraining a classifier has not been requested. Animal agriculture remains future work.

| Project question | Where to demonstrate it |
| --- | --- |
| Compare native ad counts by company and outlet | **Data → Overview**: the company–outlet matrix and exported count table. |
| Find each outlet's sponsors or each company's publishers | **Data → Knowledge graph**: select an entity, inspect named connections and their distribution, then open the supporting articles. |
| Compare dates, companies, outlets and sponsors | Apply the visible collection filters. Counts, charts, exports and article lists use the selected scope. Unknown dates are reported separately. |
| Explore themes supported by existing data | Historical label distributions and their supporting articles. A separate CLAIMS2 view reads published NC/SC assignments; actual publication is pending. Labels may overlap; absent annotations do not prove an absence of themes. |
| Explore social advertising | The social view will require the real export and a confirmed field mapping before its statistics can be accepted. |
| Ask grounded questions about both datasets | **Query**: database tools answer count and list questions; retrieved passages support content answers. Real social and cross-collection evaluation remain pending. |

Graph relationships state what source records list. `source_lists_sponsor` links an article to its source-listed sponsor name; `published_in` links it to the recorded publisher. The entity overview aggregates those record-supported connections. This alone does not establish a separately verified payment, business partnership or company identity. CERAWeek is displayed as an event/series; its inclusion in a company-only research denominator needs a client decision.

Selecting a company, outlet, connection, distribution row or article shows that object's actual named relationships and supporting records. A selected distribution segment narrows the article list; the accompanying parent distribution retains its stated denominator. Exported selected counts should correspond to the current selection.

## 2. Code and runtime

| Component | Current technology |
| --- | --- |
| Application and interface | Python 3.12 or 3.13, Dash, Waitress, Plotly, Dash Cytoscape, Dash AG Grid |
| Import and validation | pandas, Pandera, Pydantic, openpyxl |
| Persistence and retrieval | PostgreSQL, pgvector, English keyword retrieval and vector retrieval |
| Model integration | OpenAI Python SDK, Responses API; configured defaults `gpt-5.6-luna` and `text-embedding-3-small` |
| Text processing | pySBD, tiktoken; Lingua for conservative language checks |
| Delivery | uv and `uv.lock`, Docker, Railway, pytest, Ruff, GitHub Actions |
| Optional tool access | MCP Python SDK; shared read-only question tools and a separately enabled maintenance source lookup |

The database stores records, immutable text versions, annotations, passages, retrieval profiles and publication state, embeddings, import reports, saved answers, model outputs and the usage ledger. Keep the ledger and previous versions when upgrading.

Migration 3 adds six CLAIMS2 tables for taxonomies, runs, import manifests, immutable
evidence assignments, review revisions and retractions. Applying it creates storage;
it does not approve or import classifications. The importer is a maintenance CLI.
The [CLAIMS2 website and MCP reads](claims_read_views.md) consume only published current-source assignments. The optional [legacy source lookup](claims_source_discovery.md) is a separate maintenance workflow. Its MCP tool is hidden by default; `OBS_CLAIMS_SOURCE_SEARCH_ENABLED=true` exposes it in that process. A local hit makes no model call; external search uses the shared API budget and changes usage accounting only.

Dashboard browsing and keyword search do not call a model. With `OBS_RESEARCH_AGENT_ENABLED=true`, **Generate answer** first uses the configured model to interpret the question and choose constrained tools. This includes count questions: counting uses database records, but model interpretation has an API cost. Content generation also uses the API budget. Turning the flag off retains the earlier fixed-pattern statistics route.

The standard Docker image contains the installed application and locked dependencies. It does not contain source datasets, client credentials, database backups, reviewed PDFs or preview images. Optional MCP is installed with the `mcp` extra; it is not exposed by the public Dash server.

## 3. Start a new environment

Obtain an approved code revision, Python 3.12 or 3.13, uv, and a PostgreSQL database with pgvector. Start with an empty database or an explicitly approved restored copy.

```sh
uv sync --frozen --extra test --extra mcp
```

Copy `.env.example` to a private `.env` and set the database connection and a random stable `OBS_COOKIE_SECRET`. Set `OPENAI_API_KEY` only if model requests are required. Review model names, budget, request limits and source-link visibility before opening access. Keep secrets out of source control and public reports.

```sh
uv run observatory migration-status
uv run observatory migrate
uv run observatory health
uv run observatory serve
```

Open `http://127.0.0.1:8050`. Installing the code does not load the live collection. On an existing environment, back up before migrations; migration does not reimport data or recalculate embeddings.

## 4. Choose the data reproduction route

### A. Restore a complete database copy

A complete database backup is the route for preserving existing embeddings, old article versions, saved citations, import history, retrieval membership and state, and the usage ledger. A fresh source import cannot recreate those histories.

The current verification script creates one exported PostgreSQL snapshot, backs it up and restores it to a new database in the same owned local cluster:

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/verify_database_restore.py `
  --restore-database observatory_restore_handoff_20260930 `
  --output reports/restore_handoff_20260930.json
```

Choose fresh target and report names. The script rejects an existing target or output, compares every user table's row fingerprints, schema, sequences, extensions and retrieval state, and checks stored body, passage, evidence and citation locations. It does not call a model, change the application's connection or delete a database. Its actual result must be recorded; the script's existence is not a completed restore check. It covers a local snapshot, not the separate Railway database, another machine, global roles or source files.

The existing Windows helper operates only on this project's owned local cluster. From the original workspace, it creates a new backup under the ignored `.runtime/backups` directory:

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/local_postgres.py backup
```

Verify the archive SHA-256 against its sidecar, then restore to an explicitly named database that does not already exist:

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/local_postgres.py restore `
  --backup-file '.runtime/backups/ACTUAL_BACKUP_FILE.dump' `
  --database observatory_handoff_review
```

Replace the archive placeholder and use a unique new database name. The helper refuses the main database and existing targets, does not use `--clean`, and does not switch `.env`. It retains a failed new target for inspection. Restoring to a different host also requires its pgvector extension, suitable roles and private deployment configuration.

Before treating a restore as accepted, compare it with the backup's fixed source snapshot:

1. Every public business table, including `schema_migrations`, retrieval profiles, membership, preparations and publications, embeddings, saved answers, generation outputs and usage.
2. Full row-content fingerprints, schema/index/constraint definitions and relevant sequence state, not only row counts.
3. Source-data version, active profile, index version and combined data version.
4. All stored text hashes, passage IDs and original Unicode character ranges; old citations must still resolve to the versions used when they were generated.
5. Actual keyword and vector retrieval on the restored copy, using stored vectors without requesting new embeddings.
6. The main database and its connection settings remain unchanged.

The [30 September 2026 complete local restore receipt](https://github.com/yaobc77-ai/ciss-advertising-observatory/blob/main/reports/database_restore_v0_4_2_complete_20260930.json) passed all seven comparisons. Its fixed snapshot and restored copy contain 15 tables, 828 text versions, 2,071 stored passages, 1,051 embeddings, 121 saved answers and 176 usage entries. All 729 saved evidence references and 189 citations locate correctly, including 94 citations to earlier text versions. Six keyword, hybrid and empty-filter probes matched between source and restore. The active profile retained its 556 current passages; the 2,071 total also includes historical passages. No model requests were made, and the main database and private configuration stayed unchanged.

This completes the tested **local same-cluster restore**. It does not establish restoration of the separate Railway database, recovery on another machine, source-file recovery, semantic answer quality or another implementer's handoff acceptance. The older 0.2.4 nine-table restore and synthetic wheel checks retain their separate scope.

### B. Import from approved source files

The native importer requires the exact inputs bound by the three manifests in `config/`. Preserve bytes and relative paths; do not edit manifest hashes to make a different file pass validation.

| Private input | Purpose |
| --- | --- |
| `sources/FA25_SP26/final_dataset_cleaned.csv` | Cleaned native record baseline. |
| `sources/Native Advertising Data/native_ad_dataset.xlsx` | Original disclosure values and quality information. |
| `sources/pdf_archive_20260915/nested_unique/native-ads-download/combined_ads_12-4-25.csv` | Additional collected candidates and admission evidence. |
| `sources/FA25_SP26/CLAIMS 1.0 Runs/CSVS/predictions_calibrated.csv` | Earlier annotation outputs and their version binding. |
| `analysis/pdf_archive/source_index.json` | Archive candidates and acquisition/source information. Title candidates are not verified attachments. |
| `sources/pdf_archive_20260915/pdfs/summer_2025_run/CNBC/2018-12-28T10_21_52-0500_Usingmolluskstomonitorindustrialsites.pdf` | The reviewed PDF-265 source snapshot. |
| `sources/recovered_native/PDF-265.pypdf-6.10.0.txt` | Exact extracted PDF-265 text, with preserved character positions and page separators. |

The corresponding manifests are `config/native_admissions.json`, `config/native_body_ranges.json` and `config/native_body_recoveries.json`. They specify the source hashes, reviewed admissions and retrieval ranges. The PDF and extracted-text hashes are checked independently. `scripts/extract_pdf_text.py`, with the optional locked `pdf` extra, reproduces the reviewed extraction without OCR or normalizing the original text.

```sh
uv run observatory import-native --root /path/to/approved-source-bundle
```

Read the import report before continuing. Import commands default to **upsert**, retaining records absent from this batch. Use `--mode snapshot` only for an approved complete replacement: it retires missing records and refuses an empty snapshot. A canonical JSONL alternative is available through `import-records`; use `--dry-run` to validate it before opening the database.

For real social data, confirm the meaning of source IDs, platform, account/advertiser, text, dates, links and optional engagement fields. Copy the example mapping to a dedicated reviewed file; do not assume its field names match the client's export.

```sh
uv run observatory import-social /path/to/posts.csv --mapping /path/to/reviewed-mapping.json
```

Reconcile source and imported record totals, rejected/duplicate rows and date handling. Post counts and article counts have different units. Engagement is not an impression measure unless the source explicitly defines it that way.

Source import may create passages without their embeddings. `observatory index` fills missing embeddings for the active profile and can incur API cost. Read `index-status` and `budget` first. Preparing or activating another profile uses the explicit `index-prepare` / `index-activate` contract; activation refuses missing vectors or a changed source snapshot. See [index maintenance in the original repository](https://github.com/yaobc77-ai/ciss-advertising-observatory/blob/main/docs/operations.md#检索索引准备发布与回退). Do not invoke indexing automatically on ordinary startup or claim that a source import restores historical model outputs.

## 5. Preserve source materials separately

Only reviewed, hash-bound PDF mappings may be served. The current reviewed local mapping is PDF-265; most other archive matches are still candidates. The public `archive_url` field and a reviewed local capture are distinct.

Record detail attachment access depends on the source root containing the recovery manifest and approved PDF. Preview access additionally needs the matching PNG and JSON receipt under `sources/recovered_native/previews`. `scripts/render_record_previews.py` prepares the first-page cache using Poppler outside web requests. The receipt binds the image to the reviewed PDF hash.

For a hosted environment, the new [private record asset bundle](record_assets.md) separates reviewed files from the code-only image:

```sh
uv run python scripts/build_record_asset_bundle.py build --source-root . --destination .runtime/record-assets-release --record-id d340f887-efa7-5746-aaf8-14aabba6b63f
```

The builder returns the manifest SHA-256. Transfer the bundle privately, then check that mounted copy:

```sh
uv run python scripts/build_record_asset_bundle.py check --root /mounted/record-assets-release --manifest-sha256 ACTUAL_MANIFEST_HASH
```

Set `OBS_RECORD_ASSET_ROOT` to that mount and `OBS_RECORD_ASSET_MANIFEST_SHA256` to the returned hash. The bundle contains `record_assets.json`, hash-named PDFs and optional previews. A configured bundle is checked as a whole; an invalid or incomplete bundle disables attachments without falling back to other files. The record JSON exposes `record_asset_status=bundle_verified` or `bundle_unavailable`. Without these settings, the existing local-workspace attachment paths remain in use.

The actual local PDF-265 bundle build and check have passed with one reviewed PDF and one cached preview; its manifest SHA-256 is `0a4eed8b7dcd205ec7d6b99f9e3837aa6e0ce29fa5344919c19962d2c84d10d9`. The private files were not published. This does not provision a Railway mount or prove hosted attachment access. Check the deployed detail, PDF and preview endpoints after private transfer; hosted attachment acceptance remains pending until that check. Missing or changed assets must stay unavailable rather than being substituted. A restored database by itself does not restore filesystem attachments.

## 6. Verify code, data and answers separately

```sh
uv run ruff check src tests scripts
uv run pytest -q -m "not integration and not live"
```

GitHub CI builds and installs a wheel, checks packaged resources, and runs tests without a database or paid model. Check the target commit's actual CI run. Integration tests require a dedicated test database and must never point at the application database. The current installed-wheel reproduction process is documented in [Current wheel reproduction](https://github.com/yaobc77-ai/ciss-advertising-observatory/blob/main/docs/CURRENT_RELEASE_REPRODUCTION.md).

The 0.4.2 publication receipt records 982 passed and 62 deselected engineering tests, plus production graph selection checks. Its real-corpus graph checker verifies selected relationship scopes and counts. These checks do not establish answer meaning, a verified greenwashing classification, client usability, or social-data acceptance.

The 0.4.4 installed wheel passed 1,258 tests, with one Windows symlink test skipped and 62 integration/live tests deselected. Both repository CI runs also passed 1,258 tests, with one private-bundle case skipped. Its [offline package reproduction](../reports/current_release_offline_reproduction_v0_4_4_20260930.json) separately verifies installed modules, packaged assets/SQL and the canonical import dry run. No model request was made; these checks do not replace the full local restore or client review.

The 0.4.5 installed wheel passed 1,406 offline tests (one Windows symlink skip,
79 integration/live deselections) and 22 isolated database tests. Both repository
CI runs passed 1,406 tests, with one private-bundle case skipped. Local migration
and the observed production predeploy log reached version 3. The [engineering
receipt](../reports/claims_import_engineering_v0_4_5_20260930.json) and
[source archive receipt](../reports/source_handoff_v0_4_5_20260930.json) bind those
checks and the two clean source archives. No new classification or real review
approval is inferred from them.

The evaluation files contain 20 development questions and 20 acceptance **drafts**. Customer review materials remain a tracked TODO. When available, obtain reviewed questions and known supporting records, freeze the acceptance set, and collect independent human judgments of attribution, sufficiency and completeness. Keep unavailable social cases pending. Report native, social, cross-collection, no-evidence, latency and cost results with their own denominators. Citation character matching establishes location; it does not establish that the quote supports the generated answer.

Format-2 review packets can now bind native, social or cross-collection inputs to
review files, the complete selected record/version/body set and the source/index snapshot.
Evaluation accepts these only with `--frozen-manifest`; draft mode remains unchanged.
See the [frozen evaluation guide](frozen_evaluation.md). A verified input manifest
does not establish semantic accuracy or customer acceptance.

## 7. Hosted deployment and operating limits

Railway serves the application; GitHub Pages serves a static project description. Pages does not host PostgreSQL or run RAG. Cloudflare configuration is an optional alternative and is not required by the existing Railway deployment.

Use the provided Dockerfile. Set `OBS_HOST=0.0.0.0`; Railway supplies `PORT`. Configure the database connection, stable cookie secret and API key through private platform variables. For HTTPS, enable secure cookies and configure the trusted proxy for the actual deployment.

The deployment contract uses `/app/.venv/bin/observatory migrate` before startup, `/healthz` with a 60-second timeout, and deployment after successful CI. Check the platform's applied settings and logs: the repository's `railway.json` alone does not demonstrate that a setting was adopted.

The historical public health observation on 30 September 2026 identifies application **0.4.4** at commit `e01edc9e7767f37691686d1a999c8423a0a2ceb4`, with native 275 records, 556 passages, `sentence600-v1`, and the graph, distribution and research-agent features enabled. The source, data and index identifiers match the earlier 0.4.3 observation. Both repository CI runs passed; the query/data routes and static project page returned HTTP 200. These checks are recorded in the [publication receipt](../reports/release_v0_4_4_publication_20260930.json), which binds the tested application commits before a documentation-only follow-up. A healthy endpoint is not proof that every user workflow or attachment works.

The latest 0.4.5 observation identifies commit `f2c9142e9274dc4f694c5ac0e6a61270997d753c`.
Its successful deployment and migration-3 log were checked in the existing
Railway project. Native 275, 556 passages, source/data/index identities and the
three feature flags are unchanged. Query/Data and the static page returned 200.
The [0.4.5 receipt](../reports/release_v0_4_5_publication_20260930.json) records this
scope; product graph flows were not repeated for this maintenance update.

The example configuration specifies a $100 monthly application/API budget, 5 requests per visitor per minute, 30 per UTC day and 2 concurrent generations. The deployed values can differ; inspect the actual configuration and ledger before spending. Application/API controls are separate from Railway hosting charges. Failed or uncertain requests retain their recorded costs and reservations; do not clear the ledger to resume service.

After deployment, verify filters, the matrix, entity selection, exact article drilldown, record and count exports, source details, keyword search, and a separately authorized generated answer. Test public access from another network, restart persistence and failure/limit behavior. These remain distinct from CI and human client acceptance.

## 8. Handoff completion checklist

Build the source archive from a reviewed clean commit, using a new output name:

```sh
python scripts/build_handoff.py --output .runtime/ciss-observatory-source-v0.4.6.zip
```

The builder includes tracked public code, documents, assets, `Dockerfile`,
`.dockerignore`, `railway.json` and CI configuration. `HANDOFF_MANIFEST.json`
records the commit, package version and file hashes; `CONTENTS.sha256` checks
the payload and manifest, and a separate sidecar checks the ZIP. A changed
working tree is recorded explicitly. Runtime outputs, source datasets, private
configuration and backups require their separate private transfer.

- Identify the delivered commits, dependency lock, installed package and target database snapshot.
- Transfer approved source inputs, complete database backup, attachment files and private configuration through the agreed private channel.
- Have another implementer reproduce setup, migration/import or full restore, engineering checks and the demonstrated UI flows.
- Integrate the approved CLAIMS outputs with explicit record/text-version, taxonomy-version and source-evidence bindings; this scheduled work is not implemented by the historical labels.
- Deferred TODO: reconcile the real social dataset and run both social and cross-collection acceptance cases after the client export becomes available.
- Obtain the client decisions on company identity, CERAWeek, historical label meaning and useful filters/visualizations.
- Deferred TODO: complete frozen human answer review and client usability review when the customer review materials become available; keep failures visible.
- Present the current implemented features and their explicit TODOs. The final two-dataset demonstration and client handoff acceptance follow the outstanding materials; the old 0.2.2 presentation is an earlier research preview.

Until these are evidenced, the product is a working native-data research preview rather than an accepted complete dual-dataset delivery.
