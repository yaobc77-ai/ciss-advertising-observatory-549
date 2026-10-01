# Native exports and installed runtime handoff

Checked: 1 October 2026. Application: **0.4.10**. These are team engineering checks of selected native-data tasks, not independent client acceptance.

## Actual hosted CSV downloads

The [download reconciliation receipt](native_export_reconciliation_20261001.json) records four files downloaded through the hosted browser:

| Selection | Records CSV | Company–outlet cross-tab | Reconciliation |
| --- | ---: | ---: | --- |
| All native records, all dates, unknown dates included | 263 distinct records | 20 sponsor categories × 8 outlets; 160 cells | Every cell and both sets of totals match the records CSV |
| ExxonMobil, The Washington Post, 1 January–31 December 2022; unknown dates excluded | 3 records | 1 cell with 3 records | The same three record IDs appear in the selected matrix drilldown |

The full records file has 226 searchable records and 22 dates represented as `(Unknown)`. Required URL, publisher, title, date, sponsor and collection-search-term fields are present. Cell reconciliation joins the records CSV's displayed sponsor to the cross-tab's **Sponsor / organization** display label and the same outlet name; the cross-tab also retains its raw source-sponsor value. Sponsor categories include the missing-value category; the graph's 19 named sponsors use a different denominator.

This verifies downloaded file contents and one full-scope and one combined-filter export. It does not mean that all 160 cells were individually clicked, every export mode was tested or the client approved the counting unit and organization identities. Successful CSV downloads do not resolve the separate Edge PDF download failure.

## Fresh installed runtime, restored database and selected attachment

The [runtime receipt](native_runtime_handoff_20261001.json) combines previously delivered artifacts outside the application checkout:

- The 0.4.10 source archive at application revision `c949b797ece83d7e31b586e2b9082b62ffa290d2`, with all 458 entries, safe paths and checksums verified.
- A new Python environment with locked dependencies installed from the local package cache and the delivered wheel installed without editable source imports.
- The existing verified local restored database, connected with default read-only transactions.
- A byte-identical copy of the three-file, reviewed PDF-265 attachment bundle.

The application started twice on a separate loopback port. After terminating only the harness's first child process, a second process became ready and repeated the selected tasks:

| Task | Before restart | After restart |
| --- | --- | --- |
| ExxonMobil publisher distribution | 15 records across 4 outlets | Same counts and distribution |
| ExxonMobil → The Washington Post | 5 specific articles; 5/15 and 5/18 shares | Same five record IDs and denominators |
| Free `biogas` keyword search | 3 passages with source-record links | Same three records |
| PDF-265 record | First-page preview rendered | Preview rendered again |
| Selected attachment HTTP checks | Body, PDF, download and preview bytes match; cross-record isolation passes | Same checks pass |
| Article-version/annotation graph API | 5 records, 46 nodes, 101 edges | Parsed JSON response matches |

The API inspector graph is distinct from the collection graph shown in the Data page. Both startup health responses have the same source, data and index identities. Four complete inventories of the restored database match its fixed snapshot across all 21 tables. No new model call, database write, import or indexing occurred. Private configuration remained unchanged. Both owned processes were stopped and the temporary port closed.

Windows termination returned exit code 1 for each owned child; the receipt preserves this. The check demonstrates controlled stop and restart, not graceful shutdown or production failover.

## Documentation and remaining acceptance

The [setup and user guide](../docs/guide.md), detailed user guide, [current handoff](../docs/current_handoff.md) and [native demonstration](../deliverables/native_demo_v0_4_9.en.md) now describe the current hosted release, record pagination, citation numbers, export results and observed limits. Historical source archives retain their original contents and hashes.

This rehearsal used the same implementer and machine. Another implementer's reproduction, another-machine or whole-service recovery, complete source/archive recovery, real social-data tasks, approved CLAIMS publication, independent frozen semantic review, customer counting/entity decisions and the final two-dataset demonstration remain pending. The hosted PDF download is still blocked by Edge with `ERR_BLOCKED_BY_CLIENT`; the preview and HTTP byte checks are separate evidence.
