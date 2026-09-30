from observatory.models import RecordInput
from observatory.quality import (
    asset_url_reason,
    inspect_body,
    mark_duplicate_bodies,
    retrieval_boundary,
)


def codes(body):
    return {issue.code for issue in inspect_body(body)}


def test_placeholders_and_numeric_extraction_are_not_article_text():
    assert "body_missing" in codes("  ")
    assert "body_missing" in codes("N/A")
    assert "body_video_placeholder" in codes(" video\n")
    assert "body_numeric" in codes("2.0")
    assert "body_numeric" in codes(2)


def test_question_and_footer_fragments_are_flagged():
    assert {"body_short", "body_question_only"} <= codes("What is the future of work?")
    footer = "ExxonMobil Paid Post Share The news and editorial staff of The New York Times had no role in this post's creation."
    assert "body_footer_only" in codes(footer)


def test_real_article_can_contain_footer_and_normal_unicode():
    body = (
        "公司介绍能源技术。 The study describes changing energy demand and several proposed responses. "
        * 60
    )
    body += " The news and editorial staff had no role in its creation."
    assert codes(body) == set()
    assert "body_garbled" in codes(body + " broken ‚Äì text")


def test_assets_require_explicit_path_evidence_not_subject_matter():
    assert asset_url_reason("https://example.org/article/image-400x400/")
    assert asset_url_reason("https://example.org/photo.JPG?source=article")
    assert asset_url_reason("https://example.org/energy-transition-chart/") is None
    assert asset_url_reason("https://example.org/menopause/") is None


def test_exact_duplicates_are_flagged_without_merging_distinct_urls():
    body = "The same complete article text. " * 40
    records = [
        RecordInput(
            record_id=str(i),
            dataset="native",
            url=f"https://example.org/{i}",
            body=body if i < 2 else "video",
            retrievable=True,
        )
        for i in range(3)
    ]
    mark_duplicate_bodies(records)
    assert len(records) == 3
    assert all(
        "duplicate_body" in {issue.code for issue in row.issues} for row in records[:2]
    )
    assert records[0].retrievable and records[1].retrievable
    assert not records[2].issues


def test_navigation_has_exact_boundary_but_normal_topic_mentions_do_not():
    prefix = "The mollusks monitor water quality. " * 20
    body = (
        prefix
        + "For more on the subject: Biogas to offset air travel emissions More mollusk text."
    )
    assert retrieval_boundary(body) == len(prefix)
    assert "body_related_navigation" in codes(body)
    assert retrieval_boundary("Microalgae can produce biogas and biofuels.") is None
    truncated = "Available passage about energy. " * 91
    assert "body_truncated_suspected" in codes(truncated + "...")
    assert "body_truncated_suspected" not in codes("An intentional pause...")
