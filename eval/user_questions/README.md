# Realistic user questions

38 questions that a lawyer, journalist or researcher with no background in this project would type into the Query page. They follow the client's request (Michelle, 29 September) to test realistic questions such as "How many native ads are from the New York Times?" and "Which publishers is ExxonMobil working with?", and they cover the six research questions on p. 3 of the project description.

| Research question (PD p. 3) | Cases |
| --- | --- |
| Q1 Compare ad volume by company and outlet | U01–U07 |
| Q2 Which companies sponsor ads at each outlet | U08–U12 |
| Q3 Counts across dates, outlets, companies and sponsors | U13–U21 |
| Q4 Themes and topics the data supports | U22–U26, U36 |
| Q5 Social media filters and visualisations | U27–U28 |
| Q6 RAG grounded in the records | U29–U35, U37–U38 |

## How the questions were written

- Plain wording, short names (NYT, Exxon), a misspelling and a terse question, as real users type them. No field names or project terms.
- Each question was written from the client's examples and the source data, before any system answer to that wording was seen.
- Four cases (`source: "client_seen"`: U01, U08, U09, U36) repeat the client's own questions, which were already used during development. Report them separately from the 34 new cases.
- Some cases test traps a non-expert cannot see: CERAWeek appears as a sponsor but is a conference; API and AFPM are associations; Williams has three source spellings; 22 ads have no date; CNBC's top year is a tie and 19 CNBC ads are undated; social data is not loaded; verified greenwashing findings and disclosure wording are not available.

**Freeze rule:** after a system run, do not edit the questions or gold specifications, and do not tune prompts, aliases or routing against the new cases. Changes need a new version of the set.

## Correct answers

`scripts/build_user_question_gold.py` computes every correct answer with independent SQL over the current native records. It does not use the application's query code, calls no model and writes nothing to the database. It produces:

- `outputs/user-questions-gold-<time>.json`: counts, lists, shares and relevant articles for each case.
- `outputs/user-questions-review-<time>.csv`: a review sheet with the question, the correct answer, what the answer must say, and blank columns for the system's answer and the review.

For questions about content (`records`), the gold is the set of articles containing the relevant words. It shows whether cited sources are relevant; whether each statement is supported still needs a human reader.

## Review: would a non-expert get a usable, honest answer?

Score each column pass / fail, then give a verdict.

| Column | Question for the reviewer |
| --- | --- |
| `correct` | Does the number, list or cited article match the correct answer? |
| `scope_clear` | Does it say, in plain words, that this is about ads in this collection, not every ad that exists? |
| `gaps_disclosed` | Does it mention undated ads, name variants or other gaps that change the answer? |
| `plain_language` | Could someone without project background understand it? Internal words such as "eligible", "countable", "scope", "facet", "chunk" or record IDs count against it. |
| `no_overclaim` | Does it avoid treating a sponsor listing as proof of payment or partnership, an ad's claim as a fact, or an automated label as a finding? |
| `usable_next_step` | Can the user open the articles or list behind the answer? |

A case passes when `correct` and `no_overclaim` pass and nothing in `must_say` is missing. Report results per research question and keep client-seen cases separate. Mechanical checks and AI-assisted reading can prepare the sheet; the verdict needs a named human reviewer.
