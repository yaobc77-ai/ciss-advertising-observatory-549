# Import saved CLAIMS2 results

The maintenance CLI can publish reviewed assignments from the supplied CLAIMS2
paragraph results. It reads saved model responses; these commands do not call a
model, run the upstream classifier, or create embeddings.

**Current status:** the importer and read-only query are implemented. No real
CLAIMS2 labels have been published. Dashboard and MCP integration are next.
An assignment means that an original passage has been mapped to a definition in
a specific taxonomy. It is not independent fact checking or a legal finding of
greenwashing.

## Requirements

- An installed Observatory environment and its configured database connection.
- The original upstream `CLAIMS_2.0_model/src/data` directory, including the saved
  paragraph SQLite results, input CSVs, and four taxonomy JSON files.
- Current Observatory articles with accepted, countable, retrievable text.
- A completed source audit and a review JSON file bound to that audit and taxonomy.
- Ordered database migrations applied before an import with `--apply`.

Use the project's existing private configuration. Check migration status and
back up an existing database before applying migrations:

```sh
uv run observatory migration-status
uv run observatory migrate
```

Keep audits, review files, raw model responses, and database backups private.
The examples put generated files under the ignored `outputs/` directory. Replace
the bundle path with the actual upstream directory and choose a new audit output
directory. The review template also requires a new filename in an existing
directory.

## 1. Audit saved results and original sources

```sh
uv run observatory claims-audit --bundle "path/to/CLAIMS_2.0_model/src/data" --out outputs/claims2-audit-review --dataset native --projection strict
```

This command opens the upstream SQLite database read-only and checks its saved
inputs against the supplied CSV rows and Observatory source versions. It writes
private, hash-bound audit artifacts, including source and quote candidates,
review CSVs, taxonomy provenance, and a summary. It publishes no labels.

`--dataset` accepts `native` or `social`; the supplied paragraph bundle contains
native advertisements. It is not a social dataset.

The default `strict` comparison preserves exact text or mapped whitespace
differences. If the supplied upstream text used its documented character
cleaning, a separate audit can explicitly use `--projection upstream-ascii-v1`.
That versioned projection is lossy. The resulting evidence still contains the
original Unicode quote and original character offsets; the loss is recorded.
Do not change the projection silently or replace an earlier audit directory.

Only eligible candidates enter the publication review: a unique current source
association, the retained upstream article paragraphs located in order, and a
unique original quote inside accepted retrieval text. This verifies retained
input correspondence. It does not prove that upstream cleaning retained the
entire original article. Ambiguous, missing, old-version, excluded, or malformed
results remain in the audit instead of being forced onto an article.

## 2. Create and complete the pending review

```sh
uv run observatory claims-review-template --audit outputs/claims2-audit-review --out outputs/claims2-review.json
```

The template starts with pending authority, pending source and semantic reviews,
and `publication: "hold"` for every eligible candidate. It never fills approval
fields or overwrites an existing review file.

An accountable reviewer completes the file after inspecting the private audit,
the original passage, and the pinned category definitions:

| Field | Meaning and allowed decisions |
| --- | --- |
| `authority` | Whether the selected run and taxonomy may be published as taxonomy assignments: `pending` or `approved`. Approval requires an owner and a timestamp with a timezone. Keep `publication_meaning` as `taxonomy_assignment`. |
| `source_review` | Whether the candidate is associated with the correct article version and original quote: `pending`, `confirmed`, or `rejected`. A completed decision requires reviewer, timestamp, and note. |
| `semantic_review` | Whether the quote supports the candidate under its current definition: `pending`, `supported`, or `unsupported`. A completed decision requires reviewer, timestamp, and note. `supported` must also bind `definition_sha256` to the candidate's unchanged `expected_definition_sha256`. |
| `publication` | `hold`, `reject`, or `publish`. This controls the current import, not an earlier publication. |

Keep the generated audit/run/taxonomy fingerprints, candidate keys, and expected
definition fingerprints unchanged. Keep all candidate decision entries. Pending
review fields remain null; completed review dates must include a timezone, such
as `2026-09-30T12:00:00Z`.

The importer validates these recorded assertions and their source bindings. It
does **not** authenticate the named reviewer or independently prove that a
human performed the review. Ownership and review practice remain project
responsibilities.

Publication requires approved authority and a confirmed source review:

- A candidate with no detected definition or mapping conflict and a pending
  semantic review can publish as `automatic_unverified`. Source confirmation
  alone does not verify the classification.
- A `supported` semantic review bound to the current definition publishes as
  `human_supported`. Definition drift or a raw-response SC conflict requires
  this explicit current-definition review; automatic publication cannot resolve it.
- `unsupported`, `reject`, and `hold` create no new published assignment.

