# Reviewed record attachments

Article text lives in PostgreSQL. PDFs and display previews are separate files;
publishing the application code does not deploy those files. A healthy website
therefore does not prove that its attachments are available.

## Build a private bundle

Select record IDs that already appear in `config/native_body_recoveries.json`.
The builder checks the reviewed PDF, extraction and optional first-page preview
hashes. It copies only those selected captures into a new directory, without
changing the database or the original files.

```powershell
uv run python scripts/build_record_asset_bundle.py build `
  --source-root . `
  --destination .runtime/record-assets-release `
  --record-id d340f887-efa7-5746-aaf8-14aabba6b63f
```

Save the returned `manifest_sha256`. Each bundle contains `record_assets.json`,
hash-named PDFs under `pdfs/`, and optional PNG previews under `previews/`.
The manifest binds each capture to its record ID, original URL and exact body
hash. It contains no unreviewed archive candidates or source CSVs.
The existing recovery manifest records AI engineering review, not customer
approval. Exporting a bundle does not change that review status.

## Configure the running application

Copy the selected private bundle to a persistent deployment volume or another
explicitly managed directory. Provisioning that storage and publishing the
selected files are separate deployment actions. Set both server variables:

```text
OBS_RECORD_ASSET_ROOT=/mounted/record-assets-release
OBS_RECORD_ASSET_MANIFEST_SHA256=<manifest_sha256 returned by the builder>
```

Check the deployed files before restarting the application:

Run this inside the running container, using its installed package:

```sh
/app/.venv/bin/python -c "from pathlib import Path; from observatory.asset_bundle import load_bundle; print('verified assets:', len(load_bundle(Path('/mounted/record-assets-release'), 'ACTUAL_MANIFEST_HASH')))"
```

Replace the path and hash with the mounted release's values. The production
image contains the installed package, not the repository's `scripts/`
directory. Volumes are available in running containers; a build or pre-deploy
command cannot validate their contents.

The application validates the entire configured bundle at startup. A missing
file, changed manifest, unsafe path or mismatched hash disables attachments;
article text and browsing remain available. It does not fall back to another
archive. Record detail JSON reports `record_asset_status` as `bundle_verified`
or `bundle_unavailable`. Each PDF and preview is checked again when requested,
and the current record URL and body hash must still match the reviewed binding.

Without `OBS_RECORD_ASSET_ROOT`, local development retains the existing project
manifest and archive paths. The source-link switch also disables bundle links.
Creating a bundle does not fill `archive_url`, verify the current online page,
approve an advertiser's claims or authorize publication of any source file.

For an update, build and check a new bundle directory, update both variables and
restart. Keep the prior bundle until the deployed record/PDF/preview requests
are verified. Check both returned bytes and hashes; startup status alone is
not end-to-end deployment acceptance.

## Verify public attachment responses

Run the read-only checker from a source checkout that has the pinned private
bundle. It does not load deployment credentials or call a model:

```sh
uv run python scripts/verify_record_assets_http.py \
  --root /path/to/private-bundle \
  --manifest-sha256 ACTUAL_MANIFEST_HASH \
  --base-url https://your-application.example \
  --other-record-id ANOTHER_EXISTING_PUBLIC_RECORD_ID \
  --out reports/record-assets-http-new.json
```

For the existing local source workspace, add `--expected-status local_workspace`.
The output must be a new file. The checker verifies the independently computed
body hash, exact record binding, PDF and download bytes, preview bytes, and
404 responses for unknown assets and cross-record access. The other record
must first have a successful public detail response. It follows no redirects
or arbitrary URLs returned by the API. Failed checks exit with code 1 and save
a sanitized report when output storage is available. Preflight parameter or
existing-output errors exit with code 2 before any GET; output-file errors also
exit with code 2 and may prevent the receipt from being saved.

The report deliberately leaves the remote manifest, browser display and storage
persistence unverified. Check the mounted manifest separately with the installed
package, inspect the record in a browser, and repeat file validation in a new
deployment. Preserve both deployment identities and file hashes.
