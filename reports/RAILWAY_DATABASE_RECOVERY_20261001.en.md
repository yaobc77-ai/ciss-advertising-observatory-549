# Railway database recovery verification

The complete Railway database was backed up and restored to a fresh database on
the same PostgreSQL service. The [verification receipt](railway_database_recovery_20261001.json)
records 13 passed checks and a checksum-verified private download of the archive.
Production remained connected to its original database.

## What was recovered

| Stored material | Rows in the fixed snapshot |
| --- | ---: |
| Advertising records | 275 |
| Text versions | 828 |
| Stored passages, including historical profiles | 2,071 |
| Embeddings | 1,053 |
| Historical annotations | 1,608 |
| Saved answers | 120 |
| Saved model outputs | 112 |
| Usage ledger | 177 |
| Applied migrations | 3 |
| CLAIMS2 tables | Six tables, all empty |

All 21 user tables matched their restored row counts and content fingerprints.
Schema definitions, owners, permissions, extension versions, retrieval state and
four sequence states also matched. The current profile remained
`sentence600-v1`, with 556 active passages. Total stored passages include earlier
profiles and must not be reported as the current retrieval count.

The restored copy contained 757 saved evidence references and 190 citations.
All located in the correct stored text; 267 evidence references and 94 citations
refer to earlier text versions. Keyword, publisher-filtered, empty-filter and
hybrid retrieval probes matched the source results. The hybrid probe reused a
stored 1,536-dimensional vector. No model request or embedding recalculation
occurred.

These figures describe the Railway snapshot. The earlier
[local recovery report](DATABASE_RESTORE_V0_4_7_20260930.en.md) has different
answer and embedding counts because the two databases have separate histories.
Neither copy was merged into the other.

## Procedure and safeguards

1. Verify the intended Railway project, environment and services. Check native
   PostgreSQL tools and available storage before creating anything.
2. Hold a UTC `REPEATABLE READ READ ONLY` source transaction. Export its snapshot,
   then compute all user-table content fingerprints and stored source locations.
3. Run the PostgreSQL service's native `pg_dump --format=custom --snapshot=…`
   while that source transaction remains open. Use a new private output file and
   include every table, owner and permission entry.
4. Transfer the archive through the existing authenticated SSH connection into
   a fresh private local file. Check its SHA-256 before restoration.
5. Create an explicitly named database that did not previously exist. Restore
   using `pg_restore --exit-on-error --single-transaction`. No `--clean`, drop or
   application connection change is used. Retain a failed target for inspection.
6. Compare the restored inventory, schema, extensions, migrations, locators and
   retrieval probes. Require the recorded checks to pass; a command exiting
   successfully alone is insufficient. Release the exported source transaction.

The server and native tools were PostgreSQL 18.6. The older local PostgreSQL 16
tools were not used to inspect or restore this archive. The normalized source and
restored schema hashes matched. Only randomized `pg_dump` execution-guard tokens
and line-ending differences were normalized.

Table inventories and the archive share the exported snapshot. PostgreSQL
sequences are not MVCC data; this check observed identical sequence state before
the dump, in the restored copy and in the final source observation. Retrieval
probes used separate live read transactions and are reported separately from the
snapshot evidence.

## Backup and retained target

- Private archive: `railway-full-20261001-68b3.dump`, 13,139,934 bytes.
- SHA-256: `273bff93544824337c526e785abcf4f229451194fae6be096998f95883b72f07`.
- Private local storage: the project's ignored `.runtime/backups` directory,
  with a checksum sidecar. The dump is not committed to GitHub.
- Retained recovery database: `observatory_restore_cloud_20261001_68b3` on the
  existing Railway PostgreSQL service.
- Both observed public health responses identify application 0.4.7 at commit
  `6883ea25475aafc3ad5f7a563e267e50f6f363eb`, with unchanged collection, data,
  source and index identities. Local private configuration hashes also matched.

Obtain the archive and required private configuration through an approved private
channel. Verify its sidecar and use compatible PostgreSQL tools. Global roles and
passwords need separate provisioning on another server. Source attachments also
need their separate reviewed bundle; a database restore cannot recreate PDFs.

## Remaining acceptance

This establishes Railway-origin logical recovery into another database on the
same managed cluster, plus an off-service retained backup. Independent-machine
recovery, whole-service disaster recovery and another implementer's reproduction
remain pending. The six empty CLAIMS2 tables do not demonstrate recovery of
nonempty classification histories.

Location checks and retrieval equality do not establish answer meaning, source
identity approval or classification correctness. Real social data, independent
frozen human answer review and customer acceptance are still required for the
complete project delivery.