These decisions never classify an entire article as negative. A missing output,
an unprocessed paragraph, or a saved no-match response is also not a verified
negative.

## 3. Validate without writing

```sh
uv run observatory claims-import --audit outputs/claims2-audit-review --bundle "path/to/CLAIMS_2.0_model/src/data" --review outputs/claims2-review.json --out outputs/claims2-import-dry-run.json
```

There is no `--dry-run` flag for this command: omitting `--apply` is the dry run.
The importer reloads the private artifacts and taxonomy, validates their hashes
and review gates, and rechecks current database sources. When the schema exists,
it also checks immutable identities and reports planned new candidates, review
revisions, and unchanged results. It writes only the requested local summary.

Inspect `prepared_publications`, `held_count`, `rejected_count`, `review_states`,
`database_schema_ready`, and the planned counts. An all-held template legitimately
prepares zero publications; that is not a completed label import. A passing dry
run checks the state at that moment. The writing command rechecks it.

## 4. Apply the reviewed assignments

```sh
uv run observatory claims-import --audit outputs/claims2-audit-review --bundle "path/to/CLAIMS_2.0_model/src/data" --review outputs/claims2-review.json --apply --out outputs/claims2-import-applied.json
```

`--apply` commits the eligible assignments, their frozen taxonomy/run provenance,
and review revisions in one transaction. Every source must still be active and
current; its body hash, exact original quote, and accepted retrieval interval
must match. Any validation or identity conflict aborts the import.

Article bodies, retrieval passages, embeddings, and historical CLAIMS1 labels are
not rewritten by this importer. It stores CLAIMS2 assignments separately.

### Repeat imports and later reviews

Candidate identity binds the upstream response, run, taxonomy, article version,
category, and original evidence span. Candidate evidence remains immutable.
Import-level manifests record the reviewed file; changed per-candidate reviews
append revisions. Public queries use the latest revision for each candidate.

- Repeating the same reviewed import produces unchanged results.
- Approving a previously held candidate can add it without duplicating earlier
  assignments.
- Supporting an existing automatic assignment appends a `human_supported`
  revision; it does not overwrite the earlier review.
- Reimporting an older file cannot downgrade `human_supported` to
  `automatic_unverified`.
- A new article body or taxonomy requires a new audited candidate. Earlier
  analysis remains in history.

Changing an existing candidate to `hold`, `reject`, or `unsupported` in a review
file does **not** withdraw its earlier publication. Use explicit retraction.

## 5. Query published matches

```sh
uv run observatory claims-matches --dataset native --limit 20 --offset 0 --out outputs/claims2-matches.json
uv run observatory claims-matches --dataset native --nc-id NC_1 --review-state human_supported
uv run observatory claims-matches --dataset native --publisher "The Washington Post" --sponsor "ExxonMobil"
```

Available filters are `--dataset native|social|all`, repeated `--nc-id`, repeated
`--sc-id`, repeated `--record-id`, repeated `--publisher`, repeated `--sponsor`,
`--taxonomy` (the bundle fingerprint), and `--review-state`. Entity filters must
use the values stored in the collection; use the current collection values when
replacing the illustrative publisher and sponsor above.

`total_records` counts distinct current articles with matching assignments.
`total_matches` counts candidate assignments, not review-history rows. Pagination
uses articles; each returned article includes its matching assignments, original
quotes and offsets, NC/SC definitions, taxonomy/run versions, and latest review
state/version. Retracted, inactive, and old-source-version assignments are hidden.
Articles without published matches remain unclassified, not negative.

`claims_version` is separate from the existing source and retrieval versions. It
changes with CLAIMS publications, latest reviews, retractions, or source states
affecting those candidates. Consumers should retain it alongside source and
retrieval provenance. The CLI projection omits raw model payloads and private
reviewer notes.

## 6. Retract a published candidate

Use the actual `candidate_key` from a published match:

```sh
uv run observatory claims-retract CANDIDATE_KEY --reviewer "Reviewer name" --reason "Reason for withdrawal" --reviewed-at "2026-09-30T12:00:00Z" --out outputs/claims2-retraction.json
```

Retraction appends a dated event and retains the assignment and review history.
The same event is idempotent. Any retraction hides the candidate from matches;
reimporting its result or appending another review does not reactivate it. There
is no undo-retraction command in this version.

## Remaining product work

The CLI/store is the publication boundary. Record-detail displays, CLAIMS filters
and coverage, graph edges, read-only MCP tools, and grounded question routing
still need integration. Saved-result import does not run classification on the
remaining articles. Real publication also awaits the selected bundle/run and
recorded authority, source, and semantic review decisions.

See the [integration plan](claims_integration_plan.md) and
[source audit](claims_source_audit.md) for the supplied bundle's evidence and
coverage limits.
