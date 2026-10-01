# Native advertising demonstration — 0.4.9

Prepared: 1 October 2026. **Draft for native-data review. This is not final project acceptance or the final demonstration of both datasets.**

[Open Data](https://ciss-advertising-observatory-production.up.railway.app/data) · [Open Query](https://ciss-advertising-observatory-production.up.railway.app/query) · [Project page](https://yaobc77-ai.github.io/ciss-advertising-observatory-549/)

The full sequence remains incomplete. Selected steps were checked on the hosted **0.4.9** application in this round, at documentation commit `bc20d07`. Its evidence has three scopes:

- **Current native UI walkthrough:** full native scope, both organization distributions, the ExxonMobil–Washington Post relationship and the combined 2022 filter in Knowledge graph and Overview. See the [current walkthrough report](../reports/NATIVE_HANDOFF_20261001.en.md). This is a team check, not independent client acceptance.
- **0.4.9 publication checks:** hosted NYT record pagination, CNBC percentage pagination, and HTTP access to one selected PDF and preview. See the [publication receipt](../reports/query_v0_4_9_publication_20261001.json).
- **Remaining session checks:** matrix-cell clicks, exported file contents, the article-mode/Fit sequence, complete live CCS/biogas semantic review and independent client tasks. Earlier [0.4.2 material](native_demo_v0_4_2.en.md) remains historical; use the current observations below for sponsor counts. A known development question or an engineering check does not establish semantic accuracy or customer approval.

## Before the session

1. Open [health](https://ciss-advertising-observatory-production.up.railway.app/healthz). Record the application version and commit, data version, index version and active profile. The earlier publication receipt identifies **0.4.9**, commit `fe20c9204af9a2e3f44820943c76dc7bbea18b6c`, and `sentence600-v1`; this round's UI observations follow documentation commit `bc20d07`. Keep the two observations separate and record the actual release before repeating the tasks.
2. Use the hosted application. Its 0.4.9 deployment is recorded; the existing local port 8050 was not verified as reloaded to this release.
3. Select **Native advertising**, clear company/outlet/date/label filters, and include unknown dates. Use **Current collection and its filters** for Query examples. Examples fill the question box; they do not reset filters or submit the question.
4. Read the displayed scope. The release baseline has **275 active records and 556 active passages**. This round's full native UI selection showed **263 eligible records, 226 with searchable text and 22 unknown dates**. These are different measures; record the values again if the collection changes.
5. Check the available API budget before submitting generated questions. Data interactions, exports, keyword search and statistics pagination make no model calls. **Generate answer** uses a paid model to understand the question and choose tools; the database calculates the totals.
6. Let the reviewer attempt each task first. Record help given and failures. Do not count assisted completion as independent completion or replace a failed live answer with an undisclosed saved answer.

## Q1. Compare native ad volume by company and news outlet

Client question: **How can users compare the volume of fossil fuel native advertisements by company and news outlet?**

1. Open **Data → Overview**. Read the company–outlet matrix, its values and legend.
2. Select a cell. Check that the matching record list names that company and outlet, rather than displaying only a collection total.
3. Download the counts and compare the selected row with the displayed count. Explain that the counting unit is currently eligible source records. A source-noted NYT/BP duplicate candidate is awaiting a decision on records versus distinct articles/landing pages; no merge has been applied.

**Current check boundary:** the filtered matrix value was inspected in Q3 below. Clicking a matrix cell and verifying the resulting scope have not been checked in this round. Count-export preparation was observed in the graph, but no downloaded file was obtained; file contents remain unverified.

Then open **Query**, restore the full native scope, and submit:

> How many native ads are from the New York Times?

The **0.4.9 hosted receipt** records 19 eligible NYT records. Open **Inspect matching records**: the first page had ten titles and the next page nine distinct titles. Use **Next**, **Previous** and **First page** to inspect all matching records. Open a title. The total includes eligible metadata-only records, not just retrievable bodies.

If the budget permits, also submit:

> What percentage of native ads are from CNBC?

The recorded full native selection returned **116 / 263 = 44.11%**. Check the stated denominator. Matching-record pages should browse the **116 CNBC numerator records**, not all 263 denominator records. The publication check covered the first two pages and return to the first page; it did not browse all 116 records in the hosted browser.

Edit the question without submitting it. The existing answer should retain its last submitted selection and request a new submission for the edit. Paging an existing statistics result should make no new model call. Confirm the tool/cost details where available; receipt costs are observations from earlier calls, not fixed prices for this session.

**Review:** Can the user compare exact counts, understand the denominator and reach the records behind the answer?

## Q2. Find an outlet's sponsors and a company's publishers

Client question: **Which companies are sponsoring native advertisements at each news outlet?**

Open **Data → Knowledge graph**. Use **ExxonMobil → publishers**, then inspect the named counterpart table and donut chart. Select The Washington Post through its row, slice or graph association. The selection heading, supporting articles and **Download selected counts** should describe that specific relationship.

The following distribution was observed in the **current 0.4.9 hosted UI** with the full native selection:

| ExxonMobil publisher | Supporting records | Share of ExxonMobil's 15 records |
| --- | ---: | ---: |
| Business Insider | 5 | 33.3% |
| The Washington Post | 5 | 33.3% |
| The New York Times | 3 | 20.0% |
| The Wall Street Journal | 2 | 13.3% |

Selecting **ExxonMobil → The Washington Post** showed five supporting article IDs. The panel reported **5 / 15 = 33.3%** of ExxonMobil's records and **5 / 18 = 27.8%** of The Washington Post's records. The parent distribution retained its denominator of 15. Repeat this selection and inspect the actual titles.

**Download selected counts** displayed **Prepared 1 categories for 5 records; parent denominator 15** in this round. The browser download event timed out after ten seconds and no file was obtained. Preparation is verified; successful download and CSV contents are not. In the session, obtain the file and confirm it contains the selected Washington Post category rather than the full parent list.

Next select **Washington Post → sponsors**. The current UI showed **18 records, including two with unknown dates**, with this complete distribution:

| Source-listed organization | Supporting records |
| --- | ---: |
| API | 6 |
| ExxonMobil | 5 |
| Shell | 2 |
| Southern Company | 2 |
| AFPM | 1 |
| Chevron | 1 |
| Eni | 1 |

These are current observations; do not substitute sponsor values from an earlier script or receipt. Select ExxonMobil to inspect the five supporting records.

The following article-mode sequence remains a **historical 0.4.2 procedure to recheck**. Open **Articles** and select **Capturing carbon around the world**. Inspect its named, directed relationships:

- **Article → Source lists sponsor → ExxonMobil**
- **Article → Published in → The Washington Post**

Switch to **Entities** and use **Fit**. Verify that the selected focus remains understandable. Choosing a different node, edge or category must update the actual relationships and supporting articles. Use **Previous records / Next records** if the graph selection contains more than ten articles.

Say: “These relationships describe names listed in the stored source records. They do not independently verify a payment or business contract. Source-listed organizations include associations and events; CERAWeek's treatment in a company-only denominator needs the client's decision.”

**Review:** Can the reviewer answer both directions, read the relationship names and inspect the specific articles? Record the observed current counts before marking this task passed.

## Q3. Compare counts across dates and filters

Client question: **How do advertisement counts change across selected dates, outlets, companies, and sponsors?**

1. Apply ExxonMobil and The Washington Post filters.
2. Select **1 January–31 December 2022** and exclude unknown dates.
3. Compare the Overview count, selected graph relationship, supporting record list and export. In this round the current UI showed **three eligible records, all three searchable, and zero unknown dates**. Selecting the ExxonMobil relationship showed **Washington Post: 3 / 3 = 100%** and three articles whose dates were all in 2022.
4. Inspect the annual counts and separately reported unknown dates. Explain that an unknown date is different from a known date outside the interval.
5. Restore the full native selection before the remaining examples. On Query, changed filters require resubmission; saved results should not silently adopt the new scope.

Switching to **Overview** in this round preserved the same combined filters and the same three record IDs. The matrix had one populated cell with **3**, and the annual chart showed **2022: 3**. This verifies the selected filtered views; it does not verify a matrix-cell click or downloaded export contents.

**Review:** Are the active filters visible, and do counts, relationships and records change together?

## Q4. Explore themes where the existing data supports analysis

Client question: **What themes or topics are represented in the native advertisements where the existing data supports this analysis?**

Open the historical label distribution in **Overview**, choose a label and inspect its supporting articles. Read an article's text rather than treating the category name alone as proof. Labels may overlap, and an absent annotation is not a verified negative.

Say: “Historical labels are earlier automatic annotations. They are useful for exploration, but they are not independently verified themes, factual verdicts or greenwashing findings.”

Show the **CLAIMS2 publication-pending** state. Reviewed import and read-only interfaces are implemented in Overview, article detail, Query/MCP and the article-provenance graph. They preserve record/text-version, taxonomy and source-evidence bindings. **All 37 real saved-result candidates remain held for source, taxonomy and semantic review; no new real assignments are published.** Do not demonstrate synthetic fixtures as published client results or report an empty claims view as zero greenwashing.

CLAIMS integration is scheduled current project work. Retraining a classifier has not been requested; animal agriculture remains future work. After reviewed publication, a separate demonstration must verify NC/SC category definitions, quoted passages and assignment-specific relationships.

**Review:** Are annotation meaning, overlap, review state and missing evidence understandable? Record the customer's preferred theme questions and taxonomy decisions as pending decisions.

## Q5. Explore fossil fuel social media advertising

Client question: **Which filters and visualizations are most useful for exploring the fossil fuel social media advertising dataset?**

Switch to the social collection and show its **not connected** explanation. The importer and separate collection interface exist, but the designated real fossil fuel social export is still a **TODO**. No real social or cross-collection demonstration or evaluation is accepted.

Explain what is needed next: the authoritative export/version, field meanings and mapping, source access, and representative social research questions. A disconnected collection must not be interpreted as a verified zero-ad count. Restore Native advertising afterward.

**Review:** Capture the requested filters and visualizations for later testing with real data. This question remains pending; the current native demonstration cannot answer it.

## Q6. Ask grounded questions and inspect original evidence

Client question: **How can RAG search help users explore both datasets while remaining grounded in the underlying advertising records?**

The current demonstration covers native records only. Explain the three paths:

| Task | Current path | What to check |
| --- | --- | --- |
| Exact counts, shares, sponsors and publishers | Model understands the submitted question; constrained read-only database tools calculate the result | Effective scope, counting unit, denominator and supporting records |
| Advertisement-content question | Paid retrieval and grounded generation over eligible text | Answer wording, quoted passages, source version and original record |
| Find passages without generation | **Tools → Search keywords**, with no model call | Search-term coverage and source passages; rank is retrieval order, not confidence |

Use one content question at a time, within the agreed session budget:

> How does the ExxonMobil Baytown hydrogen advertisement link natural gas to CCS, and is the new facility described as already completed?

Or:

> What feedstock supplies the biogas digesters in Total and GoodPlanet's employee air-travel offset project, and where is that project?

Both are existing development examples. Select **Generate answer**, inspect the evidence, and open the original record. Compare the subject, location, units and planned-versus-completed wording with the text. Record unsupported details, repeated citations, missing evidence or service errors. Exact citation location does not establish that the quotation supports every generated statement.

For a free search example, try `carbon capture and storage biogas`, inspect **Search term coverage**, then search `biogas` alone. Keyword search uses English word stems and OR matching; a returned passage may match only some terms. Do not describe five retrieved passages as five confirmed answers.

Finally open the [Using mollusks to monitor industrial sites record](https://ciss-advertising-observatory-production.up.railway.app/records/d340f887-efa7-5746-aaf8-14aabba6b63f). Inspect **Stored source text**, extraction notes, original URL, PDF snapshot and first-page preview. Try PDF access/download during the session. **0.4.9 HTTP checks** verified the selected PDF, download and preview bytes; earlier hosted browser inspection verified the rendered preview. A browser download action still needs session confirmation.

This is one reviewed local capture, not complete archive coverage. Its text is partial and its infographic is untranscribed. Original URLs, public archive URLs and local snapshots are separate sources; unavailable or unverified material must stay explicit. Neither a captured PDF nor a cited answer verifies the advertiser's environmental claims.

**Review:** Can the user follow an answer to its exact passage and record, and understand coverage and failure messages? Grounded social and cross-collection answers remain pending real data and independent review.

## Record the session and next decisions

For each of Q1–Q6 record: **version/scope, reviewer, task, observed result, supporting records, assistance, failure or limitation, and decision**. For generated questions retain the answer/run reference, selected tool, cost and latency where available. Mark unperformed steps **not run**, not passed.

The complete 0.4.9 sequence remains unfinished despite the selected current graph/date checks. Matrix-cell clicks, downloaded file contents, the article-mode sequence, complete live CCS/biogas semantic review and independent client usability tasks still need evidence. Keep the real social export, reviewed CLAIMS publication, counting/entity decisions, independent frozen questions, final two-dataset demonstration and another implementer's handoff reproduction as explicit TODOs. Use the [current handoff guide](../docs/current_handoff.md) for setup, data and attachment transfer, tests and reproduction; this script does not establish those tasks as completed.
