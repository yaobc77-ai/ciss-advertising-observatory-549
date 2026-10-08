"""Bounded projections of stored metadata, distinct from display normalization.

Only adapter-defined cells are selected. No source filenames, import paths,
private identifiers, bodies, notes or complete raw rows are returned.
"""

from __future__ import annotations

import json
import re

METADATA_FIELDS = (
    "title", "publisher", "sponsor", "original_url", "publication_date",
    "collection_search_term", "disclosure_language", "disclosure_location",
)
_DISPLAY_FIELDS = {
    "title": "title", "publisher": "publisher", "sponsor": "sponsor",
    "original_url": "url", "publication_date": "source_date",
    "collection_search_term": "keyword", "disclosure_language": "display_disclosure",
}
_NATIVE_CELLS = {
    "title": ("title", "Title"), "publisher": ("publisher", "Publisher"),
    "sponsor": ("sponsor", "Sponsor"), "original_url": ("url", "URL"),
    "publication_date": ("date", "Date"),
    "collection_search_term": ("keyword", "Keyword"),
    "disclosure_language": ("disclosure language",),
    # The supplied native workbook calls its position/style note a description.
    # Keep the physical column path in source_refs; do not create a new source.
    "disclosure_location": ("disclosure location", "disclosure description"),
}
_COPY_CELLS = {key: value for key, value in _NATIVE_CELLS.items()
               if key not in {"disclosure_language", "disclosure_location"}}
_SOCIAL_CELLS = {
    "title": (("title",),), "publisher": (("publisher",),),
    "sponsor": (("parent_entity",),), "original_url": (("urls", "post_url"),),
    "publication_date": (("date_time", "published_at"),),
    "collection_search_term": (("keyword",),),
    "disclosure_language": (("disclosure",),),
    "disclosure_location": (("disclosure location",),),
}
MAX_CELL_CHARACTERS = 8000
MAX_ORIGINS = 20
_HASH = re.compile(r"[0-9a-f]{64}")


def _sql_text(value):
    return "'" + value.replace("'", "''") + "'"


def _origin_sql(payload, path, kind, priority, cells, role):
    """All arguments are fixed module constants, never model-supplied SQL."""
    selected = []
    for field, aliases in cells.items():
        for alias in aliases:
            full_path = (*path, *((alias,) if isinstance(alias, str) else alias))
            sql_path = "ARRAY[" + ",".join(map(_sql_text, full_path)) + "]"
            selected.append(
                "jsonb_build_object('field'," + _sql_text(field)
                + ",'payload_field_path',to_jsonb(" + sql_path + ")"
                + ",'present',(" + payload + "#>" + sql_path + ") IS NOT NULL"
                + ",'value'," + payload + "#>" + sql_path + ")"
            )
    provenance = (
        "COALESCE((SELECT jsonb_agg(jsonb_build_object('row',q->'row',"
        "'row_basis',q->'row_basis','sha256',q->'sha256')) FROM jsonb_array_elements("
        "CASE WHEN jsonb_typeof(" + payload + "->'provenance')='array' THEN "
        + payload + "->'provenance' ELSE '[]'::jsonb END) q WHERE q->>'role'="
        + _sql_text(role) + "),'[]'::jsonb)"
    )
    return ("jsonb_build_object('origin_kind'," + _sql_text(kind)
            + ",'priority'," + str(priority) + ",'cells',jsonb_build_array("
            + ",".join(selected) + "),'provenance'," + provenance + ")")


def metadata_origins_sql(payload="v.payload"):
    """Select only known cells and row/hash provenance in the scoped statement."""
    native = [
        _origin_sql(payload, ("raw", "metadata"), "original_metadata", 0,
                    _NATIVE_CELLS, "disclosure_and_notes"),
        _origin_sql(payload, ("raw", "baseline"), "baseline_source_copy", 1,
                    _COPY_CELLS, "baseline"),
        _origin_sql(payload, ("raw", "supplement"), "supplement_source_copy", 1,
                    _COPY_CELLS, "candidate"),
    ]
    social = _origin_sql(payload, ("raw", "source_row"), "social_source_row", 0,
                         _SOCIAL_CELLS, "social_export")
    return ("CASE WHEN p.dataset='native' THEN jsonb_build_array("
            + ",".join(native) + ") ELSE jsonb_build_array(" + social + ") END")


def _provenance(refs):
    selected = []
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, dict):
            continue
        clean = {}
        if type(ref.get("row")) is int and ref["row"] > 0:
            clean["row"] = ref["row"]
        if ref.get("row_basis") == "logical_record_including_header":
            clean["row_basis"] = ref["row_basis"]
        if isinstance(ref.get("sha256"), str) and _HASH.fullmatch(ref["sha256"]):
            clean["source_sha256"] = ref["sha256"]
        if clean and clean not in selected:
            selected.append(clean)
    missing = [field for field in ("row", "source_sha256")
               if not selected or any(field not in ref for ref in selected)]
    return {"status": "needs_review" if len(selected) > 1 else "unknown" if missing else "recorded",
            "references": selected, "unknown_fields": missing}


