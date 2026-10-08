"""Public social member cards retain source metadata without native semantics."""

from copy import deepcopy

import pytest

from observatory.network_ui import record_cards


def text(component):
    if component is None:
        return ""
    if isinstance(component, (str, int)):
        return str(component)
    if isinstance(component, list):
        return "".join(text(item) for item in component)
    return text(getattr(component, "children", None))


def source(**updates):
    return {"record_id": "post:source-1", "version_id": "current-version", "dataset": "social",
            "title": "Stored source post", "account": "NYT / 来源账号", "platform": "Twitter",
            "sponsor": "Source Parent Entity", "publisher": "Native-outlet trap",
            "date": "2024-02-01", "url": "https://example.org/post", "keyword": "energy",
            "body": "private body must not be shown", "raw": {"private": "never-public"}, **updates}


def test_social_members_show_exact_account_platform_affiliation_with_source_limit():
    row = source()
    original = deepcopy(row)
    cards = record_cards([row])
    rendered = text(cards)
    assert "Account: NYT / 来源账号" in rendered
    assert "Platform: Twitter" in rendered
    assert "Company affiliation: Source Parent Entity" in rendered
    assert "Source-listed company affiliation does not establish paid sponsorship." in rendered
    assert "Native-outlet trap" not in rendered and "outlet" not in rendered.lower()
    assert "never-public" not in rendered and "private body" not in rendered
    assert row == original
    assert cards[0].children[0].children[0].href == "/records/post%3Asource-1"
    assert "Version current-version" in rendered


@pytest.mark.parametrize("missing", [None, "", "(Unknown)"])
def test_missing_social_fields_use_their_own_names_without_unknown_outlet_or_sponsor(missing):
    card = record_cards([source(account=missing, platform=missing, sponsor=missing, publisher="")])[0]
    rendered = text(card)
    assert "Account: Unknown account" in rendered
    assert "Platform: Unknown platform" in rendered
    assert "Company affiliation: Unknown company affiliation" in rendered
    assert "Unknown outlet" not in rendered and "Unknown sponsor" not in rendered
    assert "does not establish paid sponsorship" in rendered


def test_same_name_social_members_retain_each_platform_without_merging_or_aliases():
    cards = record_cards([source(record_id="x", platform="Twitter"),
                          source(record_id="youtube", platform="YouTube")])
    assert len(cards) == 2
    assert "Account: NYT / 来源账号" in text(cards[0]) and "Account: NYT / 来源账号" in text(cards[1])
    assert "Platform: Twitter" in text(cards[0]) and "Platform: YouTube" in text(cards[1])
    assert "The New York Times" not in text(cards)


@pytest.mark.parametrize("missing", [False, True])
def test_native_member_metadata_and_structure_remain_unchanged(missing):
    row = source(dataset="native", publisher="" if missing else "The Washington Post",
                 sponsor="" if missing else "exxonmobil")
    card = record_cards([row])[0]
    assert len(card.children) == 4
    metadata = card.children[1]
    assert metadata.className == "muted record-metadata"
    assert text(metadata) == ("Unknown sponsor · Unknown outlet" if missing
                              else "ExxonMobil · The Washington Post")
    assert metadata.children[0].className == "record-sponsor"
    assert metadata.children[2].className == "record-outlet"
    assert "Account:" not in text(card) and "Platform:" not in text(card)
    assert "Company affiliation:" not in text(card) and "paid sponsorship" not in text(card)


def test_social_member_links_follow_existing_public_link_policy():
    hidden = record_cards([source()], links_enabled=False)[0]
    unsafe = record_cards([source(url="https://user:secret@example.org/post")])[0]
    assert "Original source" not in text(hidden) and "Original source" not in text(unsafe)
    assert "Account: NYT / 来源账号" in text(hidden)
    assert hidden.children[0].children[0].href == "/records/post%3Asource-1"
