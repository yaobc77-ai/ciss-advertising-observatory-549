"""The shared entity registry: validation, matching and cross-collection spellings."""

import json

import pytest

from observatory.entities import REGISTRY_PATH, Registry, expand, mentioned_in, registry


def data():
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def org(oid, name, values, **extra):
    return {"id": oid, "name": name, "type": "company", "source_values": values, **extra}


def build(organizations, outlets=()):
    raw = json.dumps({"organizations": organizations, "outlets": list(outlets)}).encode()
    return Registry(json.loads(raw), raw)


def test_shipped_registry_is_valid_and_unreviewed():
    current = registry()
    assert current.version.startswith("entity-registry-v1:")
    assert current.review["status"] == "ai_proposed" and current.review["reviewer"] is None
    assert all(entity.review_status == "ai_proposed" for entity in current.entities.values())
    types = {entity.type for entity in current.entities.values() if entity.kind == "organization"}
    assert types == {"company", "trade_association", "event"}


def test_one_source_spelling_cannot_belong_to_two_organizations():
    with pytest.raises(ValueError, match="belongs to two entities"):
        build([org("org:a", "Alpha", {"native.sponsor": ["alpha"]}),
               org("org:b", "Beta", {"native.sponsor": ["alpha"]})])


def test_an_alias_shared_by_two_organizations_is_rejected():
    with pytest.raises(ValueError, match="shared by"):
        build([org("org:a", "Alpha Energy", {"native.sponsor": ["alpha"]}, aliases=["AE"]),
               org("org:b", "Beta Energy", {"native.sponsor": ["beta"]}, aliases=["AE"])])


def test_unknown_type_is_rejected():
    with pytest.raises(ValueError, match="Unknown organization type"):
        build([{**org("org:a", "Alpha", {"native.sponsor": ["alpha"]}), "type": "ministry"}])


def test_cross_collection_spellings_expand_within_the_scope():
    entity, spellings = expand("Exxon Mobil", "sponsors", {"exxonmobil", "ExxonMobil", "bp"})
    assert entity.id == "org:exxonmobil" and spellings == ["ExxonMobil", "exxonmobil"]
    assert expand("ExxonMobil", "sponsors", {"exxonmobil"})[1] == ["exxonmobil"]
    assert expand("Statoil", "sponsors", {"statoil", "Equinor"})[1] == ["Equinor", "statoil"]
    assert expand("Imaginary Petroleum", "sponsors", {"Imaginary Petroleum"}) == (None, [])


def test_owner_only_covers_listed_spellings():
    current = registry()
    assert current.owner("ExxonMobil", "sponsor").id == "org:exxonmobil"
    assert current.owner("Exxon", "sponsor") is None  # an alias is a name, not a source spelling
    assert current.resolve("Exxon", "sponsor").id == "org:exxonmobil"


@pytest.mark.parametrize("question,value,expected", [
    ("How many BP ads ran in 2021?", "bp", True),
    ("What is the total number of ads?", "totalenergies", False),
    ("How many ads did TotalEnergies place?", "totalenergies", True),
    ("埃克森美孚在华盛顿邮报投了多少广告", "exxonmobil", True),
    ("Ads by the American Petroleum Institute", "api", True),
    ("Which ads mention a rapid response?", "api", False),
    ("Did Statoil sponsor any articles?", "Equinor", True),
    ("How many NYT articles?", "The New York Times", True),
    ("How many ads mention the bpm of music?", "bp", False),
])
def test_mentions_use_word_boundaries_and_exact_case_for_short_forms(question, value, expected):
    dimension = "publishers" if value == "The New York Times" else "sponsors"
    assert mentioned_in(value, question, dimension) is expected


def test_supplemented_and_out_of_scope_records_carry_their_basis():
    current = data()
    [fill] = current["supplemented_values"]
    assert fill["organization"] == "org:ceraweek" and fill["review_status"] == "ai_proposed"
    assert "/ceraweek/" in fill["basis"] and fill["version_id"]
    excluded = current["out_of_scope_records"]
    assert len(excluded) == 65 and len({item["record_id"] for item in excluded}) == 65
    assert all(item["dataset"] == "social" and item["basis"] and item["version_id"] for item in excluded)