def _cell_status(value):
    if value is None or isinstance(value, str) and value.strip().casefold() in {
        "", "n/a", "na", "none", "null", "nan", "unknown"
    }:
        return "unknown"
    if not isinstance(value, (str, int, float, bool)):
        return "unsupported_value"
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False)) > MAX_CELL_CHARACTERS:
        return "exceeds_limit"
    return "recorded"


def project_record_metadata(row, fields=None, *, public_url, links_enabled=True):
    """Return exact stored scalar cells and independently labeled display values.

    An Excel metadata row outranks a cleaned CSV copy. Only differing values
    at the same priority are ambiguous; a cleaning difference is not an Excel
    conflict. Absence never establishes absence of disclosure on a live page.
    """
    fields = tuple(fields or METADATA_FIELDS)
    if any(field not in METADATA_FIELDS for field in fields):
        raise ValueError("Unknown metadata field")
    binding = {key: row.get(key) for key in ("record_id", "dataset", "version_id", "body_hash")}
    if any(not isinstance(binding[key], str) or not binding[key]
           for key in ("record_id", "version_id", "body_hash")):
        raise ValueError("Stored metadata needs a current record/version/body hash")
    originals, display, source_refs = {}, {}, []
    origins = row.get("metadata_origins") or []
    if not isinstance(origins, list) or len(origins) > MAX_ORIGINS:
        raise ValueError("Stored metadata origins exceed the bounded projection")
    review_required = False
    for field in fields:
        candidates = []
        for origin in origins:
            if not isinstance(origin, dict) or origin.get("origin_kind") not in {
                "original_metadata", "baseline_source_copy", "supplement_source_copy", "social_source_row"
            } or origin.get("priority") not in (0, 1):
                continue
            for cell in origin.get("cells", []):
                if not isinstance(cell, dict) or cell.get("field") != field or cell.get("present") is not True:
                    continue
                path = cell.get("payload_field_path")
                if (not isinstance(path, list) or not path or any(not isinstance(part, str)
                        or len(part) > 80 for part in path)):
                    raise ValueError("Stored metadata has an invalid field path")
                value = cell.get("value")
                status = _cell_status(value)
                if field == "original_url":
                    if not links_enabled:
                        value, status = None, "source_links_hidden"
                    elif not isinstance(value, str) or not public_url(value):
                        value, status = None, "unsafe_url_withheld"
                    else:
                        value = public_url(value)
                elif status in {"unsupported_value", "exceeds_limit"}:
                    value = None
                candidate = {"value": value, "status": status,
                             "origin_kind": origin["origin_kind"], "payload_field_path": path,
                             "provenance": _provenance(origin.get("provenance"))}
                candidates.append((origin["priority"], candidate))
                source_refs.append({**binding, "field": field, "payload_field_path": path,
                                    "origin_kind": origin["origin_kind"],
                                    "provenance": candidate["provenance"]})
        if not candidates:
            originals[field] = {"status": "not_recorded", "value": None,
                                "provenance": {"status": "unknown", "references": [],
                                               "unknown_fields": ["row", "source_sha256"]}}
        else:
            priority = min(item[0] for item in candidates)
            preferred = [item for rank, item in candidates if rank == priority]
            copies = [item for rank, item in candidates if rank > priority]
            resolved = preferred
            if field == "disclosure_location":
                # A blank alternate column is missing information, not a
                # competing location. Retain every cell in stored_sources.
                recorded = [item for item in preferred if item["status"] == "recorded"]
                resolved = recorded or preferred
            distinct = {json.dumps(item["value"], ensure_ascii=False, sort_keys=True) for item in resolved}
            conflict = len(distinct) > 1
            review_required |= conflict or any(item["provenance"]["status"] == "needs_review" for item in preferred)
            originals[field] = ({"status": "needs_review", "value": None,
                                 "reason": "same_priority_stored_sources_disagree"} if conflict else dict(resolved[0]))
            originals[field].update(stored_sources=preferred, source_copies=copies)
        display[field] = row.get(_DISPLAY_FIELDS[field]) if field in _DISPLAY_FIELDS else None
        if field == "original_url":
            display[field] = public_url(display[field]) if links_enabled else ""
    # Admission disagreements remain unknown; the selected display copy is not
    # an adjudication of multiple original social observations.
    social_conflicts = row.get("metadata_conflicting_fields") or []
    for field, stored_field in (("sponsor", "sponsor"), ("publication_date", "published_at")):
        if field in originals and stored_field in social_conflicts:
            originals[field].update(status="needs_review", value=None,
                                    reason="stored_source_observations_disagree")
            review_required = True
    return {"status": "ok", "record": binding, "original_fields": originals,
            "display_fields": display, "source_refs": source_refs,
            "review_required": review_required, "source": "stored_original_metadata",
            "online_truth": "not_established", "semantic_support": "not_verified",
            "message": "These are stored source cells, not current online verification. Blank or missing disclosure is unknown; display normalization is shown separately."}
