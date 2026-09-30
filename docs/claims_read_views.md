# Read published CLAIMS2 results

The read-only interface is implemented for 0.4.6. All 37 saved-result candidates
remain on hold pending source, taxonomy and semantic review. No new real
classifications have been imported or published by this update.

## Website

- **Data → Overview** shows published matching articles, category definitions
  and original passages. Select an NC or SC ID and a review state to narrow the
  current collection filters. Empty results do not mean an article has no claims.
- **Article detail** displays assignments only when their version, body hash and
  exact quotation still match the text being shown.
- **Knowledge graph → Article provenance → CLAIMS2 evidence** follows an article
  to its assignment, NC definition, mapped SC and original text. Select one
  assignment to see its specific relationship provenance. Unmapped NCs have no
  invented parent; NC/SC IDs from different taxonomy versions remain distinct.
- **Query** can use the read-only claims tool. The database returns exact counts
  and source evidence; the tool does not run classification or edit a taxonomy.
- **Download matching page** exports up to ten matching records and their evidence
  as JSON, with whole filtered-set totals, category counts, filters and publication version labelled
  separately. The page is not presented as the complete matching collection.

The initial empty state keeps the source records available and explains that
CLAIMS2 publication is pending. A filtered empty result remains clearable. No
fabricated examples are shown as results.

## Counts and interpretation

`total_records` counts distinct current eligible records with published matches.
`total_matches` counts assignments, so multiple categories or source passages
can belong to one record. Category counts may overlap. Scope totals describe
eligible records under the collection filters, not a completed classification
denominator. Classification completion remains unknown.

`automatic_unverified` means a published automatic assignment whose semantic
support has not been approved by a human. `human_supported` means the quoted
passage was reviewed against the specified taxonomy definition. Neither state
establishes the statement's external truth, legal status or verified greenwashing.

Historical twelve-label annotations remain separate from CLAIMS2 NC/SC results.
An absent assignment is not a classified negative.

## Tools and source changes

The optional MCP server exposes `get_claims_matches` with typed NC/SC IDs,
taxonomy fingerprint, review state and bounded pagination. The ordinary model
interface uses the same executor and trusted collection filters. The default
tool page contains five records; an explicit page is limited to twenty.

Whole-selection category counts are available in the website and page export;
they are omitted from tool responses to keep the model input bounded.

Each quotation includes record, text version, body hash and original Unicode
character positions. Run, taxonomy and review versions are retained separately.
The graph reads article versions and assignments in the same database snapshot.
Retractions and source updates remove affected assignments from current reads;
they do not erase their maintenance history or overwrite source text.

## Legacy source URLs

The supplied legacy results contain unknown article URLs. A source-discovery
workflow first looks for the unchanged excerpt in eligible current native text.
Literal matches are candidates for review, including repeated locations and
ambiguous articles. They do not automatically prove that the legacy article
and current record are the same document.

Unlocated excerpts can use the optional maintenance MCP source-search tool,
which is hidden unless explicitly enabled for a maintenance process.
Its candidates remain separate from the article database and reviewed importer.
See [legacy source discovery](claims_source_discovery.md) for its settings,
cost accounting and review boundary.

## Related instructions

[Reviewed importer](claims_result_import.md) ·
[Source audit](claims_source_audit.md) ·
[Integration plan](claims_integration_plan.md)
