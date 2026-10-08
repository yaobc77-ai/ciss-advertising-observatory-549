"""Statistics examples retain a post's public counting scope without raw data."""

from types import SimpleNamespace

import pytest

from observatory.config import Settings
from observatory.models import Filters
from observatory.service import Service


@pytest.mark.parametrize("dataset", ["native", "social", "all"])
def test_statistics_preserves_social_scope_fields_and_original_native_whitelist(dataset):
    calls = []

    def dashboard(filters, **_kwargs):
        calls.append(filters)
        row = {"record_id": filters.dataset + "-1", "dataset": filters.dataset,
               "title": "Stored record", "retrievable": False,
               "collection_scope": "collected_company_posts",
               "count_unit": "platform_canonical_original_post_url",
               "paid_ad_status": "not_verified",
               "body": "PRIVATE SOURCE TEXT", "raw": {"private": "SOURCE VARIANTS"}}
        return {"stats": {"total": 1, "retrievable": 0, "unknown_dates": 1},
                "page": {"rows": [row]}}

    service = Service(Settings(), db=SimpleNamespace(), rag=object())
    service.dashboard = dashboard
    plan = SimpleNamespace(kind="count", filters=Filters(dataset=dataset),
                           group_by=None, scope_notes=[])
    data = service._statistics_answer(plan).structured_result
    assert [scope.dataset for scope in calls] == (["native", "social"] if dataset == "all" else [dataset])
    for row in data["records"]:
        assert "body" not in row and "raw" not in row
        fields = {"collection_scope", "count_unit", "paid_ad_status"}
        if row["dataset"] == "social":
            assert {key: row[key] for key in fields} == {
                "collection_scope": "collected_company_posts",
                "count_unit": "platform_canonical_original_post_url",
                "paid_ad_status": "not_verified"}
        else:
            assert not fields.intersection(row)
    assert all(item["total"] == 1 and item["retrievable"] == 0 for item in data["collections"])
