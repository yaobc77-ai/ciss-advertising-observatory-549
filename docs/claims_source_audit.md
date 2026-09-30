# Audit saved CLAIMS results

The `claims-audit` command is the first implemented integration step. It reads
the supplied paragraph results and the Observatory's current and historical
article versions. It does not call a model, execute upstream code, change either
database, or publish labels to the website.

## Inputs and command

Obtain the supplied `CLAIMS_2.0_model/src/data` directory, its sibling
`src/notebooks/clean_paragraph_data.ipynb`, and the original Observatory database.
The directory must contain the four taxonomy JSON files, saved SQLite database,
sampled paragraph CSV and cleaned paragraph CSV. No new dependency is required.

```sh
uv run observatory claims-audit \
  --bundle /private/ml-ciss-native-ads-main/CLAIMS_2.0_model/src/data \
  --dataset native \
  --out /private/audits/claims-strict
```

The default allows only exact and whitespace-normalized text matches. A separate
run can explicitly enable the supplied notebook's recorded punctuation and
ASCII conversion:

```sh
uv run observatory claims-audit \
  --bundle /private/ml-ciss-native-ads-main/CLAIMS_2.0_model/src/data \
  --dataset native \
  --projection upstream-ascii-v1 \
  --out /private/audits/claims-projected
```

Each output directory must be new. The optional projection checks the notebook
hash, records the transformation and Unicode version, and maps every candidate
back to the literal original quote and character positions. It marks changes
beyond whitespace as lossy and preserves ambiguous boundaries. It never makes
fuzzy, case-insensitive or title-only associations.

## What the checks mean

- Join saved paragraph IDs only to the supplied input CSV. Check text and
  metadata article IDs; these numbers never directly select Observatory UUIDs.
- Confirm all sampled groups reproduce the cleaned CSV's retained paragraph
  sequence. Upstream filtering removed material, so this does not establish
  complete original-article coverage.
- Find all possible current and historical text occurrences. Check all retained
  paragraphs in order before marking a full retained-input association candidate.
- Decode each original model response and use its action-specific category and
  snippet together. The flattened snippet list has lost this correspondence.
- Validate quote positions, source versions and existing retrieval exclusions.
  Preserve partial-article, old-version, unlocated and ambiguous candidates.
- Preserve recorded model/time, historical definitions, NC/SC conflicts and
  missing mappings. Absent prompt or code identities remain unknown.

Each classification occurrence has a stable candidate key based on its saved
run, raw response, response pointer, taxonomy, source version and quote span.
Repeated audits do not create new database annotations.

## Outputs

| File | Use |
| --- | --- |
| `selected_bundle_manifest.json` | Exact taxonomy file hashes, bundle identity and integrity findings. |
| `claims_integration_dry_run.json` | Counts, snapshot identity, unresolved states and zero publication/model usage. |
| `claims_source_linkage_candidates.csv` | Paragraph-level source candidates, including historical versions. |
| `claims_article_linkage.json` | Per-article retained-input coverage and ordered candidate versions. |
| `claims_result_candidates.json` | Response-indexed NC/SC candidates and their original evidence locations. |
| `claims_evidence_review.csv` | Current evidence candidates where available, with definitions, issues and blank human review fields. |
| `audit_output_manifest.json` | Completed output set and per-file hashes. A partial output folder has no completed manifest. |

Keep detailed outputs private: they contain article text and model excerpts.
Review the evidence CSV by `quote_state`, `issue_codes` and source identity;
source-location validation and semantic support have separate review fields.
The CSV is a review artifact, not a publication command.

## Actual September 30 audit

Both runs used the same original-text snapshot. All 806 saved rows match the
810-row input manifest exactly; IDs `410`, `525`, `580`, and `702` have no saved
output. They remain unprocessed. The candidate taxonomy has 502 subclaims,
57 superclaims and 497 valid hierarchy mappings.

The installed 0.4.4 wheel passed 1,258 offline engineering tests, with one
Windows symlink case skipped and 62 integration/live cases not run. Its separate
[package reproduction check](../reports/current_release_offline_reproduction_v0_4_4_20260930.json)
also passed. These checks validate implementation and resources; they do not
measure classification correctness or customer acceptance.

| Check | Strict | Recorded ASCII projection |
| --- | ---: | ---: |
| Article groups with a unique current retained-input candidate | 1 | 8 |
| Paragraph results with a unique current candidate inside the accepted text scope | 0 | 84 |
| Classified occurrences with a unique current original quote and retained-input association | 0 | 37 |
| Classified occurrences with a unique current quote but incomplete retained-input association | 239 | 289 |
| Original candidate quote locations checked against stored bodies | 840 | 1,109 |
| Invalid quote locations | 0 | 0 |

The strict run's single article candidate is outside the accepted retrieval
scope. These are association and location counts, not verified advertising or
greenwashing counts. The decoder retained 762 category occurrences: 199 saved
rows are structurally clean candidates, 605 require review, and two are
quarantined because they contain multiple JSON blocks. Sixty-six occurrences
have snippets absent from their own input. Mapping conflicts and changed
definitions are reported separately. See the [audit receipt](../reports/claims_source_linkage_20260930.json).

## Next integration gate

Resolve source associations, choose the authoritative taxonomy/run and define
what the public label means before importing published results. A located quote
does not prove that it supports the category or that its statement is false.
The website still uses historical annotations; new CLAIMS results are not live.
The [result importer](claims_result_import.md) now validates private review files,
rechecks the current original source, and preserves publication/review history.
The 37 projected candidates have a pending review template; all remain held.
See the [integration plan](claims_integration_plan.md) for product, batch
and read-only MCP work. Social data and customer acceptance remain TODOs.
