"""Filter choices must not materialize unused body-bound source projections."""

from types import SimpleNamespace

import pytest

from observatory.db import Database
from observatory.models import Filters
from observatory.social_annotations import social_state_options


class FacetDatabase(Database):
    def __init__(self, rows):
        super().__init__("")
        self.rows, self.calls = rows, []

    def connect(self):
        db = self

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                db.calls.append((sql, params))
                return SimpleNamespace(fetchall=lambda: db.rows)

        return Connection()


@pytest.mark.parametrize("dataset", ["native", "social", "all"])
def test_only_facet_fields_are_materialized_and_scope_remains_parameterized(dataset):
    db = FacetDatabase([])
    db.facets(dataset)
    sql, params = db.calls[0]
    assert sql.startswith("WITH filtered AS (SELECT dataset,publisher,sponsor,platform,account,keyword,labels FROM (")
    # The complete public source query is still the trusted filter authority;
    # the outer projection only prunes work, and does not invent new selection.
    public_sql, public_params = db._public_query(Filters(dataset=dataset))
    assert f"FROM ({public_sql}) AS facet_rows" in sql
    assert params == public_params == ([] if dataset == "all" else [dataset])
    assert "WHERE option.name<>'accounts' OR dataset='social'" in sql
    assert "ORDER BY name,value COLLATE \"C\"" in sql


def test_native_historical_labels_and_source_options_are_not_replaced():
    expected = [{"name": "labels", "value": "green.claim"},
                {"name": "labels", "value": "ff.claim"},
                {"name": "sponsors", "value": "(Unknown)"},
                {"name": "sponsors", "value": "Source-listed Company"}]
    db = FacetDatabase(expected)
    result = db.facets("native")
    assert result["labels"] == ["green.claim", "ff.claim"]
    assert result["sponsors"] == ["(Unknown)", "Source-listed Company"]
    assert "claims-calibrated" in db.calls[0][0]


def test_social_fixed_states_do_not_depend_on_present_annotation_candidates():
    db = FacetDatabase([{"name": "sponsors", "value": "AFPM"},
                        {"name": "accounts", "value": "(Unknown)"}])
    result = db.facets("social")
    assert result["labels"] == [item["value"] for item in social_state_options()]
    assert result["sponsors"] == ["AFPM"] and result["accounts"] == ["(Unknown)"]
