# MCP and research tools

The Advertising Observatory uses a shared set of research tools to answer questions about stored advertising records. The model selects a tool; the application calculates counts, reads sources, and checks evidence.

The web application uses **Responses API function calling**. A separate **Model Context Protocol (MCP) server** exposes the same `ToolCatalog` to MCP clients. These are two interfaces to the same tool implementation. The public dashboard does not host a remote MCP endpoint.

## How a question is answered

```text
Question + current collection filters
  → model selects a supported tool and its arguments
  → database statistics, article retrieval, graph relationships, or CLAIMS reads
  → scope, source-version, and citation checks
  → one labeled external lookup if the web answer remains unresolved
  → summary, evidence, and limitations
```

An MCP client calls the tools directly through the separate MCP server. It supplies its own model or workflow; starting the server does not start the web application's model-selection process.

The tools operate within a trusted collection selection. A request may narrow that selection, but it cannot silently clear filters or switch to another dataset. Results describe the stored collection, not every advertisement in the real world.

## Eight core tools

| Tool | Purpose | Main output |
| --- | --- | --- |
| `resolve_entity` | Match a company or outlet name to actual source-field values. | Candidate names and IDs, with ambiguity information. |
| `record_statistics` | Calculate exact counts, distributions, and shares. | Counts by outlet, sponsor, platform, or year; highest years; period comparisons; denominator and date-basis information. |
| `search_records` | Find relevant article passages with keyword retrieval. | Bounded passages, record and version IDs, character positions, source references, and search diagnostics. |
| `get_record` | Read part of an identified article. | An unchanged text interval with its version, body hash, and character positions. The current detail adapter supports native records. |
| `get_record_sources` | Read the materials behind an identified record. | Public original and archive references, with annotation and source limitations. The current detail adapter supports native records. |
| `get_graph_schema` | Explain the graph's node and relationship types. | Definitions, source-identity rules, and adapter limitations. |
| `get_graph_neighborhood` | Explore the evidence relationships around selected articles. | A paged graph for up to five native articles, with typed relationships and provenance. |
| `get_claims_matches` | Read published CLAIMS2 classifications. | NC/SC definitions, review states, taxonomy versions, and exact source evidence. |

The core tools do not call a language model. Model interpretation and content generation in the web application use the project's API budget, including interpretation of count questions.

### Examples

| Question | Tool and operation |
| --- | --- |
| How many native ads are from the New York Times? | `record_statistics`: select the outlet and return the total. |
| Which publishers is ExxonMobil working with? | `record_statistics`: select the source-listed sponsor and group by publishers. The answer explains that the source fields do not independently verify contracts. |
| Which sponsors appear in Washington Post records? | `record_statistics`: select the outlet and group by sponsors. |
| Which year has the most CNBC ads? | `record_statistics`: group by years and return every highest-year tie. |
| How do counts before and after 2020 compare? | `record_statistics`: compare two explicitly bounded periods. |
| What do ExxonMobil and Shell emphasize about reducing emissions? | `search_records`: retrieve separately for each company, then use the web RAG path for a cited comparison. |
| Which published CLAIMS categories match this article? | `get_claims_matches`: filter by the record and read published assignments. |

## Exact statistics

Counts come from the database, including matching records without searchable article text. Retrieved passages and graph pages cannot establish collection totals.

### Years and ties

For a year distribution, use `group_by: "years"`. Add `ranking: "highest"` to return all years tied for the highest dated-record count. Unknown dates are reported separately.

```json
{
  "filters": {"publishers": ["CNBC"]},
  "group_by": "years",
  "ranking": "highest"
}
```

### Period comparisons

The tool supports two or three named periods in one database snapshot. Endpoints are inclusive; `null` leaves an endpoint open. Every period retains the shared collection filters. Missing dates are never assigned to a period, and records outside the specified periods remain outside their counts.

```json
{
  "filters": {"sponsors": ["totalenergies"]},
  "periods": [
    {"label": "Before 2020", "date_from": null, "date_to": "2019-12-31"},
    {"label": "2020 onward", "date_from": "2020-01-01", "date_to": null}
  ]
}
```

