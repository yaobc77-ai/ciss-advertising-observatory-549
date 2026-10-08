"""Statistics routing must resolve metadata and narrow every active constraint."""

from datetime import date

import pytest

from observatory.models import Filters
from observatory.structured_queries import plan_question, validate_share_scope


@pytest.fixture
def facets():
    return {
        "publishers": ["The New York Times", "The Washington Post", "Politico", "Aster Gazette", "(Unknown)"],
        "sponsors": ["exxonmobil", "totalenergies", "cera", "Orion Energy", "The Williams Companies, Inc."],
        "keywords": ["carbon capture"],
        "labels": ["false_solutions"],
    }


@pytest.mark.parametrize("question,kind,group,field,expected", [
    ("How many native ads are from the New York Times?", "count", None, "publishers", ["The New York Times"]),
    ("Which publishers is ExxonMobil working with?", "list_publishers", "publishers", "sponsors", ["exxonmobil"]),
    ("Which fossil fuel companies has the Washington Post worked with?", "list_sponsors", "sponsors", "publishers", ["The Washington Post"]),
    ("How many native advertisements were published by NYT?", "count", None, "publishers", ["The New York Times"]),
    ("Which sponsors are sponsoring native ads at WaPo?", "list_sponsors", "sponsors", "publishers", ["The Washington Post"]),
    ("Which news outlets does TotalEnergies work with?", "list_publishers", "publishers", "sponsors", ["totalenergies"]),
    ("How many records are from Aster Gazette?", "count", None, "publishers", ["Aster Gazette"]),
    ("Which outlets has Orion Energy worked with?", "list_publishers", "publishers", "sponsors", ["Orion Energy"]),
    ("Which publishers for CERAWeek?", "list_publishers", "publishers", "sponsors", ["cera"]),
    ("How many ads are sponsored by The Williams Companies, Inc.?", "count", None, "sponsors", ["The Williams Companies, Inc."]),
    ("纽约时报有多少篇原生广告？", "count", None, "publishers", ["The New York Times"]),
    ("ExxonMobil和哪些媒体合作？", "list_publishers", "publishers", "sponsors", ["exxonmobil"]),
])
def test_known_source_categories_and_unseen_names(question, kind, group, field, expected, facets):
    plan = plan_question(question, Filters(), facets)
    assert plan.status == "ready"
    assert plan.kind == kind
    assert plan.group_by == group
    assert getattr(plan.filters, field) == expected
    assert plan.matched_entities[field] == tuple(expected)


def test_multiple_entities_and_cross_field_constraints(facets):
    plan = plan_question(
        "How many native ads are from NYT and Washington Post and sponsored by ExxonMobil?",
        Filters(), facets,
    )
    assert plan.status == "ready"
    assert plan.filters.publishers == ["The New York Times", "The Washington Post"]
    assert plan.filters.sponsors == ["exxonmobil"]


def test_intersection_preserves_other_filters_and_input(facets):
    scope = Filters(
        publishers=["The Washington Post"], sponsors=["exxonmobil", "Orion Energy"],
        keywords=["existing-term"], labels=["historical-tag"], record_ids=["r1"],
        date_from=date(2020, 6, 1), date_to=date(2022, 3, 31),
    )
    original = scope.model_dump()
    # Naming a publisher outside the active selection asks instead of silently dropping it.
    assert plan_question("How many ads from NYT and WaPo in 2021?", scope, facets).status == "clarify"
    plan = plan_question("How many ads from WaPo in 2021?", scope, facets)
    assert plan.status == "ready"
    assert plan.filters.publishers == ["The Washington Post"]
    assert plan.filters.sponsors == scope.sponsors
    assert plan.filters.keywords == scope.keywords
    assert plan.filters.labels == scope.labels
    assert plan.filters.record_ids == scope.record_ids
    assert plan.filters.date_from == date(2021, 1, 1)
    assert plan.filters.date_to == date(2021, 12, 31)
    assert plan.filters.include_unknown_dates is False
    assert scope.model_dump() == original


@pytest.mark.parametrize("phrase,lower,upper", [
    ("in 2021", date(2021, 1, 1), date(2021, 12, 31)),
    ("during 2021", date(2021, 1, 1), date(2021, 12, 31)),
    ("from 2020 to 2022", date(2020, 1, 1), date(2022, 12, 31)),
    ("between 2020 and 2022", date(2020, 1, 1), date(2022, 12, 31)),
    ("in 2020–2022", date(2020, 1, 1), date(2022, 12, 31)),
    ("from 2021-04-03 to 2021-07-08", date(2021, 4, 3), date(2021, 7, 8)),
])
def test_explicit_date_forms(phrase, lower, upper, facets):
    plan = plan_question(f"Which publishers is ExxonMobil working with {phrase}?", Filters(), facets)
    assert plan.status == "ready"
    assert plan.filters.date_from == lower
    assert plan.filters.date_to == upper
    assert plan.filters.include_unknown_dates is False


