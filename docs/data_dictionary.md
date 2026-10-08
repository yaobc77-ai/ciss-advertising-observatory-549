# Data dictionary

## Record fields

| Field | Meaning |
| --- | --- |
| `record_id` | Stable record identifier |
| `dataset` | `native` or `social` |
| `title`, `body` | Stored title and source text |
| `url`, `archive_url` | Original and available archived references |
| `publisher` | Recorded news outlet |
| `sponsor` | Recorded sponsor or company-affiliation value; interpretation depends on collection |
| `keyword` | Collection search term; it is not the sponsor field |
| `platform`, `account` | Social platform and recorded account name |
| `published_at` | Source publication date, when recorded |
| `disclosure` | Recorded advertising-disclosure text |
| `countable` | Whether the record can enter collection statistics |
| `retrievable` | Whether approved text can enter content retrieval |
| `provenance`, `issues` | Source origin and recorded quality limitations |
| `annotations` | Historical or imported annotations with their status |

## Versions and evidence

A version identifies the stored record state. A body hash identifies its exact text. Evidence includes the record/version, source text, and character start/end positions. Changes to the body require a new matching evidence binding.

Social evidence can identify a particular saved observation. Several observations can refer to one post; conflicting text, dates, account names, or historical labels are retained as source limitations.

## Counting units

- **Native:** count admitted advertisement records in the selected scope.
- **Social:** count unique admitted company posts; distinguish saved observations from unique posts.
- **Both:** return separate collection counts. Adding them does not create one common advertising unit.
- **Shares:** state the numerator and denominator within the selected collection and filters.
- **Date comparisons:** retain source dates, missing dates, and optional inferred dates as separate states.

## Names and labels

Display names can differ from normalized filtering values. Entity aliases do not establish a legal identity or corporate relationship. Ambiguous names require clarification.

Historical labels, published automatic classifications, and human-supported classifications are separate. Unknown or unmatched classifications are not negative findings. A classification does not establish whether an advertising claim is factually true.

## Missing information

A blank date, missing archive link, absent disclosure position, and unavailable body are different gaps. Preserve the available fields and state the missing field; do not infer a value from an unrelated source.

See [the input contract](PROTOTYPE_DATA_PIPELINE.md), [tool reference](mcp_research_tools.md), and [record assets](record_assets.md).