Source-only dates and supplemented dates are distinct modes. The returned scope states which mode was used and reports missing dates. A supplemented date does not replace the stored original date.

### Percentages

Use `measure: "share"` with `group_by: "none"`. By default, the denominator is the trusted current selection, and the requested filters narrow the numerator. A question may define a narrower comparison group through `denominator_filters`; the target is then counted inside that group.

The database calculates the numerator, denominator, and percentage. A zero denominator is undefined. Native and social percentages remain separate. Year distributions and period comparisons currently support counts, not shares.

### Ambiguous names

Source names are candidates, not approved corporate identities. Display aliases improve presentation but do not merge companies.

For example, `williams`, `the williams companies, inc.`, and `williams companies` remain separate source values. An unresolved short name must be clarified, or the user must explicitly select the desired source values. The system cannot silently count one spelling or combine all related names.

## Article retrieval and citations

`search_records` supplies keyword passages to the shared tool interface. For web content questions, the application then uses its keyword-plus-vector RAG path and citation-constrained generation.

Comparisons use `comparison_scopes` to retrieve separately for two or three targets. One company's passages cannot stand in for another company's evidence. Each target needs an explicit source-listed sponsor or outlet.

Native passages are checked against the unchanged article text, version, body hash, and character positions. A valid quotation proves that the text exists at that location; it does not prove that the generated conclusion is correct or that an advertising claim is true.

## What the knowledge graph represents

The graph connects records, text versions, source materials, and classifications through named relationships. A simplified view is:

```text
Article
 ├─ source_lists_sponsor → SponsorCandidate
 ├─ published_in → Outlet
 ├─ has_text_version → TextVersion
 ├─ has_source_reference / has_archive_reference → SourceArtifact
 ├─ has_annotation_record → Annotation → Label / EvidenceSpan
 └─ has_claim_assignment → ClaimAssignment
                           ├─ assigns_subclaim → Subclaim (NC)
                           │                     └─ subclaim_of → Superclaim (SC)
                           └─ cites_claim_evidence → EvidenceSpan → TextVersion
```

Relationships retain their source and version limits. A source-listed sponsor relationship does not independently establish payment, ownership, a contract, or endorsement. Missing relationships do not establish factual absence.

`get_claims_matches` reads published results; it does not rerun classification. A record without a published match is not a classified negative. A human-supported assignment means it was reviewed under its taxonomy, not that greenwashing or factual truth was independently verified.

## External search: two different entry points

### Web application fallback

With `OBS_WEB_SEARCH_ENABLED=true`, the web service first attempts a collection-based answer. If it remains unresolved, it may make one external lookup, including for insufficient evidence, unresolved clarification, or unavailable collection services.

This is a broader fallback than the standard MCP ticket workflow below. A successful database answer, including an exact zero count, does not trigger it. Invalid questions, budget limits, disabled source links, source-version changes, and citation-integrity failures do not trigger it either. An earlier external attempt is not repeated.

External answers show **Outside the advertising collection** and separate `[W1]`-style references. They cannot supply missing collection counts or resolve a source identity by silently changing stored data.

### Standard MCP ticket workflow

When enabled, `search_external_sources` is an additional MCP tool:

1. Call `search_records` in the same server instance.
2. A healthy empty keyword search with no rejected evidence may return `web_fallback_ticket` for a supported scope.
3. Call `search_external_sources` with that ticket.

The ticket expires after five minutes and is consumed before dispatch. It can be used once, even if the lookup fails. The tool checks the collection version again before searching. It does not accept a new question, arbitrary URL, or SQL through the ticket request.

An empty keyword search is not proof that no semantically relevant material exists in the database. Unlike the webpage, this tool does not first run the complete hybrid-retrieval and generation path.

External lookups have an API cost and update usage accounting. Returned text is a model paraphrase with provider citations; it does not have the local article's character-position guarantee. External pages do not enter the article tables, vector index, graph, or CLAIMS classifications automatically.

### Optional CLAIMS source maintenance

`find_claims_source_candidates` is a separate maintenance tool for legacy CLAIMS excerpts whose original article or URL is unresolved. It searches locally first, may perform one paid external lookup, and returns pending source candidates for review.

