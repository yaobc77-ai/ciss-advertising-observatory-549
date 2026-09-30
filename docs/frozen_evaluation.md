# Frozen evaluation inputs

Updated: 30 September 2026.

This interface freezes reviewed questions and their source bindings. It does not
approve the product or establish answer quality. The implementation was checked
with synthetic offline fixtures. No real review packet was frozen and no model,
paid API or production database evaluation was run for this change.

## Prepare the review packet

A packet contains `review_plan.json`, `questions.jsonl` and `article_groups.csv`.
The existing [client intake packet](../eval/client_review_20260925/README.md)
shows the fields. Its supplied questions and review declarations are incomplete;
it remains pending. Existing development and acceptance-draft questions are
treated as previously used material.

The review plan records the reviewer, approval reference, source of the tasks,
acceptance criteria, exact `expected_data_version`, and declarations that the
tasks are representative, not used for tuning and grouped by article. These are
human declarations. The software checks their presence and consistency; it does
not authenticate the reviewer or verify that the approval happened.

The plan's `scope` can be:

- `native`: native cases only.
- `social`: social cases only.
- `cross`: native, social and cross-collection cases in one packet.

Every case retains its own dataset and filters. Ready social/cross cases also
require the existing `reviewed_release` object with reviewer, approval reference
and matching data version. Both collections must have active source records for
a ready cross case. This declaration does not substitute for an actual released
and reviewed social dataset. Cases without those materials remain
`pending_social`, with no invented source records, quotes or counts.

## Check and freeze

Replace the example packet and output paths with actual reviewed materials. Run
from the repository root in the project's existing environment:

```powershell
# Inspect local prerequisites. Missing materials return exit code 2.
.\.venv\Scripts\python.exe scripts/prepare_client_review.py --packet eval/client_review_20260925

# Read-only database source and version check; no search or model requests.
.\.venv\Scripts\python.exe scripts/prepare_client_review.py --packet eval/client_review_20260925 --check-sources

# Write a new directory only after review and source checks pass.
.\.venv\Scripts\python.exe scripts/prepare_client_review.py --packet eval/client_review_20260925 --freeze --output outputs/reviewed-inputs-v1
```

A format-2 `manifest.json` binds exact SHA-256 hashes of all packet files,
`reviewed_supports.json` and copied previously used question files. Copying those
prior inputs makes the bundle independent of their original local paths.
Question IDs, order, complete case content, review declarations and article
groups are checked again by the evaluator. Preparation and frozen consumption
reject duplicate JSON keys and nonfinite numbers rather than silently accepting
the last conflicting declaration. Group CSV files require unique `record_id`,
`article_group_id` and `split` columns, with optional `review_note`, and consistent
row widths.

The manifest fixes `data_version`, `source_data_version`, `index_version` and
`active_profile`. The support artifact binds each active source's dataset,
version, body hash and payload hash; each case's complete filtered record set;
and exact original quote locations. Count cases must enumerate the entire
filtered set. Previously used questions, overlapping article groups and exact
normalized-body overlap cannot become an independent holdout by changing IDs.

The destination must not exist. Freezing retains
`semantic_acceptance: pending_human_review` and `overall_pass: null`. Format-1
manifests are not accepted by the new runner because they lack these complete
bindings; prepare a newly reviewed format-2 packet instead.

## Run with the manifest

```powershell
# Free keyword retrieval and deterministic database counts.
.\.venv\Scripts\python.exe -m observatory.evaluate --frozen-manifest outputs/reviewed-inputs-v1/manifest.json --output outputs/reviewed-inputs-v1-lexical.json

# Generated answers require explicit paid authorization.
.\.venv\Scripts\python.exe -m observatory.evaluate --frozen-manifest outputs/reviewed-inputs-v1/manifest.json --paid --output outputs/reviewed-inputs-v1-paid.json

# Also exercise count questions through the actual answer route.
.\.venv\Scripts\python.exe -m observatory.evaluate --frozen-manifest outputs/reviewed-inputs-v1/manifest.json --paid --answer-counts --output outputs/reviewed-inputs-v1-answers.json
```

The question file defaults to the manifest directory's `questions.jsonl`.
An explicit `--cases` file must have exactly the same bytes. Optional
`--expected-data-version` remains available and must also match.

Local files and review prerequisites are checked before constructing the
service. Snapshot identity and current records are checked before search,
statistics or answer dispatch. Frozen files and source/index/profile stability
are checked during and after execution; a changed binding invalidates the run.
The result retains manifest and input hashes, snapshot identity and the recorded
review reference. `gold_status: frozen_inputs_verified` describes input checks.
Without `--frozen-manifest`, existing runs keep `gold_status: draft_not_frozen`.

## Interpret the results

Native, social and cross cases have separate denominators. Pending collection
cases do not enter retrieval/count denominators or call the answer service.
Ready-case failures remain in their applicable denominators. No-evidence cases
in free mode do not measure generated-answer abstention. Paid abstention is a
recorded service behavior, not proof that the corpus lacks relevant evidence.

Quote location, version consistency and exact counts are engineering checks.
Whether an answer is supported, correctly attributed, qualified, complete and
useful still needs the [human answer review](../eval/client_review_20260925/answer_review.csv)
for the exact run and case. The runner leaves semantic review pending and does
not create an overall acceptance verdict.

## Offline checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_frozen_evaluation.py tests/test_evaluate.py eval/client_review_20260925/test_prepare_client_review.py
```

These tests use synthetic inputs and an offline service. They validate the
freeze/runner contract, not customer acceptance or model performance.
