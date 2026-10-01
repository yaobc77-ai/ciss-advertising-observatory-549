# Development selftest

This set contains 24 statistics questions and 7 retrieval topics, each asked in English, Chinese and an English paraphrase. The team wrote these cases for development. They are not an independent evaluation set, a human review of generated answers, or the project's formal 20 development + 20 acceptance questions.

## What is checked

Statistics gold comes from independent SQL over current, active, countable native records. A result must be available, have no failure, use database statistics, and match the full filters, dataset, result kind, grouping dimension and values. Zero counts must satisfy the same checks. `no_count` cases check only that unsupported questions avoid a statistics answer; they do not score the meaning of the response.

S19 requires a real percentage: its numerator is the CNBC selection and its denominator is the native selection before the question narrows it. Both counts, scopes, percentage and denominator basis must match. A count or a publisher distribution alone fails. An empty denominator requires a null percentage and an explicit empty-selection status.

Retrieval gold is every active, countable, retrievable native record containing the case's phrase inside an accepted source interval. Excluded navigation and gaps do not count. Records without active chunks remain in gold so missing index coverage remains visible. A variant passes when a gold record appears among the top five distinct results. An empty gold set is an invalid check, not a pass. This metric does not establish citation quality or generated-answer correctness.

## Run

| Check | Command | Access |
| --- | --- | --- |
| Case format, planner scopes and runner regressions | `python -m pytest tests/test_selftest_cases.py tests/test_selftest_runner.py` | Offline; no database or API |
| Rule statistics and keyword retrieval | `python scripts/run_selftest.py` | Configured database; no model calls |
| Selected rule cases | `python scripts/run_selftest.py --rules --ids S01,S07,S19` | Configured database; no model calls |
| Selected research-agent answers | `python scripts/run_selftest.py --agent --paid --ids S01,S07,S19` | Configured database and paid API |
| Selected hybrid retrieval | `python scripts/run_selftest.py --hybrid --paid --ids R01` | Configured database and query embedding API |

`--ids` accepts distinct known IDs and keeps case-file order. Every requested mode must have selected cases. The application budget and visitor quotas still apply. Research-agent questions can use several model calls; pacing between questions does not guarantee staying below the call quota. Paid runs stop after an unavailable answer or an exception. They do not bypass limits or retry failed cases automatically.

`rules: "gap"` marks a known planner gap. Offline CI treats these as strict expected failures, so a fix requires changing the corresponding case to `"pass"` after its scope checks pass.

## Receipts and failure handling

Each run reserves a fresh `outputs/selftest-<timestamp>-<id>.json` before accessing the configured services. It then replaces that run's receipt atomically after each case and retrieval variant. Earlier runs are preserved. Pending cases stay in the score denominator, and completed-case counts are reported separately.

Receipts bind the case version and bytes, runner bytes, Git commit, relevant working-tree changes, and source-module hashes. Source data version, retrieval index version and active profile are recorded before and after the run. A change invalidates the run while retaining partial results. This is a change guard; it is not a shared, frozen database snapshot across service calls.

Wrong results, invalid gold, incomplete runs, interruptions and exceptions return a nonzero exit code. Exceptions are recorded by type without connection strings or exception messages. Known answer costs and embedding cost-sink values are recorded; the usage ledger is authoritative for additional or uncertain exposure after an exception. If writing a receipt itself fails, the command fails and the last valid receipt remains.

Historical result files retain their original rubric. Version 2 removes the earlier count/distribution alternative for S19. Regrading old responses under another rubric is not a new live result. No before/after performance claim is made here. Formal evaluation and customer evidence review remain separate.
