"""Opt-in historical annotation reads in a reused owned loopback test schema."""

import hashlib
import json
import os
from copy import deepcopy
from datetime import date

import pytest
from test_social_accounts_integration import isolated_db as isolated_db
from test_social_accounts_integration import post

from observatory.config import Settings
from observatory.models import Filters, ImportBatch, RecordInput
from observatory.quality import body_hash
from observatory.records import RecordDetails
from observatory.research_tools import ToolCatalog
from observatory.service import Service
from observatory.social_admission import _admit_group
from observatory.social_annotations import SCHEME, SOCIAL_LABELS, STATUS

pytestmark = pytest.mark.integration
CHECKS = {"historical_detail_current_sql", "historical_detail_version_and_admission",
          "historical_source_tool_scope"}


@pytest.fixture
def annotation_db(isolated_db):
    if os.environ.get("OBS_RUN_SOCIAL_ANNOTATIONS_INTEGRATION") != "1":
        pytest.skip("Explicit historical-annotation integration opt-in is absent")
    isolated_db.receipt["schema_version"] = "social-historical-annotation-sql-v1"
    isolated_db.receipt["limits"].append(
        "Historical source state and scope only; no classification, customer data or semantic gold.")
    return isolated_db


def admitted(value):
    """Fabricate a literal, hash-bound source; never use actual social rows."""
    if value.dataset != "social" or not value.countable:
        return value
    # Preserve the same post identity when a test replaces its current body.
    prior = value.raw.get("social_admission", {})
    number = (prior.get("selected_source_record_id", "").removeprefix("junkipedia:")
              or str(int(hashlib.sha256(value.record_id.encode()).hexdigest()[:12], 16)))
    url = f"https://twitter.com/synthetic/status/{number}"
    source = value.model_copy(update={"record_id": "junkipedia:" + number, "url": url,
        "countable": False, "retrievable": False,
        "raw": {"body_sha256": body_hash(value.body),
                "source_row": {"id": number, "post_text": value.body}}})
    result, _ = _admit_group([(1, source)], url, "a" * 64, {
        "data_sha256": "b" * 64, "selections": {"D02": {"id": "synthetic-D02"}, "D03": {"id": "synthetic-D03"}},
    })
    return RecordInput.model_validate_json(result.model_dump_json())


def annotated(identifier, positive=False, **changes):
    value = post(identifier, **changes)
    values = dict.fromkeys(SOCIAL_LABELS, False)
    if positive:
        values["green_binary"] = values["decreasing_emissions"] = True
    payload = {
        "version": SCHEME, "status": STATUS,
        "basis": "supplied_source_post_id_and_exact_body",
        "taxonomy_mapping": "source_keys_preserved_no_native_label_mapping",
        "values": values, "labels": [key for key in SOCIAL_LABELS if values[key]],
        "body_sha256": hashlib.sha256(value.body.encode()).hexdigest(),
        "source_sha256": "a" * 64, "source_row": 1,
        "source_path": "PRIVATE-ANNOTATION-SQL",
        "explanations": {"green_explanation": "An old model comment.",
                         "private_key": "PRIVATE-ANNOTATION-SQL"},
    }
    return admitted(value.model_copy(update={"annotations": [payload]}))


def details(db, root):
    return RecordDetails(db, Settings(show_source_links=False), root=root)


def test_exact_annotation_sql_read_and_public_state(annotation_db, tmp_path):
    db = annotation_db
    items = [annotated("hist-true", positive=True), annotated("hist-false"),
             admitted(post("hist-missing")), annotated("hist-native", dataset="native")]
    db.import_batch(ImportBatch(records=items))
    public = details(db, tmp_path)
    for item in items[:2]:
        value = public.get(item.record_id)
        annotation = value["social_historical_annotation"]
        assert annotation["validation_state"] == "bound"
        assert len(annotation["values"]) == 13
        assert annotation["provenance"]["version_id"] == value["version_id"]
        assert annotation["provenance"]["body_hash"] == value["body_hash"]
        assert "PRIVATE-ANNOTATION-SQL" not in json.dumps(value)
        assert value["url"] == value["archive_url"] == ""
    assert sum(item["value"] is True for item in public.get(items[0].record_id)["social_historical_annotation"]["values"]) == 2
    assert all(item["state"] == "source_false" for item in public.get(items[1].record_id)["social_historical_annotation"]["values"])
    missing = public.get(items[2].record_id)["social_historical_annotation"]
    assert missing["validation_state"] == "missing" and all(item["value"] is None for item in missing["values"])
    assert "social_historical_annotation" not in public.get("hist-native")
    db.checked("historical_detail_current_sql", thirteen_states=True, source_private_fields_omitted=True,
               native_unchanged=True, missing_is_not_false=True)


