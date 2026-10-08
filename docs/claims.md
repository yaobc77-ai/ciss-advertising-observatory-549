# CLAIMS result integration

The application validates imported CLAIMS results and provides source-bound reads of configured assignments. These functions do not establish that a deployment contains complete or independently verified classifications.

## Source binding

Imported assignments identify their record, exact source version, body hash, and quoted excerpt. Legacy paragraph IDs remain distinct from Observatory record IDs.

Unknown article or URL associations remain unmatched. Changed text invalidates an earlier binding; candidate source associations are not adopted automatically.

## Taxonomy and status

CLAIMS2 uses subclaims (`NC_`) and superclaims (`SC_`), definitions, and their mapping. Stored results retain the taxonomy fingerprint.

Historical labels, automatic assignments, and human-supported assignments have distinct statuses. Unmatched or unpublished records are not negative classifications. A claim label does not establish factual truth or verify greenwashing.

## Available commands

| Command | Function |
| --- | --- |
| `claims-audit` | Check saved outputs against source text and versions |
| `claims-import` | Validate supplied assignment and source-binding files; write only with `--apply` |
| `claims-matches` | Read configured stored assignments with filters and pagination |
| `claims-retract` | Withdraw an assignment while retaining its history |

Use `observatory --help` and the command's `--help` for arguments. Import validation is the default; `--apply` explicitly writes assignments. Input and audit files are supplied separately.

## Reading results

The website and `get_claims_matches` expose configured assignments with definitions, review states, and exact source quotations. Filters narrow collection, record, taxonomy, and classification scope.

Query reads stored results. It does not run a new classification batch for each question.

## Data limits

- Missing source identity prevents reliable assignment binding.
- Source quotes must match the stored text version.
- Automatic labels retain their unverified state.
- Available positives do not establish coverage of every record.
- Retrieval evidence does not establish an exhaustive matching list.

See [the data dictionary](data_dictionary.md), [tool reference](mcp_research_tools.md), and [operations](operations.md).
