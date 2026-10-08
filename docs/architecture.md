# Architecture

## Components

| Component | Role |
| --- | --- |
| Dash application | Query, Data, and record-detail pages |
| Plotly, Cytoscape, AG Grid | Charts, relationships, and record tables |
| Service layer | Coordinates validated reads and answer generation |
| PostgreSQL | Versioned records, observations, metadata, and annotations |
| pgvector and full-text search | Vector and keyword retrieval |
| Shared tool catalog | Typed research operations for the web application and MCP |
| Attachment bundle | Separately supplied PDFs and previews |

The application runs as one Python service. PostgreSQL stores advertising records; attachments are separate files. Deployment does not automatically import a dataset or attachment bundle.

## Data and retrieval

Imports retain source hashes, issues, metadata, and distinct record versions. Counting permission and retrieval permission are separate: a record can contribute to a count while its body is unavailable for content search.

Native advertisements and social posts share filters and record access, while preserving collection identity and counting units. Multiple saved observations can support a social post without increasing its unique-post count.

Content retrieval combines keyword and vector results. Passages retain exact source positions. Citation validation checks the record version, source hash, and quoted location; it does not by itself prove that a statement correctly interprets the source.

## Relationships and classifications

Relationships connect stored companies, publishers, articles, and available classifications. Source fields determine the connection. The graph is a record-exploration view, not independent evidence of contracts or greenwashing.

CLAIMS import and read interfaces are implemented. When results are configured, the website reads their taxonomy, source binding, and review state; it does not run classification for every question. Classification coverage depends on the supplied result data.

## Configuration

Database, model, budget, and attachment settings are server-side. Optional tools and features require their configured material. Missing configuration does not imply that no relevant advertisement exists.

The structured-question interpreter is optional and defaults to disabled. Its implementation is distinct from real-model performance validation.

## Main source files

| File | Role |
| --- | --- |
| [app.py](../src/observatory/app.py) | Pages and callbacks |
| [service.py](../src/observatory/service.py) | Application operations |
| [db.py](../src/observatory/db.py) | Database reads and writes |
| [research_tools.py](../src/observatory/research_tools.py) | Tool contracts and execution |
| [mcp_server.py](../src/observatory/mcp_server.py) | MCP transport |
| [rag.py](../src/observatory/rag.py) | Evidence-bound answer generation |
| [knowledge_graph.py](../src/observatory/knowledge_graph.py) | Relationship representation |

See [operations](operations.md) and [the tool reference](mcp_research_tools.md).
