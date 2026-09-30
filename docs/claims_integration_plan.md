# CLAIMS integration plan

Updated September 30, 2026. Status: read-only source audit and candidate decoder
implemented; database import and product integration remain scheduled. No
CLAIMS 2 results have been newly classified, imported or published by this work.

## Current scope

CLAIMS integration is now part of the requested work. The original FA26 brief
placed the complete CLAIMS backend in a later semester; that is the historical
scope, superseded for this work by the client's new request and the user's
instruction to schedule integration of the available repository.

Use the existing CLAIMS implementation and saved results. Do not retrain a
model or replace the current Observatory stack. Classification runs offline
against a fixed article and taxonomy version. The website performs real-time
RAG over published records and analysis results; a question does not rerun
classification or modify the taxonomy.

Formal social data and customer acceptance materials remain TODO items. They
do not prevent auditing and implementing the native-article integration.

## What the existing repository contains

Read-only inspection used the supplied `ml-ciss-native-ads-main.zip` and its
extracted directory. The ZIP contains 243 files and has SHA-256
`d15f8e558c13d168c73edebdc65b490a3ed3e6c417aca7c64987da7202a14e2c`.
All 61 files inspected under `CLAIMS_2.0_model/src/dashboard/` and `src/data/`
match their ZIP entries. No upstream code or model request was executed.
The [source-bundle inventory receipt](../reports/claims_source_bundle_audit_20260930.json)
records the full file hashes, reference integrity and read-only SQLite counts.

| Component | Actual role | Integration treatment |
|---|---|---|
| `cal-open-coding/greenwashing_builder.py` | Offline LLM extraction; stores per-input results in SQLite and evolves the NC/SC taxonomy while processing | Reuse its extraction contract and prompts behind an isolated batch adapter; taxonomy changes must remain unpublished proposals |
| `src/dashboard/backend/app.py` | FastAPI analysis and proposal-management API; ranks taxonomy texts with TF-IDF, optionally calls an LLM, and persists proposals | Reference implementation, not a required second public web application |
| `src/dashboard/backend/llm_confidence.py` | Scores whether a subclaim belongs under a superclaim | Its score does not validate whether the input paragraph expresses that claim |
| `src/dashboard/script.js` | Tries `/api/analyze`; on failure compares pasted text to historical snippets with overlap/longest-common-substring matching | Keep fallback results explicitly heuristic; do not silently publish them as LLM classifications |
| Four taxonomy JSON files | NC definitions, SC definitions, NC-to-SC mappings and definition/history events | Pin one complete, internally consistent bundle |
| `subclaim_bertopic_collapse.json` | Precomputed clustering/similarity suggestions | Optional taxonomy maintenance aid; not evidence that an article contains a claim |

The dashboard README describes an older static-only interface. The inspected
code now includes `/api/analyze` and proposal actions; use the code contract
rather than assuming the README is current. Without an API key, backend analysis
can still return its best TF-IDF match and persist proposals. With an API key,
its first scoring call checks NC-to-SC compatibility, not paragraph-to-NC
support. Another path extracts/maps a standalone paragraph and assigns a fixed
`0.6` score. None of these scores is a calibrated correctness probability.

The upstream builder also creates or updates categories during normal runs.
Calling it directly against the published taxonomy would change the meaning
of later results. Integration needs a frozen taxonomy and a separate proposal
output, not unrestricted calls to the existing analyze/apply endpoints.

## Taxonomy versions are different

The short fingerprints below reproduce the upstream filename-and-byte hash
algorithm. They identify inspected bundles; the integration manifest must also
retain full SHA-256 hashes for each file.

| Directory under `CLAIMS_2.0_model/` | Fingerprint | NC | SC | Mapping rows | Observed integrity |
|---|---|---:|---:|---:|---|
| `src/dashboard/` and `src/dashboard/backend/` | `d2908e965057` | 502 | 57 | 124 | References exist; 378 NC definitions have no SC mapping |
| `src/data/` | `c71de7fbf6e2` | 502 | 57 | 497 | References exist; 5 NC definitions have no SC mapping |
| `src/data/sentence_run_Mar6/` | `038769a58643` | 4,114 | 558 | 4,092 | 24 defined NC IDs are unmapped; 2 mapping keys are absent from the codebook, and targets include 6 distinct absent SC IDs |

The two 502-NC bundles have identical codebook, superclaims and history bytes,
but different mapping files. Combining the dashboard mapping with another
run's results silently loses hierarchy coverage. The larger March bundle is
not interchangeable with the paragraph bundle and needs its own integrity
report. Do not choose a bundle merely because it has more categories.
The unresolved March keys are `NC_026` and `NC_232, NC_2432`; the latter is a
combined string, not a valid single category reference. Do not silently split
it or change zero padding to repair the source.

