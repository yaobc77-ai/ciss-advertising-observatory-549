# Input and import contract

The application accepts source-backed native advertisements and social-media company posts. Preserve originals and prepare canonical records before import.

## Required identity

Each canonical record has a stable `record_id` and a `dataset` value of `native` or `social`. Retain original URLs, source hashes, and source-row or observation provenance.

| Input | Preparation |
| --- | --- |
| Native tables and saved pages | Preserve original metadata; admit reviewed text and retrieval ranges |
| Nested social JSON or ZIP | Prepare canonical posts while preserving saved observations |
| Canonical JSONL | Validate each `RecordInput` before import |
| CLAIMS outputs | Audit and bind separately to source versions |
| PDFs and previews | Supply a separate verified attachment bundle |

## Metadata and text

Keep publication date, publisher, sponsor/company value, collection search term, platform, and account as distinct fields. Missing dates remain missing. Do not replace a source date with the collection time.

Store original body text unchanged. Retrieval ranges select usable material without rewriting its source positions. Counting and retrieval permissions are separate.

For social media, distinguish unique posts from multiple saved observations. Preserve conflicting text and metadata instead of silently choosing one source.

## Validate first

```sh
uv run observatory import-records /path/to/records.jsonl --dataset native --dry-run --out /path/to/new-validation.json
```

A dry run validates rows without opening the database. Review rejected rows and issues before adoption. Use new output paths and retain the input hash.

## Import

Apply ordered migrations before importing into the chosen database. `upsert` retains current records and adds changed versions; `snapshot` is a distinct collection-adoption operation. Confirm the intended scope before using it.

An import does not generate vectors, publish attachments, approve CLAIMS assignments, or establish that a social post was a paid advertisement.

## Retrieval

Sentence-aware passages preserve source offsets and retrieval ranges. Embeddings and keyword indexing are versioned independently from the original text. Reindexing is an explicit maintenance step and may incur API charges.

After an update, reconcile imported records, counting units, missing fields, usable text, and rejected rows. Test against a private database before changing an application database.

See [the data dictionary](data_dictionary.md), [record attachments](record_assets.md), and [operations](operations.md).
