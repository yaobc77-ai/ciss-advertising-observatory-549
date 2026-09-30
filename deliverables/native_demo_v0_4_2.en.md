# Native advertising demonstration — 0.4.2

**Draft demonstration script. Customer acceptance and the final dual-collection demonstration are pending.**

Prepared: 30 September 2026. Use this with the [client review checklist](../docs/CLIENT_REVIEW_V0_4_2.md).

[Open Data](https://ciss-advertising-observatory-production.up.railway.app/data) · [Open Query](https://ciss-advertising-observatory-production.up.railway.app/query)

## Before the session

1. Read `/healthz` and record the application version/commit, data version, index version, and active profile. The recorded 0.4.2 corpus has 275 stored native records, 263 eligible for counts, 226 with searchable bodies, and 556 active sentence-based chunks. It has 22 unknown publication dates. These are different measures.
2. Select **Native advertising**, clear company/outlet/date/label filters, and include unknown dates. Verify the selection before using the expected numbers below. Corpus changes require renewed reconciliation.
3. Confirm sources and Query are available. A generated question can incur API costs; dashboard interaction and keyword search do not.
4. Ask the reviewer to perform each task first. Note where help is needed; do not present assistance as independent task completion.

## 1. Compare companies and news outlets

Open **Overview**. Read the company–outlet matrix, including the numbers and legend. Select a cell and examine its matching records. Export the counts.

Say: “This view compares the eligible records in this collection. Collection search terms are acquisition metadata, not sponsor identities. Associations and events remain visible under their source names while the research counting policy is reviewed.”

Review: Are values legible? Can the reviewer identify the selection and find the underlying records? Does the CSV match the displayed selection?

## 2. Answer the company-to-publisher question

Open **Knowledge graph** and select **ExxonMobil → publishers**.

For the recorded full selection, the side panel should show 15 supporting records and:

| Publisher | Records | Share of ExxonMobil records |
| --- | ---: | ---: |
| Business Insider | 5 | 33.3% |
| The Washington Post | 5 | 33.3% |
| The New York Times | 3 | 20.0% |
| The Wall Street Journal | 2 | 13.3% |

Select the Washington Post row, chart segment, or corresponding graph relationship. The heading should become **ExxonMobil → The Washington Post**, with five supporting records. The article list should contain those five records. The full ExxonMobil distribution can remain visible, with its parent denominator explicitly stated.

Download **selected counts**. For this selection it should contain the Washington Post category, count 5, and parent denominator 15, rather than the four-category parent list.

Say: “The relationship means these names are co-listed in the stored source records. It does not independently certify payment or a business contract.”

## 3. Answer the publisher-to-sponsor question

Select **Washington Post → sponsors**. The recorded full selection contains 18 records and seven source-listed categories:

| Source-listed organization | Records |
| --- | ---: |
| API | 6 |
| ExxonMobil | 5 |
| Shell | 2 |
| Southern Company | 2 |
| AFPM | 1 |
| Chevron | 1 |
| Eni | 1 |

Select ExxonMobil and inspect its five articles. Ask whether the association/company distinction is clear and whether the complete list answers Michelle's question.

## 4. Follow one article to its source

In **Articles** view, select **Capturing carbon around the world**. Inspect the two named relations: the article's **Source lists sponsor** link to ExxonMobil and its **Published in** link to The Washington Post.

Switch back to **Entities** and use **Fit**. The article selection and its supporting relations should remain identifiable. Open the title to inspect stored text, source status, and any available original material. Missing or unverified archives must remain explicit.

Review: Can the user distinguish a summary connection, an article, and its original-source fields? Can the user inspect the actual evidence instead of relying only on a node's total?

## 5. Change the dates

Apply ExxonMobil and The Washington Post filters, then select 1 January–31 December 2022 and exclude unknown dates. The five-record source list in the recorded release contains three dated 2022 articles; check that the filtered graph, record list, and export agree before presenting that expectation.

Return to Overview and inspect annual counts and the separately reported unknown dates. Restore the full native selection afterward.

Review: Is the active date scope obvious? Do counts and supporting articles change together? Is an unknown date visibly different from an article outside the chosen interval?

## 6. Try the client's natural-language questions

Open **Query** and use the example **Count ads at an outlet**:

> How many native ads are from the New York Times?

Select **Generate answer**. The recorded full selection has 19 eligible NYT records. Confirm that the tool result uses the complete filtered collection, including records without searchable text, and exposes the supporting records. Repeat the two company/publisher examples if the session budget allows.

Say: “The model interprets the question and chooses data tools. Those interpretation calls use the API budget. The database calculates the numbers. A failed model call is a service failure, not evidence that the collection contains no matching records.”

The known questions above are already development examples. Their success is useful for task review, but does not establish general question-answering accuracy.

## 7. Inspect a cited CCS or biogas answer

Use one content question at a time. For example:

> How does the ExxonMobil Baytown hydrogen advertisement link natural gas to CCS, and is the new facility described as already completed?

Or:

> What feedstock supplies the biogas digesters in Total and GoodPlanet's employee air-travel offset project, and where is that project?

Both are previously used development questions. Select **Generate answer**, then open the evidence and original record. The reviewer should check the claimed subject, units, location, and prospective language against the text. Record failures, limitations, and service errors as they occur; do not replace a failed live result with an undisclosed earlier answer.

Say: “We can examine what the advertisement says and how the answer cites it. This does not verify whether the advertised environmental benefit was achieved.”

## 8. Show the remaining scope

- Inspect a historical label and its articles. Explain that labels are automatic historical annotations, may overlap, and are not verified greenwashing findings.
- Switch to the social collection and show the **not connected** explanation. No real social or cross-collection result is accepted yet.
- Explain that the project still needs the designated fossil fuel social export, counting/organization decisions, reviewed independent questions, user acceptance, and final handoff checks. These social/review dependencies remain explicit TODOs.
- Explain that the customer has now scheduled CLAIMS integration. The provided GitHub package is being assessed for reusable code and output mappings. This changes the brief's earlier future-work schedule; it does not establish that a classifier is already connected or its results verified. Animal agriculture remains future work.

## Save the review

Retain the exact version/scope, task, observed result, supporting records, reviewer, assistance required, and decision. For generated answers, retain the answer/run reference and tool trace, plus measured cost and latency where available. A screenshot is useful context; it does not replace the task record or original evidence.
