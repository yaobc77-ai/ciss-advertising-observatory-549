# Client materials and review request

**Draft — not sent.** Prepared for the 0.4.2 review on 30 September 2026.

**Subject:** Advertising Observatory: review of native workflows and materials for the social collection

Hi Michelle and Ned,

The current prototype supports the three examples in Michelle's feedback: counting New York Times records, listing ExxonMobil's publishers, and listing the Washington Post's source-listed sponsors. The dashboard includes a company–outlet matrix and an interactive graph. Selecting a connection opens its count, shares, and supporting articles.

- [Data dashboard](https://ciss-advertising-observatory-production.up.railway.app/data)
- [Question interface](https://ciss-advertising-observatory-production.up.railway.app/query)
- [Project page](https://yaobc77-ai.github.io/ciss-advertising-observatory-549/)

We would like to review whether these workflows answer the project's six research questions and are usable by journalists, lawyers, and researchers. Software and deployment checks have been performed; domain, answer-quality, and user acceptance are still pending.

Could you help with these materials and decisions? Existing files or shared-folder access are sufficient. The team will handle conversion, field mapping, reconciliation, and preparation of review sheets.

1. **Designated fossil fuel social media dataset.** Please share the current export or a representative original-field sample, its collection/export dates, inclusion criteria for advertising, and any existing field documentation. Please identify what is available for Twitter and for Facebook/Instagram, the owner of each dataset, and how we can access the University of Miami prototype. We will preserve platform-specific fields and distinguish post counts from native-article counts. There is no need to resend the native-data archives we already have.

2. **Counting and organization policy.** Please confirm whether companies and sponsors mean the same thing for these questions, how associations and events such as API, AFPM, and CERAWeek should appear, and whether name variants or parent/subsidiary names should be grouped. Please also confirm the unit for duplicate URLs/reposts, multiple sponsors, and unknown dates. Until reviewed, the application retains source categories and describes links as record-supported associations rather than verified payments.

3. **Sources and public use.** Please share any existing mapping from advertisement URLs/IDs to PDFs, screenshots, extracted text, or archive URLs, and identify the authoritative body version and materials that may be shown publicly. We have many candidate files, but most have not been verified against individual records. The team will perform the mapping checks; an existing index is preferable to duplicate file packages.

4. **Reviewers, tasks, and acceptance.** Please nominate a domain reviewer and, if possible, one or two representative users. Michelle's three examples can be reviewed immediately. Additional realistic tasks and known source links will help us prepare an independent 20-question acceptance packet, separate from examples already used in development. We will assemble candidate evidence and complete record lists for review before freezing the packet. Please identify who can approve counting decisions, answer support, and final acceptance.

5. **Expected use and handoff.** Please confirm expected collection size, update frequency, typical simultaneous users, and an acceptable waiting time. Please also confirm the final shared repository and its maintainers, and who should receive the data/update/deployment handoff. These decisions determine which performance checks and maintenance instructions are appropriate.

Dashboard interaction and keyword search do not call a language model. With the current research mode enabled, generated questions use the API to interpret the request, including counting questions; totals are then computed by the database. Content answers retrieve article evidence, but a correctly located quotation still needs domain review for support, attribution, and completeness.

The current scope is fossil fuel native and social advertising, with CLAIMS integration now scheduled following the updated customer direction. We already have the supplied GitHub package and will inspect it for reusable code before asking for duplicate material. To accept the integration, we need to identify the approved taxonomy/codebook, expected versioned outputs, article/text mappings, and the responsible reviewer. We will prepare those contracts and flag specific missing artifacts after inspection. Historical labels should not be treated as verified greenwashing findings. Animal agriculture remains future work under the project description.

If a material or decision is unavailable, its owner and expected availability will help us continue the other work. We can conduct the native workflow review while the social export is arranged.

Best,

[Team / sender]
