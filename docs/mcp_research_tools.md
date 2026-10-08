# Research tools

The web application and MCP server share one typed catalog. Tools operate on the selected collection and filters; they do not accept arbitrary SQL.

## Main operations

| Tool | Result |
| --- | --- |
| `resolve_entity` | Matching sponsor, publisher, or social-account candidates |
| `record_statistics` | Counts, shares, groups, years, tied maxima, and period comparisons |
| `search_records` | Bounded matching source passages |
| `find_records` | Exact or literal-title record candidates |
| `get_record` | Versioned source text by record ID |
| `get_record_metadata` | Requested original metadata fields and missing-field states |
| `get_record_sources` | Source references, provenance, and annotation limits |
| `get_graph_schema` | Node and relationship definitions |
| `get_graph_neighborhood` | Bounded native-record relationships |
| `get_claims_matches` | Configured stored CLAIMS assignments and source quotations |
| `get_media_evidence` | Configured image/video-derived evidence |

Availability depends on mounted material and operator configuration. The authoritative schemas and catalog are in [research_tools.py](../src/observatory/research_tools.py).

## Scope and results

- Filters narrow the active collection; they do not silently broaden it.
- Native and social counts retain separate units.
- Retrieval results are bounded; passage count is not advertisement count.
- Unknown dates, inferred dates, and source dates remain distinguishable.
- A missing field does not invalidate other known metadata fields.
- Ambiguous titles or entity names return candidates for selection.
- Results identify source records and versions where applicable.

## Failure states

A zero count, no matching passage, unavailable material, invalid request, ambiguous name, and service failure have different meanings. Preserve the stated reason. A model or budget failure is not evidence that the database lacks information.

Optional external-source lookup is labeled separately and does not alter stored records or collection counts. Source-discovery proposals require review before any association is adopted.

## Run MCP

Install the optional dependency:

```sh
uv sync --frozen --extra mcp
uv run python -m observatory.mcp_server --dataset all
```

The default transport is stdio. Optional HTTP transport is restricted to loopback:

```sh
uv run python -m observatory.mcp_server --transport streamable-http --host 127.0.0.1 --port 8051 --dataset all
```

Database and material settings come from the server environment. The MCP endpoint is not mounted on the public Dash page. See [operations](operations.md) for configuration.