Full hashes for the inspected `src/data/` paragraph candidate:

| File | SHA-256 |
|---|---|
| `greenwashing_codebook.json` | `36ce5904f2c45002b3e91608f2e68c578a7d27253594e8fce6243fed1144cac7` |
| `greenwashing_superclaims.json` | `d923cb594ec2b51bb079b7a657637fdb4500f301dbcd81978a2a2ad2a147d493` |
| `claim_superclaim_map.json` | `18b1dc8f15c6db358ae8260cc7b6d831ad07263f6013c13884ee271d9308f494` |
| `greenwashing_claim_history.json` | `8cef59b5ecda4627cfdd1e8796cc4351e3c69d443eba4dce52767e2d06fc986a` |

This is a candidate for a first dry run, not a declaration that it is the
client's authoritative taxonomy. Record the selected run and owner before
publishing classifications. That decision does not block the linkage audit.

## Saved outputs need source linkage

| Material under `src/data/` | Verified contents | Limit |
|---|---|---|
| `sampled_50_articles_paragraphs.csv` | 810 rows; fields `id`, `article_id`, `text`; 50 article IDs | Native article paragraphs, not a formal social export |
| `native_ads_paragraphs_cleaned.csv` | 3,905 rows; fields `id`, `text` | No article URL or explicit article ID column |
| `greenwashing_discourse_analysis.db` | 806 `post_analysis` rows; 50 `metadata_json.article_id` values | Every URL, platform and parent entity is `unknown` |
| `sentence_run_Mar6/greenwashing_discourse_analysis.db` | 10,740 result rows; 246 metadata article IDs | Every URL, platform and parent entity is `unknown` |
| Paragraph claim history | 502 claim entries and 756 history events; 740 URLs are `unknown`, 16 are `N/A` | Zero usable HTTP source URLs; history events are not a complete article-label table |

Use the per-input SQLite rows and raw responses to reconstruct analyses.
Definition changes, seed events and repeated matches in claim history do not
each represent another advertisement. `source_article_id`, paragraph `id`,
metadata `article_id` and Observatory `record_id` are different identifiers.
Their equality must never be assumed.

First match each saved input to its exact supplied input row. Then reconstruct
article candidates from source text and metadata. A unique match in the current
article body is useful evidence, but titles or numeric IDs alone do not certify
identity. Preserve ambiguous, absent and changed-source cases in a report.
Do not force a result onto a current version when it only matches an older body.

## Required data contracts

### Taxonomy bundle

Store the source repository/archive identity, all four file hashes, a full
bundle manifest hash and exact NC/SC definitions. Preserve IDs as provided;
do not renumber categories or map them into the legacy twelve labels. Each
NC-to-SC edge belongs to this bundle. A missing parent is `unmapped`, not a
guessed SC. Invalid references prevent that edge from being published.

### Classification run and result

| Field group | Required values |
|---|---|
| Source identity | `record_id`, `version_id`, `body_hash`, original URL, dataset |
| Run identity | Unique `run_id`, `system=CLAIMS2`, upstream code revision/hash, input manifest hash, taxonomy bundle hash, model and prompt identifiers, timestamps |
| Processing method | `saved_upstream_result`, `llm_batch`, `tfidf_heuristic` or `browser_heuristic`; never infer the method from a numeric score |
| Category result | Exact `nc_id`; `sc_id` and mapping status from the same pinned bundle; original raw output retained privately |
| Evidence | Exact original quote, half-open Unicode `start`/`end`, paragraph ID and source version; `body[start:end] == quote` |
| Source association | `exact`, `ambiguous`, `not_located` or `version_mismatch`, with the matching basis and upstream file/row/JSON pointer |
| Processing state | `matched`, `no_match`, `failed`, `unprocessed` or `needs_review`; a completed no-match result is distinct from an absent result |
| Review state | `automatic_unverified` or a documented human review, with reviewer and date; mechanical source checks are recorded separately |
| Optional score | Value plus its method and target, such as lexical similarity or NC-to-SC compatibility; no unsupported probability label |

Upstream paragraph splitting collapses whitespace. The adapter must preserve
an explicit mapping to original character positions, or send unchanged spans.
It cannot search a normalized quote and then reuse that position in the raw
body. Multiple possible quote locations remain unresolved. Evidence also
respects existing excluded navigation intervals and partial-body restrictions.

Store derived results separately from immutable articles and historical CLAIMS
1 labels. The importer uses an idempotent key containing the run, source
version, category and evidence span. A new body or taxonomy creates a new
analysis version; it does not overwrite the prior batch.

## Implementation order

