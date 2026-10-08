"""Minimum wire contract for catalog results; not a semantic-answer validator."""

import json
from copy import deepcopy

from .models import Filters

STATUSES = ("ok", "clarify", "not_found", "ambiguous", "invalid_request", "unavailable",
            "limited", "disabled", "not_configured", "missing_material", "no_match")


def result_schema(name):
    """Expose fixed identity/state and scoped success, leaving route payloads extensible."""
    filters = Filters.model_json_schema(mode="serialization")
    filters["required"] = list(dict.fromkeys([*filters.get("required", []), "dataset"]))
    return {
        "type": "object",
        "properties": {
            "tool": {"const": name, "type": "string"},
            "status": {"type": "string", "enum": list(STATUSES)},
            "filters": filters,
        },
        "required": ["tool", "status"],
        "allOf": [{"if": {"properties": {"status": {"const": "ok"}}},
                   "then": {"required": ["filters"]}}],
        "additionalProperties": True,
        "description": "Validates the result envelope, not source truth, completeness or semantic correctness.",
    }


def checked_catalog_result(name, result):
    """Reject malformed/foreign results without returning their unverified payload."""
    if not isinstance(result, dict) or result.get("tool") != name or result.get("status") not in STATUSES:
        raise ValueError("Invalid catalog result envelope")
    # The JSON round trip checks finite values and leaves one identical payload
    # for text and structuredContent. No coercion of serialized fields occurs.
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    checked = json.loads(encoded)
    if result["status"] == "ok" and not isinstance(result.get("filters"), dict):
        raise ValueError("Successful catalog result requires its effective scope")
    if "filters" in result:
        raw = result["filters"]
        if not isinstance(raw, dict) or "dataset" not in raw:
            raise ValueError("Invalid result scope")
        parsed = Filters.model_validate(raw)
        canonical = parsed.model_dump(mode="json")
        if any(key not in canonical or raw[key] != canonical[key] or type(raw[key]) is not type(canonical[key])
               for key in raw):
            raise ValueError("Result scope requires canonical JSON types")
    return deepcopy(checked)
