# Current local database recovery

On 30 September 2026, the current local Observatory database was backed up from an exported PostgreSQL snapshot and restored to a new database in the same project cluster. The application connection and private configuration were unchanged. No model calls were made.

[Complete verification receipt](database_restore_v0_4_2_complete_20260930.json) · [Verifier](../scripts/verify_database_restore.py)

## Verified result

All seven comparisons passed: complete table contents, schema, keyword/hybrid search, unchanged source database, unchanged private configuration, backup hash, and stored source locations.

- All 15 application tables, including profile membership, publication state, migrations and histories, matched.
- 828 body versions, 2,071 stored passages, 1,051 embeddings, 121 saved answers and 176 usage entries were preserved. Stored passage totals include historical and inactive profiles; the active profile still has 556 passages.
- All 729 stored answer evidence references and 189 quotations passed version and character-location checks. This includes 94 quotations attached to older article versions.
- Four nonempty keyword probes, an empty-record filter, and a hybrid probe returned identical evidence. The hybrid probe reused a stored 1,536-dimensional embedding, without generating a new query embedding.
- The source, index and combined data signatures remained unchanged. The active profile remained `sentence600-v1`, with 275 active native records.

The receipt contains fingerprints and counts rather than article text, answers, credentials or connection URLs. The database archive and restored databases remain private.

## Repeat the check

Use the existing owned Windows PostgreSQL cluster and fresh target/report names:

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/verify_database_restore.py `
  --restore-database observatory_restore_new_review `
  --output reports/restore_new_review.json
```

The verifier refuses an existing database or report. It never overwrites, drops, or switches the application database. Concurrent writes or sequence allocation can fail the unchanged-source comparison; the exported backup still represents its fixed snapshot.

## Boundaries

This is local logical recovery in the same cluster. It does not verify the separate Railway database, a different machine, global roles/passwords, the application environment, PDFs or previews. Row/vector equality and exact quotation locations do not establish semantic answer correctness. Real social data, independent human review and another implementer's handoff acceptance remain open.
