# Native input handoff and current demonstration

Checked 1 October 2026. [Machine receipt](native_handoff_20261001.json) ·
[Input instructions](../docs/native_source_handoff.md) ·
[Current demonstration script](../deliverables/native_demo_v0_4_9.en.md)

## Source input rehearsal

The private `native-inputs-20261001.zip` contains 12 payload files and two inventory
files. Its 4,106,391 bytes have SHA-256
`6b8f72533c507a116386a75076afd86133a8abf782a5d2b33c45fff77701ffad`.
CRC and all payload hashes passed. Private data remains outside Git and was not uploaded.

The inputs were unpacked outside the checkout and checked using the installed
0.4.9 wheel and `verify_clean_import.py --inputs-only`. All ten source/configuration
hashes matched; loading produced **275 records, 263 countable, 226 retrievable and
zero rejected rows**, with 275 distinct record IDs and 275 distinct source URLs. The
calculated source identity remained `5114ebc1cf9afe59cdaa715e3ea45166`.
The original bytes remained unchanged after loading.

There are 534 saved historical annotation entries across 267 current article bodies;
two annotations for an earlier PDF-265 body remain separately preserved. These are
not the 37 pending CLAIMS2 candidates and do not establish verified greenwashing.

Network, database, settings, dotenv and model tripwires were not called. The 42
focused historical/offline guard tests passed. The first input check also passed,
but its outer rehearsal harness mistakenly rejected the installed environment
because it was beneath the workspace directory. The completed rehearsal requires
the module to come from `site-packages`, excludes checkout `src`, and preserves
the original report. This is a same-implementer rehearsal, not independent handoff acceptance.

The source contract is the unchanged 0.2.3 input snapshot. The check does not create
the current index, embeddings, historical versions, saved answers or usage ledger.
Database restoration and attachment deployment retain their separate receipts.
The archive index's 309 old absolute paths remain unchanged candidate information;
they are not portable reviewed attachments. The approved PDF-265 file is included;
other candidate PDFs and preview caches are separate.

## Selected current hosted observations

The observations used application **0.4.9**, commit
`bc20d07e03a3d260b814e0e2c8e218f228861361`, `sentence600-v1`, 556 active passages
and the unchanged source/index identities. No generated questions or model calls
were made in this session.

| Task | Observed result |
| --- | --- |
| Full native scope | 263 eligible records, 226 searchable, 22 unknown publication dates. |
| ExxonMobil → publishers | 15 records: Business Insider 5, Washington Post 5, New York Times 3, Wall Street Journal 2. |
| Select ExxonMobil → Washington Post | Five matching article IDs; company share 5/15 (33.3%), outlet share 5/18 (27.8%). The parent distribution remains visible. |
| Washington Post → sponsors | 18 records: API 6, ExxonMobil 5, Shell 2, Southern Company 2, AFPM 1, Chevron 1, Eni 1; two unknown dates. |
| Company + outlet + 2022, unknown dates excluded | Three records. Graph and Overview retained the same three IDs; matrix cell and annual count were also 3. |
| PDF-265 article detail | Original-source link, PDF links, rendered page-one preview and partial-text limitations were visible. |

The selected-count export displayed “Prepared 1 categories for 5 supporting records”
with parent denominator 15. The browser automation's download event timed out after
ten seconds, so no downloaded CSV was inspected. This is an unverified download,
not proof that export bytes passed. PDF browser download was not exercised; earlier
HTTP attachment checks retain their own scope.

Matrix cell clicks and the complete live CCS/biogas answer sequence were not rerun.
The earlier [0.4.9 publication receipt](query_v0_4_9_publication_20261001.json)
separately covers selected query counts, percentages, pagination and HTTP attachments.
Known examples and engineering checks do not establish general answer accuracy.

## Remaining acceptance work

Another implementer must execute setup and reproduction. Real social data,
cross-dataset cases, counting-unit decisions, approved CLAIMS2 publication,
independent frozen answer review and client usability remain pending. The current
script supports native-data review; it is not the final dual-dataset demonstration.