It is hidden by default (`OBS_CLAIMS_SOURCE_SEARCH_ENABLED=false`). It is not a public-question fallback and does not publish or update article records.

## The web answer layout

Every web answer uses the same reading order:

| Section | Content |
| --- | --- |
| **Summary** | A direct answer or the main comparison. Statistics summaries are built from returned numbers; content summaries cite their evidence. |
| **Evidence** | Counts and matching records, located article quotations, graph relationships, or published classification evidence. |
| **Scope and limitations** | Submitted filters, date basis, missing data, retrieval coverage, pagination, and annotation review status. |
| **How this question was answered** | Tool execution, model calls, external-lookup status, and API cost. |

Unresolved or failed requests show status and a next step instead of unsupported findings. Loading indicators report actual stages, such as question interpretation, database search, external search, answer preparation, and citation checks. They do not expose private model reasoning or simulate progress with a timer.

This layout belongs to the web application. A standard MCP client receives structured tool results and chooses its own presentation.

## Run the MCP server

From the repository root, install the locked environment with the optional MCP dependency:

```sh
uv sync --frozen --extra test --extra mcp
```

Copy [`.env.example`](../.env.example) to `.env` and configure the database and application secrets as described in the [README](../README.md#run-locally). Keep credentials private. Set `OPENAI_API_KEY` only when paid model features are needed. Starting the MCP server does not migrate the database or import a dataset.

Start a stdio server:

```sh
uv run python -m observatory.mcp_server --dataset native
```

An MCP client normally launches this process and communicates over its standard input and output. To restrict its trusted selection at startup:

```sh
uv run python -m observatory.mcp_server --dataset native --publisher "The Washington Post"
```

Alternatively, run a separate local HTTP server:

```sh
uv run python -m observatory.mcp_server --transport streamable-http --host 127.0.0.1 --port 8051
```

HTTP is restricted to loopback addresses. Port 8051 is separate from the dashboard's port 8050. Public remote MCP access would need its own deployment and access-control design.

| Setting | Purpose |
| --- | --- |
| `OBS_RESEARCH_AGENT_ENABLED=true` | Enable model selection of the shared tools in the webpage. |
| `OBS_WEB_SEARCH_ENABLED=true` | Enable bounded paid external lookup in the webpage and optional MCP tool. |
| `OBS_CLAIMS_SOURCE_SEARCH_ENABLED=false` | Keep the separate CLAIMS maintenance tool hidden. |

These are configuration examples, not confirmation that a particular deployed process has loaded them. The standalone MCP server is not assumed to be running.

## Limits and verification

- Model selection is bounded to four steps and four tool calls. Content generation and optional external lookup are separate operations with their own budget accounting.
- The shared catalog exposes no arbitrary SQL, local file paths, URL fetching, classification jobs, or collection writes.
- Graph neighborhoods currently support native records and return at most five articles per page.
- Record text reads return at most 12,000 characters. Search passages and result pages are bounded.
- A complex compound question may require clarification because the model-selection workflow ends at one final tool result.
- The social-data importer and views exist, but real social-data analysis awaits the client export.
- The recent statistics and fallback change passed 2,205 offline tests and five isolated database tests. These do not establish real-model tool-selection accuracy, external-source relevance, or customer acceptance. No new paid model or web call was made for those checks.

## Source code

| Responsibility | File |
| --- | --- |
| Shared schemas, scope checks, and tool dispatch | [`research_tools.py`](../src/observatory/research_tools.py) |
| Web model-selection policy and bounded execution | [`research_agent.py`](../src/observatory/research_agent.py) |
| Standard MCP transport | [`mcp_server.py`](../src/observatory/mcp_server.py) |
| Answer orchestration, validation, and global fallback | [`service.py`](../src/observatory/service.py) |
| Database statistics and retrieval | [`db.py`](../src/observatory/db.py) |
| Graph types, predicates, and provenance | [`knowledge_graph.py`](../src/observatory/knowledge_graph.py) |
| Web answer presentation | [`app.py`](../src/observatory/app.py) |

Related documentation: [CLAIMS read-only views](claims_read_views.md) · [Legacy source discovery](claims_source_discovery.md) · [Current handoff](current_handoff.md).