def test_new_version_does_not_borrow_prior_annotation_and_admission_is_enforced(annotation_db, tmp_path):
    db = annotation_db
    old = annotated("hist-version", positive=True)
    db.import_batch(ImportBatch(records=[old]))
    public = details(db, tmp_path)
    prior = public.get(old.record_id)
    changed = admitted(old.model_copy(update={"body": old.body + " A changed source text."}))
    db.import_batch(ImportBatch(records=[changed]))
    current = public.get(old.record_id)
    assert current["version_id"] != prior["version_id"]
    assert current["social_historical_annotation"]["validation_state"] == "invalid"
    assert all(item["value"] is None for item in current["social_historical_annotation"]["values"])
    duplicate = annotated("hist-duplicate")
    duplicate = duplicate.model_copy(update={"annotations": duplicate.annotations + deepcopy(duplicate.annotations)})
    inactive = annotated("hist-inactive")
    db.import_batch(ImportBatch(records=[duplicate, annotated("hist-pending", countable=False, retrievable=False), inactive]))
    assert public.get(duplicate.record_id)["social_historical_annotation"]["validation_state"] == "ambiguous"
    assert public.get("hist-pending") is None
    with db.connect() as conn:
        conn.execute("UPDATE records SET active=false WHERE record_id=%s", (inactive.record_id,))
        stored = conn.execute("SELECT count(*) AS n FROM annotations a JOIN record_versions v "
                              "ON v.version_id=a.version_id WHERE v.record_id=%s", (old.record_id,)).fetchone()["n"]
    assert stored == 2 and public.get(inactive.record_id) is None
    db.checked("historical_detail_version_and_admission", changed_body_not_false=True,
               prior_annotations_retained=True, duplicate_unknown=True, pending_and_inactive_unreadable=True)


def test_source_tool_preserves_all_scope_filters_and_link_setting(annotation_db):
    db = annotation_db
    chosen = annotated("hist-chosen", positive=True, account="Chosen", sponsor="Chosen Company")
    outsider = annotated("hist-other", account="Other", sponsor="Chosen Company")
    db.import_batch(ImportBatch(records=[chosen, outsider]))
    settings = Settings(show_source_links=False, web_search_enabled=False)
    filters = Filters(dataset="social", accounts=["Chosen"], sponsors=["Chosen Company"],
                      platforms=["Twitter"], date_from=date(2020, 3, 1), date_to=date(2020, 3, 1),
                      include_unknown_dates=False, record_ids=[chosen.record_id, outsider.record_id])
    tools = ToolCatalog(Service(settings, db=db, rag=object()), filters)
    result = tools.call("get_record_sources", {"record_id": chosen.record_id})
    assert result["status"] == "ok" and len(result["annotations"]) == 1
    assert result["social_historical_annotation"]["validation_state"] == "bound"
    assert result["annotations"][0] == result["social_historical_annotation"]
    assert result["source_artifacts"] == [] and result["source_relations"] == []
    assert result["record"]["url"] == result["record"]["archive_url"] == ""
    assert result["source_refs"][0]["version_id"] == result["social_historical_annotation"]["provenance"]["version_id"]
    assert tools.call("get_record_sources", {"record_id": outsider.record_id})["status"] == "clarify"
    assert tools.base_filters == filters and "PRIVATE-ANNOTATION-SQL" not in json.dumps(result)
    db.checked("historical_source_tool_scope", trusted_accounts_company_platform_date_records_preserved=True,
               source_hash_bound=True, hidden_links_not_exposed=True, outside_record_refused=True)
