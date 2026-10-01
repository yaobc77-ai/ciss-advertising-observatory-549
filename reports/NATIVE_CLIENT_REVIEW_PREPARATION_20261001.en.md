# Native client review preparation

Prepared: 1 October 2026. **Pending human review. These are previously used examples, not an independent or frozen acceptance set.**

The team prepared a private, readable review packet rather than requiring reviewers to assemble source IDs or inspect model logs. The [preparation receipt](native_client_review_preparation_20261001.json) binds its files and the current source/index versions.

| Task | Included material |
| --- | --- |
| New York Times native-record count | All 19 source records, titles, dates, source-listed sponsors and article-detail links |
| ExxonMobil publishers | All 15 records, four named publishers and their counts/shares |
| Washington Post sponsors | All 18 records, seven source-listed organization names and their counts/shares |
| ExxonMobil / Washington Post / 2022 | The complete three-record filtered selection |
| Baytown hydrogen and CCS | Actual saved answer from run 125 and its two original quotations |
| Total/GoodPlanet biogas | Actual saved answer from run 126 and its two original quotations |

The record lists come from the already reconciled hosted CSV download. The four quotations were checked again against the same current article versions and their exact character locations and hashes. The two saved answers were generated on **0.4.9**; including them in a packet prepared against **0.4.10** does not make them new predictions. No new model call or database write occurred.

The packet contains `review.md`, six blank rows in `decisions.csv`, `cases.json`, the downloaded record table, two article API responses and a byte-hash manifest. It distinguishes source support, attribution, qualifications, completeness, usefulness and assisted task completion. All reviewer, verdict and approval fields remain empty. The original blank frozen-review intake remains unchanged. Raw source bodies and the working review packet stay outside Git.

The packet has **not been sent to the client**. A reviewer can use the readable page or provide ordinary written comments. The team still needs an actual reviewer and decisions on counting units, organization identities and CLAIMS publication. Real social data and social/cross-collection review, 20 independent frozen questions, the planned human-support threshold, independent user tasks and another implementer's reproduction remain pending. These six examples do not satisfy those gates.
