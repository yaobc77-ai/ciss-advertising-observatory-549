# Native source input handoff

This handoff preserves the current native source snapshot. It is separate from
the public source archive, database backup and hosted attachment bundle.
The application is 0.4.9; the unchanged source files follow the fixed 0.2.3
source contract. This does not recreate the current retrieval index or saved answers.

## Contents

The private ZIP preserves the seven source files listed in
[Current handoff](current_handoff.md#b-import-from-approved-source-files), the
three native review manifests, the fixed reference
`outputs/pdf265_publication_validation_20260916.json`, and
`eval/development.jsonl`. Its 12 payload files retain their original bytes and
relative paths. `SOURCE_INPUT_MANIFEST.json` and `CONTENTS.sha256` record the inventory.

The three review manifests do not fix every optional input. The spreadsheet,
historical prediction CSV and archive index must also match the complete reference
inventory. A normal import can omit some optional files; that is not reproduction
of this snapshot.

The archive index retains its original 309 legacy absolute candidate paths.
Those paths are not portable attachments and do not establish a correct article–PDF
match. Do not rewrite this fixed source file. The reviewed PDF-265 has a relative
path in the recovery manifest and is included. Other archive PDFs and previews are
excluded; transfer the reviewed [record asset bundle](record_assets.md) separately.

## Offline input check

1. Obtain the public code archive and private input ZIP through the agreed channel.
2. Compare each ZIP's SHA-256 with the sender's separately supplied receipt before
   extracting. A checksum supplied only inside the same ZIP does not authenticate its sender.
3. Extract inputs to a new directory, preserving their relative paths. Install the
   locked application dependencies from the public code archive.
4. Run the existing verifier with the explicit **inputs-only** option and a new output path:

```sh
python /path/to/code/scripts/verify_clean_import.py \
  --inputs-only \
  --root /path/to/native-inputs \
  --output /path/to/new-native-input-check.json
```

This checks the ten source/configuration hashes, builds the native batch using
the three required review manifests, and checks 275 records, 263 countable records,
226 retrievable records and no rejected rows. It reports the reference and
development-file hashes, record identities and historical annotation inventory.
It does not connect to a database, load `.env`, build an index or call a model.
Compare the returned inventory with the sender's receipt. Existing reports are never overwritten.

**Keep `--inputs-only`.** Without it, this historical verifier clears the explicit
`obs_test` database and reproduces the older 558-passage `legacy600-v1` index.
That mode is not a source-file dry run and does not reproduce the current
556-passage `sentence600-v1` index.

## Continue the handoff

After an offline pass, choose one documented data path:

- For a fresh current-source import, use `import-native --root` and inspect its
  report before preparing the intended retrieval profile and embeddings.
- To preserve old text versions, vectors, answers and the usage ledger, restore
  the complete database backup using [the recovery guide](current_handoff.md#a-restore-a-complete-database-copy).

Provision private configuration and mount the selected attachment bundle separately.
Neither the ZIP nor the offline check transfers credentials or records customer approval.
Another implementer's setup, real social data, reviewed CLAIMS2 publication and
independent answer/client review remain pending.

The [1 October handoff check](../reports/NATIVE_HANDOFF_20261001.en.md)
records the actual private ZIP, installed-package rehearsal and selected hosted UI observations.
The [publication receipt](../reports/native_handoff_publication_20261001.json)
provides the two source commits, successful CI and separately verified source ZIP hashes.
