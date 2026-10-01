# Current local database recovery: 0.4.7

The 0.4.7 local database was exported from one PostgreSQL snapshot and restored to a new database in the same project cluster. All 21 user tables, row-content fingerprints, schema, sequences, extensions and retrieval state matched. The main database and private application configuration stayed unchanged. No model requests were made.

[Full restore receipt](database_restore_v0_4_7_complete_20260930.json) · [Migration and scope checks](database_restore_v0_4_7_scope_20260930.json) · [Verifier](../scripts/verify_database_restore.py)

## What was preserved

| Stored data | Rows |
| --- | ---: |
| Records | 275 |
| Original text versions | 828 |
| Passages, including historical profiles | 2,071 |
| Embeddings | 1,051 |
| Saved answers | 121 |
| Generation outputs | 110 |
| Usage ledger entries | 177 |
| Historical annotation entries | 1,608 |
| CLAIMS2 tables | 6 tables, all empty |

All 729 saved answer evidence references and 189 citations resolved to their stored text versions. This includes 267 historical evidence references and 94 historical citations. The active `sentence600-v1` profile retained 556 passages; this differs from all stored passages.

Four keyword queries, one empty-record filter and one hybrid query returned identical evidence from the original and restored databases. The hybrid check reused a stored 1,536-dimensional vector. It did not request a new embedding or evaluate relevance for a newly embedded question.

A subsequent read-only check confirmed that both databases had the current three migration files applied, with matching checksums and no pending migration. Their full inventories still matched the exported snapshot.

## CLAIMS2 coverage

All six new tables and the review revision sequence were recovered. The tables contain no published real CLAIMS2 data. This proves empty-table structure and state recovery, not recovery behavior for nonempty assignments, review upgrades or retractions. Source identity and semantic review remain pending.

## Verification boundaries

The inventory and dump share one exported snapshot. Search probes use separate live read transactions; unchanged source inventories before and after constrain this comparison but do not make every probe part of the exported transaction.

This is local logical recovery in the same cluster. It does not establish recovery of the Railway database or another machine, global roles/passwords, private environment configuration, PDFs or previews. Exact quotation locations and preserved vectors do not establish semantic answer accuracy. The older [15-table recovery report](DATABASE_RESTORE_20260930.en.md) retains its 0.4.2 scope.

The dump and restored database remain private. Repeat the existing verifier with a fresh target database and output filename; it refuses overwrites and never switches the application connection.
