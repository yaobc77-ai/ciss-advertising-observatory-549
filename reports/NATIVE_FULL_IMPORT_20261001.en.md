# Complete native source import rehearsal

Checked: 1 October 2026. Application **0.4.10**, installed from its wheel outside the checkout. The [receipt](native_full_import_20261001.json) binds the application, dependencies and checks. This is a same-implementer, same-machine rehearsal.

All 12 private input payloads matched their fixed hashes. The installed CLI applied migrations through version 3 and imported the full native batch into a new, dedicated local test database. It then repeated the same upsert import.

| Check | Result |
| --- | --- |
| First import | 275 new versions; no unchanged or rejected records |
| Repeat import | No new versions; 275 unchanged records; no rejected records |
| Current source counts | 275 stored, 263 countable, 226 retrievable |
| Identity and text versions | Match the fixed source input; unchanged on repeat |
| Existing databases | All 21 main-database tables and 15 existing test-database tables, sequence states and retrieval snapshots unchanged |
| Model activity | No calls, new embeddings, generated answers or model fees |

The repeated import adds its own audit entry and may advance the retrieval-preparation timestamp. It does not duplicate records, text versions, annotations or passages. The new test database remains available; existing application connections and private configuration were not changed.

**A fresh import initializes `legacy600-v1`: this test contains 558 passages and zero embeddings.** It does not recreate the production `sentence600-v1` profile with 556 active passages. Preparing that profile, generating vectors and preserving old answers and usage are separate operations. Use a complete database restore when those histories must be retained.

This closes the current-package full-input import and idempotency check. Another implementer's reproduction, real social data, reviewed CLAIMS2 publication, independent answer review and client acceptance remain pending. The [input handoff guide](../docs/native_source_handoff.md) explains the separate source-file and database paths.

## Maintenance verifier correction

The rehearsal exposed a separate defect in `scripts/verify_current_release.py --database`: a random schema followed by `public` in the search path could reach existing same-name tables. The source import above used a dedicated database and did not invoke that route.

The helper now rejects conflicting public relations before schema creation and checks every application's table resolves to its intended schema after migration. Its [focused check receipt](release_database_guard_20261001.json) records 37 passing regression tests, Ruff and an actual read-only refusal of the 15 existing test-database conflicts. No database writes or model calls occurred during that guard check. A complete successful synthetic database run after this correction was not performed; this result does not replace the earlier full-source import receipt.
