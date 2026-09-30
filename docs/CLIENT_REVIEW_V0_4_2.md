# Client review of the current release

**Draft for review — no customer acceptance has been recorded.**

Prepared: 30 September 2026. Guided interaction checks: **0.4.2**. The maintenance release is now **0.4.3**; its health and CI checks have a separate [receipt](../reports/release_v0_4_3_publication_20260930.json).

[Dashboard](https://ciss-advertising-observatory-production.up.railway.app/data) · [Questions](https://ciss-advertising-observatory-production.up.railway.app/query) · [Demonstration](../deliverables/native_demo_v0_4_2.en.md) · [Materials request](../deliverables/client_materials_request_v0_4_2.en.md)

## Scope

The current collection contains fossil fuel native advertising. The second required collection is **fossil fuel social media advertising**; its designated client export has not been imported. That import and independent customer review are explicit TODOs. Following the customer's updated direction, **CLAIMS integration is now scheduled work**: the supplied GitHub package has been inspected; its outputs still require article linkage, adaptation and import. See the [integration plan](claims_integration_plan.md). The original brief placed this backend in future work; that historical scope is not the current integration decision. There are no accepted live CLAIMS results yet. Animal agriculture remains future work. Historical labels are available for exploration, with their limitations displayed.

This review follows the six research questions in the project description and Michelle's feedback. It separates software verification, domain decisions, and user acceptance. An automated test or successful deployment does not approve an answer's meaning or usability.

## Research questions and review tasks

| Project question | Task in this release | What the reviewer should check | Current boundary |
| --- | --- | --- | --- |
| 1. How can users compare the volume of fossil fuel native advertisements by company and news outlet? | In Data → Overview, compare the company–outlet matrix and export its counts. | Can a user read exact values, identify the current filters, and open the corresponding records? Do the categories and counting unit fit the research question? | Counts describe eligible stored records. Source-listed organizations include companies, associations, and events. |
| 2. Which companies are sponsoring native advertisements at each news outlet? | Select **Washington Post → sponsors** in Knowledge graph; select an organization or chart segment to inspect its articles. | Does the complete list answer the question? Is the difference between a source-listed sponsor and verified payment understandable? | Organization identity and treatment of associations/events need a domain decision. |
| 3. How do advertisement counts change across selected dates, outlets, companies, and sponsors? | Apply sponsor, outlet, and date filters; compare annual counts, selected relationships, records, and CSV. | Do all views describe the same selection? Are missing dates visible? Does changing the date range recalculate the result? | Unknown dates require an explicit inclusion choice; absent records do not establish zero advertising outside the collection. |
| 4. What themes or topics are represented in the native advertisements where the existing data supports this analysis? | Select a historical label in Overview and inspect its records. Review the CLAIMS integration plan and proposed source-linkage contract. | Is the label useful for exploration? Is its historical, automatic status clear? Can future CLAIMS labels be tied to a taxonomy version and the correct original text? | Labels may overlap and are unverified. Missing labels are not negative findings. CLAIMS integration is scheduled; a new run and validated greenwashing interpretation are not yet available. |
| 5. Which filters and visualizations are most useful for exploring the fossil fuel social media advertising dataset? | Review the proposed mapping after receiving the designated export, then test actual platform/account/date fields. | Do post counts reconcile with that export? Are the available filters meaningful? Are missing values and collection coverage clear? | **Pending real data.** The not-connected screen and synthetic importer tests are not acceptance. |
| 6. How can RAG search help users explore both datasets while remaining grounded in the underlying advertising records? | Try the client's count/list questions in Query, then inspect a cited CCS or biogas answer and its original record. | Are filters respected, counts complete, attribution correct, and claims supported by the cited text? After social import, can a comparison cite both collections and retain separate units? | Native tools and evidence retrieval can be reviewed now. Independent answer review and social/cross-collection evaluation remain pending. |

## Known examples for a guided review

These examples have already informed development. They are demonstrations and regression tasks, **not held-out evaluation questions**.

For the recorded full native selection, with unknown dates included:

| Client question | Recorded collection result |
| --- | --- |
| How many native ads are from the New York Times? | 19 eligible records; searchable-body counts must not replace this total. |
| Which publishers is ExxonMobil working with? | 15 records across Business Insider 5, The Washington Post 5, The New York Times 3, and The Wall Street Journal 2. |
| Which companies has the Washington Post worked with? | 18 records across API 6, ExxonMobil 5, Shell 2, Southern Company 2, AFPM 1, Chevron 1, and Eni 1. These are seven source-listed organization categories. |

The specific ExxonMobil → Washington Post selection contains five records. Its shares are 5/15 of ExxonMobil records and 5/18 of Washington Post records in that full scope. The side panel must show that relationship and those five articles, even while retaining the parent distribution chart.

The results above are bound to the recorded corpus, not a permanent claim about future imports or all advertising. The [release receipt](https://github.com/yaobc77-ai/ciss-advertising-observatory/blob/271c02d8b2f82bf8cc10c8b1278fae5c8ea1d6e2/reports/graph_selection_publication_v0_4_2_20260930.json) records the production interaction checks. Before review, record the actual application commit and data/index versions from `/healthz`; if the corpus has changed, reconcile expected values again.

## Costs and answer interpretation

- Data clicks, database calculations, source reads, and **Search keywords** do not call a language model.
- With the research agent enabled, **Generate answer** uses the API to interpret the question, including count/list questions. The database computes totals; the model does not supply authoritative counts.
- A content answer uses retrieved passages with checked references to their original text versions. Quote location is a mechanical check. A domain reviewer must still judge whether the quotation supports the answer.
- An advertisement can describe a proposal, forecast, or claimed benefit. Retrieving that statement does not independently verify it.
- Each question currently uses its explicit selection. Multi-turn references such as “those ads” and requests combining several final research tasks need separate review; they are not accepted general capabilities.

## Review record

For each task, retain:

| Field | Record |
| --- | --- |
| Reviewer / role / date | To be supplied |
| Application commit / data version / index version | To be captured before the session |
| Question and active collection/filters | Exact submitted text and scope |
| Result and supporting records | Counts/list or generated answer; record IDs and available sources |
| Completion without help | Yes / no; assistance and point of confusion |
| Source support / attribution / qualifications | Supported / unsupported / uncertain, with explanation |
| Completeness and usefulness | Complete / partial / failed, with explanation |
| Decision and follow-up | Accepted for this task / revision needed / awaiting material |

For API answers, also retain the answer/run reference, tool trace, latency, and settled cost where available. A review belongs to the exact answer and version; it does not approve later generated answers automatically.

## Independent evaluation and remaining acceptance

The delivery plan calls for 20 development questions and 20 frozen acceptance questions covering native, social, cross-collection, CCS/biogas, filters, and missing evidence. Existing development and acceptance-draft files have been seen during development. The independent client packet remains empty and unapproved.

The team should prepare candidate record lists and original support text, obtain domain review, and freeze new questions and grouping decisions before running them. Customers can provide tasks and identify reviewers; they need not supply machine-readable annotations. Do not fabricate reviewer approval or fill social gold with zero counts or native substitutes.

The plan's proposed quality targets require agreement before formal review: Hit@5 at least 90% on answerable questions, 100% valid quote locations, at least 95% **human-reviewed** quote support, and explicit insufficient-evidence responses for all designated unsupported questions. These are proposed engineering targets, not achieved results or thresholds specified by the original brief. Report native/social/cross-collection results and denominators separately.

Count/list acceptance must include the actual Query model/tool path as well as a database reconciliation. The evaluator's default count cases directly call database statistics; those cases alone do not evaluate language understanding or tool selection. The explicit `--paid --answer-counts` mode additionally tests `Service.answer` and records its structured counts, filters, trace, and costs. No paid evaluation has been run for this document.

Final acceptance still requires real social import and reconciliation, independent semantic and user review, agreed organization/counting policies, the scheduled CLAIMS integration with its own validation, server/other-machine database restoration, and a complete handoff and final demonstration. A [complete local database restore](../reports/DATABASE_RESTORE_20260930.en.md) has passed against a fixed snapshot; it does not verify production or another machine. This document and the guided demonstration are review preparation, not a completed final presentation.
