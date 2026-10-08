# Record attachments

Article text lives in PostgreSQL. PDFs and previews are separate files. Publishing application code does not deploy them.

## Build a bundle

Use source-reviewed records and their pinned captures. The builder checks the PDF, extraction, and optional preview hashes without changing source files or the database.

```sh
uv run python scripts/build_record_asset_bundle.py build --source-root . --destination /path/to/new-record-assets --record-id RECORD_ID
```

Use an existing record ID with a configured recovery binding. Save the returned manifest hash. The bundle contains `record_assets.json`, hash-named PDFs, and optional previews.

## Configure storage

Copy the bundle to persistent storage and set:

```text
OBS_RECORD_ASSET_ROOT=/mounted/record-assets
OBS_RECORD_ASSET_MANIFEST_SHA256=ACTUAL_MANIFEST_HASH
```

The application validates the configured bundle at startup. Missing files, unsafe paths, or mismatched hashes disable attachments while ordinary browsing remains available.

Each request checks its file and current record binding again. A valid bundle does not fill missing archive links or approve an advertiser's claims.

## Update and verify

1. Build a new bundle directory.
2. Verify its manifest and files.
3. Update both server variables and restart.
4. Open the record, PDF, and preview in the deployed application.
5. Keep the previous bundle until the new responses are verified.

The read-only HTTP checker can verify a pinned bundle against a deployment:

```sh
uv run python scripts/verify_record_assets_http.py --root /path/to/bundle --manifest-sha256 ACTUAL_MANIFEST_HASH --base-url https://your-application.example --other-record-id ANOTHER_RECORD_ID --out /path/to/new-check.json
```

The other record must exist on the deployment. Returned bytes and hashes, browser presentation, and storage persistence require distinct checks. A healthy application does not establish attachment availability.

Image descriptions and video transcripts are derived evidence. They retain their source type and location and are not substituted for original article text.

See [operations](operations.md) and [the data dictionary](data_dictionary.md).
