# Find legacy CLAIMS article sources

Scope: recover candidate article URLs for legacy CLAIMS inputs whose source
URL is `unknown`. This does not repair sponsor names, dates or classify articles.

## Workflow

1. Preserve the legacy input, source row and file hash in the private audit.
2. Supply an unchanged, distinctive excerpt of 40–2,000 characters.
3. Find literal matches in current eligible native text and its accepted source
   intervals. Preserve ambiguous articles and repeated locations.
4. If no local candidate is found, optionally use one hosted web search.
5. Review candidate pages against the legacy input's wording, paragraph order,
   title, publisher and available captures. Similar subject matter is insufficient.
6. Record a confirmed association in the private source-review file, then use
   the reviewed importer. A source URL alone does not approve the category meaning.

## Maintenance MCP

`find_claims_source_candidates` is a maintenance-only tool. It is not offered
to the public dashboard's ordinary question agent. The process must explicitly
enable `OBS_CLAIMS_SOURCE_SEARCH_ENABLED=true` to expose this maintenance tool.
It is hidden by default. The database's local matching helper remains available
to the separate source-audit workflow without enabling paid web searches.

Inputs are an original excerpt and optional publisher/domain hints. Arbitrary
local paths, SQL and URL-fetch commands are not accepted. The output preserves
the excerpt hash, local matching limits, candidate URLs, lookup time and method.
Web candidates come from provider source/citation metadata, with review pending;
model-written links do not become authoritative source records. Up to five
candidates are returned. Local candidates follow the process's trusted native
collection filters. Domain hints constrain external search; they do not establish
that a web result belongs to the local collection.

## Search and cost

An exact local candidate returns without a model call. Otherwise, the enabled
maintenance tool can search externally and retains the local lookup status.
The implementation reuses the configured OpenAI client and shared budget ledger.
It uses `gpt-5.6-luna`, Responses `web_search`, at most one hosted tool call and
bounded output. Search context size is not an exact token cap.

The budget reserves the supported model's maximum input exposure, output and
search fee before dispatch. Known token and search usage is settled; timeouts or
unobserved fees keep their reservation until reconciliation. No recursive search,
automatic retries or bulk repair runs are part of this tool.

The tool changes only usage accounting. It is separate from the ordinary
read-only question tools and has no article, taxonomy or import write action.

Official references: [model capabilities](https://developers.openai.com/api/docs/models/gpt-5.6-luna),
[web-search interface and citations](https://developers.openai.com/api/docs/guides/tools-web-search),
[token and search pricing](https://developers.openai.com/api/docs/pricing).

## Review and remaining work

Candidates are not written into article URLs, graph identities or public labels.
Confirmed associations still need their original-source evidence and reviewer.
Unreachable pages, partial extracts, duplicated text and uncertain captures stay
unresolved. The existing importer independently checks current text versions
and exact evidence spans before publication.

Implementation is not source recovery acceptance. Real search receipts, reviewed
associations and publication evidence must be recorded separately.

The single recorded live sample returned search references but did not establish
the original article URL. Citation metadata proves the search returned a link;
it does not prove that the page contains the legacy excerpt. Model suggestions
are also unverified. See the [0.4.6 engineering receipt](../reports/claims_read_engineering_v0_4_6_20260930.json).
