"""Independent source-controlled aliases, not question-specific query rules."""

import pytest

from observatory import structured_queries as queries


@pytest.fixture(autouse=True)
def fictional_aliases(monkeypatch):
    monkeypatch.setattr(queries, "_SPONSOR_SOURCE_ALIASES", (
        ("Harbor Industry Guild (HIG)", ("HIG",)),
        ("Highland Innovation Group", ("HIG",)),
    ))


def test_alias_selects_an_existing_exact_source_value():
    source = "Harbor Industry Guild (HIG)"
    assert queries.canonical_source_values("hig", "sponsors", [source]) == [source]


def test_alias_never_creates_a_missing_facet():
    assert queries.canonical_source_values("HIG", "sponsors", ["Harbor Council"]) == []


def test_shared_alias_keeps_two_candidates_for_clarification():
    sources = ["Harbor Industry Guild (HIG)", "Highland Innovation Group"]
    assert queries.canonical_source_values("HIG", "sponsors", sources) == sources
    resolved, message = queries._resolve_list("HIG", "sponsors", {"sponsors": sources})
    assert resolved == ()
    assert "multiple source categories" in message


def test_company_alias_does_not_cross_to_account_or_publisher():
    sources = ["Harbor Industry Guild (HIG)"]
    assert queries.canonical_source_values("HIG", "accounts", sources) == []
    assert queries.canonical_source_values("HIG", "publishers", sources) == []


def test_partial_name_does_not_silently_select_a_source():
    assert queries.canonical_source_values("Harbor", "sponsors", ["Harbor Industry Guild (HIG)"]) == []


def test_exact_source_spelling_remains_available():
    source = "Harbor Industry Guild (HIG)"
    assert queries.canonical_source_values(source, "sponsors", [source]) == [source]


def test_unknown_source_is_not_a_lookup_candidate():
    assert queries.canonical_source_values("unknown", "sponsors", ["(Unknown)"]) == []
