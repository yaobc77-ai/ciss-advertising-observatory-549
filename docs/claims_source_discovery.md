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

### Create a private source review packet

The offline `claims-source-review-packet` command connects a saved lookup to
its unchanged paragraph in the original `id,article_id,text` CSV. It freezes the
selected text, CSV hash and lookup bytes in a new directory. It does not load
application settings, open a database, call a model or fetch pages.

```sh
uv run observatory claims-source-review-packet \
  --input-csv /private/CLAIMS_2.0_model/src/data/sampled_50_articles_paragraphs.csv \
  --input-id 2 \
  --lookup /private/source-lookup.json \
  --out outputs/claims-source-review-2
```

Supply the actual paragraph ID from the selected CSV. It never selects a current
Observatory record with that number. The lookup can be the tool's JSON result or
a sample wrapper containing `result` and its hash-bound `legacy_input`. The
searched excerpt must occur unchanged in that input paragraph. If it repeats,
provide `--excerpt-start` with its original Unicode character position.

Optionally supply `--captures /private/captures/manifest.json` after saving a
candidate page's extracted text as UTF-8. The capture manifest format is:

```json
{
  "schema_version": "claims-source-captures-v1",
  "captures": [
    {
      "candidate_url": "https://publisher.example/article",
      "text_file": "article.txt",
      "captured_at": "2026-09-30T18:00:00Z",
      "capture_method": "Saved page text, manually checked against the page",
      "completeness": "partial",
      "title": "Page title",
      "publisher": "Publisher"
    }
  ]
}
```

`candidate_url` must be one of the lookup's recorded public URLs. Keep text files
inside the capture manifest's directory; filenames are relative. Optional
`final_url` records a supplied redirect destination. `completeness` is
`unknown`, `partial` or `complete` as asserted by the capture provider; the
command cannot authenticate where those bytes came from or establish completeness.
At most five captures are accepted, each limited to 1,000,000 bytes. Raw UTF-8
bytes and CRLF line endings are retained. No network extraction is performed.

| Packet artifact | Use |
|---|---|
| `review.html` | Open locally to inspect candidate URLs and excerpt context. All source decisions are pending. |
| `source_input.json` | Selected original paragraph, legacy IDs, CSV row/hash and searched excerpt position. |
| `lookup_receipt.json` | Byte-for-byte saved lookup, including its earlier search usage if present. |
| `packet.json` | Candidate and capture provenance, match counts and original character locations. |
| `review.json` | Blank human decisions; separate from the result importer's review format. |
| `captured-text/` | Copies of supplied UTF-8 text, when captures are provided. |
| `packet_manifest.json` | Completion marker and hashes of every packet artifact. |

Comparisons distinguish `not_captured`, `no_match`, `unique_excerpt` and
`repeated_excerpt`. Only whitespace differences are permitted; case, punctuation
and other characters are preserved. Multiple occurrences remain visible, with a
total count and at most 100 displayed locations. Positions refer to the copied
capture's original Unicode characters, not bytes or HTML coordinates.

A partial capture with no match cannot rule out the page. An exact excerpt can
also appear in copied or syndicated pages; it does not identify the original
article or establish retained-paragraph order. The supplied title, publisher,
URL and capture date remain provenance assertions. Review these against the
original page and captures before recording a source decision.

This packet cannot publish a source association or be passed directly to
`claims-import`. Admit an externally confirmed article through the existing
corpus review/import process, then rerun `claims-audit` and the normal
[assignment review](claims_result_import.md). Keep all packet files private.

Implementation is not source recovery acceptance. Real search receipts, reviewed
associations and publication evidence must be recorded separately.

The single recorded live sample returned search references but did not establish
the original article URL. Citation metadata proves the search returned a link;
it does not prove that the page contains the legacy excerpt. Model suggestions
are also unverified. See the [0.4.6 engineering receipt](../reports/claims_read_engineering_v0_4_6_20260930.json).

## Inspect archived material when literal search misses

A failed literal match does not prove the article is absent. Inspect the preserved source files and article context before spending on another search. Do not rewrite an upstream paragraph or silently relax the evidence locator.

A subsequent [local archive comparison](../reports/claims_legacy_source_candidate_20260930.json) found a CNBC/SunPower candidate for legacy input 2 in the existing CSV and PDF-223. After whitespace compression for display, the CSV differs by a double hyphen versus an em dash; the PDF also differs by a straight versus curly apostrophe. Original text, half-open character positions, file hashes and the explicit differences are preserved in a separate private review packet. Pages 1–3 were visually inspected; full document completeness was not assessed. The live page fetch failed.

This is a manual source comparison, not a new MCP search result. The earlier paid lookup and its five URLs remain unchanged. Source identity remains pending, and no classification was approved or imported. Use the normal reviewed source/import workflow after identity and classification decisions are made.
