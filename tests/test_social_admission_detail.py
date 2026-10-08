"""Source differences are visible next to the exact body and stay bounded."""

from copy import deepcopy
from types import SimpleNamespace

from flask import Flask

from observatory.config import Settings
from observatory.quality import body_hash
from observatory.record_view import register_record_page
from observatory.records import RecordDetails
from observatory.social_admission import SCHEME, public_social_admission


def raw():
    first = {"record_id": "junkipedia:101", "dataset": "social", "title": "Source title A",
             "platform": "Twitter", "account": "Company account A", "sponsor": "Company A",
             "published_at": "2023-05-02", "url": "https://twitter.com/example/status/9001",
             "archive_url": "https://www.junkipedia.org/posts/101", "body": "The first original observation.",
             "annotations": [], "raw": {"private": "PRIVATE-PATH-TRAP"}}
    second = {**first, "record_id": "junkipedia:102", "title": "Source title B",
              "account": "Company account B", "sponsor": "Company B", "body": "The second observation <script>trap()</script>."}
    return {"social_admission": {"scheme": SCHEME, "scope": "collected_company_posts",
                                  "count_unit": "platform_canonical_original_post_url",
                                  "canonical_url": first["url"], "member_count": 2,
                                  "conflicting_fields": ["body", "sponsor", "account"],
                                  "retrieval_status": "paused_body_disagreement", "selected_source_record_id": first["record_id"]},
            "source_variants": [{"source_record_id": item["record_id"], "source_record": item,
                                 "source_line": index, "source_sha256": "b" * 64}
                                for index, item in enumerate((first, second), 1)]}


def detail(*, links=True):
    body = "The first original observation."
    row = {"record_id": "social-post:" + "a" * 64, "version_id": "c" * 64, "dataset": "social",
           "title": "Twitter post · 9001", "url": "https://twitter.com/example/status/9001",
           "body": body, "body_hash": body_hash(body), "annotations": [], "issues": [],
           "sponsor": "", "account": "", "platform": "Twitter", "date": "2023-05-02",
           "retrievable": False, "social_admission_payload": raw()}
    instance = RecordDetails.__new__(RecordDetails)
    instance.settings = Settings(show_source_links=links)
    instance.asset_status = "test"
    instance._row = lambda identifier: row
    instance._attachments = lambda value: []
    instance._candidates = {}
    return instance.get(row["record_id"])


def test_record_details_exposes_company_post_scope_and_each_conflicting_observation():
    projected = detail()
    public = projected["social_admission"]
    assert public["member_count"] == 2
    assert "conflicting text" in projected["body_label"]
    assert "display only" in projected["body_note"]
    assert "paused for RAG" in projected["body_note"]
    assert projected["sponsor"] == projected["account"] == ""
    assert {item["company"] for item in public["variants"]} == {"Company A", "Company B"}
    assert "PRIVATE-PATH-TRAP" not in str(projected)
    assert "source_line" not in str(public) and "source_sha256" not in str(public)


def test_source_link_configuration_applies_to_all_variants():
    projected = detail(links=False)
    assert projected["url"] == ""
    assert all(item["url"] == item["archive_url"] == "" for item in projected["social_admission"]["variants"])
    assert projected["social_admission"]["variants"][0]["body"] == "The first original observation."


def test_detail_page_labels_differences_and_escapes_exact_text_without_claiming_ad_status():
    row = detail()
    app = Flask(__name__)
    register_record_page(app, SimpleNamespace(get=lambda identifier: row))
    response = app.test_client().get("/records/" + row["record_id"])
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    for expected in ("Collected company social posts", "not verified paid ads", "2 supplied source observations",
                     "Unresolved source differences", "RAG retrieval is paused", "Source title A", "Source title B",
                     "The first original observation.", "Company account A", "Company account B", "Company A", "Company B"):
        assert expected in text
    assert "&lt;script&gt;trap()&lt;/script&gt;" in text
    assert "<script>trap()</script>" not in text and "PRIVATE-PATH-TRAP" not in text


def test_stale_current_body_or_url_cannot_receive_source_variants_projection():
    payload = raw()
    before = deepcopy(payload)
    assert public_social_admission(payload, current_body="A changed body") is None
    assert public_social_admission(payload, current_url="https://twitter.com/example/status/9999") is None
    assert payload == before


def test_missing_or_incomplete_admission_never_appears_as_approved_scope():
    assert public_social_admission({}) is None
    payload = raw()
    payload["social_admission"]["member_count"] = 3
    assert public_social_admission(payload) is None
