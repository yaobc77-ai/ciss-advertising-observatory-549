# CLAIMS2 import and delivery engineering — 0.4.5

30 September 2026. This is an engineering update, not a classification-quality
result or client acceptance record. The [machine-readable receipt](claims_import_engineering_v0_4_5_20260930.json)
contains the actual checks and their scope.

## Implemented

- A completed private source audit produces a pending review template.
- The importer binds the audit, selected taxonomy and each source/semantic review
  to their exact definitions and original evidence. Reviewer names are recorded
  assertions; software does not authenticate their identity or approval.
- The database rechecks the current active body, exact quote, accepted retrieval
  range and record/version ownership before the first write. A failed candidate
  rejects the whole transaction.
- Independent CLAIMS2 tables preserve taxonomy, run and import manifests,
  immutable assignments, appended review revisions and retraction events.
  Progressive approvals do not change unrelated assignments. A supported review
  cannot be downgraded by importing an older review file.
- Repeat imports add no duplicate results. Explicit retraction keeps history;
  neither repeat imports nor new reviews reactivate a retracted candidate.
  Changing a review file to hold/reject does not silently retract a prior result.
- Read-only queries return exact distinct article counts, matching assignments,
  category definitions and original evidence. Latest reviews are selected before
  review-state filtering. Public output excludes private model/review payloads.
- `claims_version` tracks analysis changes separately from source/retrieval
  versions. No match is a negative classification or proof of complete coverage.

See the [import workflow](../docs/claims_result_import.md).

## Delivery checks

The actual installed 0.4.5 wheel passed **1,406 offline tests**, with one Windows
symlink test skipped and 79 database/live tests deselected. **22 isolated database
tests** then passed against that same installed wheel: CLAIMS publication and
the existing ordered migration/import regressions. Test data and reviewer
assertions are explicitly synthetic; every test used a new owned schema in an
explicit `obs_test` database.

The [independent package check](current_release_offline_reproduction_v0_4_5_20260930.json)
also passed without model calls. Local migration 3 added six empty CLAIMS2 tables
after a private backup. Native records, source/data/index identifiers and the
556-passage `sentence600-v1` profile stayed unchanged.

Format-2 [frozen evaluation inputs](../docs/frozen_evaluation.md) now support
native, social and cross-collection scopes. Files, review declarations, selected
record/version/body sets and source/index identities are checked before query
dispatch. Duplicate JSON keys, nonfinite values and ambiguous CSV are rejected.
Pending social cases remain pending; verified inputs do not create a semantic
pass. Existing draft mode is preserved. No actual client packet was frozen.

The source archive builder now uses tracked public files, requires Docker,
Railway and CI configuration, includes assets, and records the Git revision,
file hashes and external ZIP hash. Private sources, runtime outputs, backups and
configuration are excluded. Its actual archive receipt follows the committed
code snapshot; the archive itself does not establish project acceptance.

## Real-data and product boundary

The existing projected audit has **37 located candidates**, not 37 verified
claims. A private review template was generated and all candidates remain held:
zero real imports, zero new public labels, zero model calls. Selecting the
authoritative taxonomy/run, approving source associations and reviewing meaning
remain TODOs. Website record/graph displays and the read-only CLAIMS2 MCP tool
are the next integration stage. Real social data and independent client/answer
review remain deferred TODOs. Hosted publication is documented separately from
these local engineering checks.