def test_year_range_intersects_existing_dates(facets):
    plan = plan_question(
        "How many ads from NYT from 2020 to 2022?",
        Filters(date_from=date(2021, 5, 2), date_to=date(2023, 1, 1)), facets,
    )
    assert plan.status == "ready"
    assert plan.filters.date_from == date(2021, 5, 2)
    assert plan.filters.date_to == date(2022, 12, 31)


@pytest.mark.parametrize("question,scope", [
    ("How many ads from NYT?", Filters(publishers=["Politico"])),
    ("Which publishers is ExxonMobil working with?", Filters(sponsors=["totalenergies"])),
    ("How many ads from NYT in 2019?", Filters(date_from=date(2021, 1, 1))),
    ("How many native ads from NYT?", Filters(dataset="social")),
    ("How many social ads from NYT?", Filters(dataset="native")),
    ("How many ads from NYT?", Filters(date_from=date(2023, 1, 1), date_to=date(2021, 1, 1))),
])
def test_conflicting_scope_never_broadens(question, scope, facets):
    plan = plan_question(question, scope, facets)
    assert plan.status == "clarify"
    assert plan.filters is None
    assert plan.message


def test_all_scope_can_narrow_to_explicit_collection(facets):
    plan = plan_question("How many native ads from NYT?", Filters(dataset="all"), facets)
    assert plan.status == "ready"
    assert plan.filters.dataset == "native"


@pytest.mark.parametrize("question", [
    "How many native ads are from Unlisted News?",
    "Which publishers does Imaginary Oil work with?",
    "How many ads from NYT and Unlisted News?",
    "How many ads from NYT or WaPo?",
    "How many native ads?",
    "How many ads from NYT last year?",
    "Which publishers is ExxonMobil working with since 2021?",
    "How many ads from NYT from 2023 to 2021?",
    "How many ads from NYT in 2021-02-30?",
    "How many ads from NYT between 2020 and tomorrow?",
    "How many ads from NYT published by Politico?",
    "How many ads from NYT; DROP TABLE records?",
])
def test_missing_or_unexplained_constraints_require_clarification(question, facets):
    plan = plan_question(question, Filters(), facets)
    assert plan.status == "clarify"
    assert plan.filters is None


def test_publisher_alias_collision_is_not_merged(facets):
    facets["publishers"].append("New York Times")
    plan = plan_question("How many ads from NYT?", Filters(), facets)
    assert plan.status == "clarify"
    assert "multiple" in plan.message


def test_entity_in_two_namespaces_needs_explicit_role(facets):
    facets["sponsors"].append("Politico")
    ambiguous = plan_question("How many ads from Politico?", Filters(), facets)
    assert ambiguous.status == "clarify"
    assert "both outlet and sponsor" in ambiguous.message
    explicit = plan_question("How many ads published by Politico?", Filters(), facets)
    assert explicit.status == "ready"
    assert explicit.filters.publishers == ["Politico"]


@pytest.mark.parametrize("question", [
    "How many ads mention carbon capture?",
    "How many native ads from NYT contain greenwashing claims?",
    "How many greenwashing claims are in these ads?",
])
def test_content_counts_do_not_become_metadata_counts(question, facets):
    plan = plan_question(question, Filters(), facets)
    assert plan.status == "unsupported"
    assert plan.filters is None


@pytest.mark.parametrize("question", [
    "Which ads contain greenwashing claims?",
    "What does ExxonMobil say about carbon capture?",
    "Compare natural gas narratives in these articles.",
])
def test_evidence_questions_continue_to_rag(question, facets):
    assert plan_question(question, Filters(), facets) is None


def test_unknown_bucket_is_not_a_named_entity(facets):
    assert plan_question("How many ads from Unknown?", Filters(), facets).status == "clarify"


def test_no_date_request_preserves_unknown_date_policy(facets):
    plan = plan_question("How many ads from NYT?", Filters(include_unknown_dates=False), facets)
    assert plan.filters.include_unknown_dates is False
    assert not any("requested date range" in note for note in plan.scope_notes)


@pytest.mark.parametrize("enabled", [False, True])
def test_scope_validator_rejects_changed_publication_date_basis(enabled):
    trusted = Filters(include_inferred_dates=enabled)
    candidate = trusted.model_copy(update={"include_inferred_dates": not enabled})
    with pytest.raises(ValueError, match="publication-date basis"):
        validate_share_scope(candidate, trusted)


@pytest.mark.parametrize("enabled", [False, True])
def test_scope_validator_allows_narrowing_with_matching_publication_date_basis(enabled):
    trusted = Filters(include_inferred_dates=enabled)
    candidate = trusted.model_copy(update={"publishers": ["The New York Times"]})
    validate_share_scope(candidate, trusted)
