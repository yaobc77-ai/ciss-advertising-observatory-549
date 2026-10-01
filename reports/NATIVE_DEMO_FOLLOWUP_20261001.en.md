# Native walkthrough and citation display — 1 October 2026

The hosted **0.4.9** application at `ebb350c` completed the matrix-cell, historical-label and article-graph checks described below. Two existing development questions returned source-supported answers. These are team checks, not independent customer acceptance. [Structured receipt](native_demo_followup_20261001.json).

## Current native walkthrough

- Selecting **ExxonMobil × The Washington Post** in the full native matrix showed five specific articles, matching the graph's five relationship records.
- Selecting historical **Decreasing emissions** showed 84 matching records and ten titles on the first page. These labels are unverified automated annotations; this is not a finding of greenwashing.
- Article mode for **Capturing carbon around the world** showed the directed sponsor and publisher relationships, one supporting article, and the selected focus after **Fit**.
- The reviewed PDF-265 record still showed its first-page preview. PDF, download endpoint and preview bytes passed fresh HTTP checks. An actual browser download was blocked by Edge with `ERR_BLOCKED_BY_CLIENT`; no bypass was attempted. The selected bundle was already uploaded. Existing CLI authentication was confirmed, so no repeat authorization or upload was performed.

## Two live development questions

| Question | Observed answer | Source and latency | Actual answer cost |
| --- | --- | --- | ---: |
| Baytown blue hydrogen, natural gas and construction status | The advertisement combines natural-gas hydrogen production with CCS and describes the new facility as a plan to build. | WSJ source record `0d2b4bc2-4397-5604-8235-fcda8c7088f7`; run 125; 8.471 seconds | $0.00223396 |
| Total/GoodPlanet biogas feedstock and location | Livestock slurry; Adilabad district, India, attributed to the advertisement. | CNBC source record `01374b51-3fae-5fe7-8c60-b4bffa2b7ba0`; run 126; 6.169 seconds | $0.00266663 |

All four quotations matched their retrieved evidence, and all 18 returned evidence objects passed current original-version checks. An AI-assisted reading found support for these four stated facts. This does not measure the required independent human support rate, establish population accuracy, or determine the facilities' present factual status. The complete raw responses remain private. No prompt, retrieval, source or index change was made.

## Citation display correction in 0.4.10

The live answers exposed a display defect: answer markers use claim order, while evidence cards used retrieval order and showed only the first quotation on a card. In the Baytown answer, both citations belonged to retrieval rank 2. In the biogas answer, both belonged to rank 1. A reader could mistake `[2]` for retrieval result 2 and miss the second supporting quotation.

The display now preserves every valid **Citation [n]** next to its exact quotation, and labels retrieval order **Retrieval rank**. Repeated or multiple quotations retain their numbers. Invalid references are not given citation labels, and later valid references are not renumbered. Uncited passages retain their retrieval excerpt; source-link settings still apply.

The focused source suite passed **60 tests**, including seven new checks. Ruff passed. A local browser rendered the actual saved live answers with both quotations visible on the correct cards; it made no new model or database calls. This saved-answer display check is separate from the two preceding live generations. Publication and installed-artifact checks are recorded separately when available.

## Remaining decisions and checks

Real social and cross-collection examples, reviewed CLAIMS2 publication, independent frozen questions and human/client review remain TODOs. Browser PDF download, other export modes and another implementer's reproduction are still unaccepted. Do not replace the final two-dataset demonstration with these native development checks.