| Stage | Work | Reviewable completion evidence |
|---|---|---|
| **1. Implemented: bundle and linkage audit** | Read-only SQLite and original-text snapshots; exact input joins; retained paragraph order; strict and explicit versioned ASCII-projection comparisons | [Actual audit](claims_source_audit.md) and [receipt](../reports/claims_source_linkage_20260930.json); private source/evidence review files, zero database writes and model calls |
| **2. Candidate adapter implemented; importer pending** | Strict taxonomy and original-response decoding, version/quote checks, stable candidate keys and review CSV; preserve unresolved and changed-definition states | Candidate dry runs completed. Published-result importer and repeat-import verification follow authoritative bundle/run and source-binding decisions |
| **3. Read-only product integration** | Show published NC/SC definitions, run and review status in record details; add claim filters, coverage and count exports; add typed article-to-claim and NC-to-SC graph edges | Counts reconcile to distinct current records; each selection opens the matching articles and exact evidence, not just a total |
| **4. Offline processing for remaining articles** | Reuse the upstream extractor through a narrow frozen-taxonomy adapter in an isolated run directory; reserve cost and log usage; classify new or changed eligible bodies only | Saved bounded-batch results, per-input failures, source checks and an explicit publication report; new taxonomy proposals stay separate |
| **5. RAG and regression verification** | Add a read-only claims tool and optional taxonomy-aware retrieval; generate answers from original source quotes; compare retrieval with and without the new analysis filter | Counts and ordinary searches still work; unanalysed articles remain searchable; source location and semantic support are evaluated separately |

Stage 3 depends on valid stage 2 imports, not on a full new model run. Stage 4
can fill uncovered articles after the saved-output integration works. New
proposal approval or taxonomy editing should remain a separate maintenance
workflow; public questions cannot apply a merge or mutate category definitions.

For the client's question, “Which native ads contain greenwashing claims?”,
return articles with published CLAIMS matches, their category definitions,
source quotes, analysis coverage and review state. Explain that automatic
matches identify the model's taxonomy assignments; they do not independently
establish that a company statement is false or legally misleading.

## First actionable step and remaining TODOs

The bundle inventory, backend/fallback inspection and first article-to-result
linkage audit are complete. The implemented `claims-audit` command produces:

- A selected-bundle manifest based on the existing inventory: full hashes,
  category counts and invalid mappings.
- `claims_source_linkage_candidates.csv`: saved result IDs, exact input matches,
  candidate record/version IDs, quote positions and unresolved reasons.
- `claims_integration_dry_run.json`: candidate and quarantine/review counts,
  with no production database changes.

These outputs and category-specific evidence review files have been generated
privately for the paragraph bundle's 806 saved results. All input joins are
valid. Strict matching finds one current retained-input article candidate,
outside the accepted retrieval scope. The explicit recorded ASCII projection
finds eight article candidates, 84 in-scope paragraph candidates and 37 classified
occurrences with a unique current original quote and retained-input association.
These are not approved source associations or verified classifications.

The decoder preserves 762 indexed category occurrences and quarantines two
multi-JSON-block responses. It separates snippets absent from the input, partial
article matches, historical versions, NC/SC conflicts and definition drift.
Four missing saved outputs remain unprocessed. See the [audit guide](claims_source_audit.md)
for output meanings and the exact completion boundary. The March sentence run
remains a separate candidate; its malformed mapping is not silently repaired.

The next step is to review the available source bindings and authority/version
choices, then implement a published-result importer and read-only product tools.
Partial text evidence must not be promoted to a complete retained-input
association. A domain review must separately decide whether a quote supports
its classification.

TODOs requiring customer or data-owner input are the authoritative taxonomy/run,
any missing source-ID crosswalk, the intended meaning of a published CLAIMS
match, a domain reviewer and acceptance examples. Formal social data and its
field definitions remain a separate TODO. Existing materials can be audited
without asking for duplicate archives or waiting for those acceptance items.

Reuse preserves applicable upstream license and notices. The dashboard README
metadata says MIT, while its inspected `LICENSE` file is Apache 2.0; record
the relevant file-level provenance before redistributing adapted code.

Sources: supplied archive and the inspected upstream paths above; earlier
[offline/online design](https://github.com/yaobc77-ai/ciss-advertising-observatory/blob/main/docs/CLAIMS_OFFLINE_RAG_ONLINE_20260925.zh-CN.md) and
[source review](https://github.com/yaobc77-ai/ciss-advertising-observatory/blob/main/reports/UPSTREAM_SOURCE_REVIEW_20260918.zh-CN.md) remain
historical context. Source inspection and audit did not change a taxonomy,
label or source database; maintenance package publication is recorded separately.
