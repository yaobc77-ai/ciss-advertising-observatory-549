"""Public, body-bound views of the supplied historical social classification.

These are source export values, not fresh classifications or factual findings.
The caller must first enforce current-record scope and publication eligibility.
"""

import hashlib
import re

from .social_archive import SOCIAL_LABELS

SCHEME = "claims-social-export-v1"
STATUS = "historical_automatic_unverified"
SOCIAL_STATES = ("source_true", "source_false", "unknown")
_STATE_DISPLAY = {
    "source_true": "Source export recorded True",
    "source_false": "Source export recorded False",
    "unknown": "Unknown annotation",
}
_HASH = re.compile(r"[0-9a-f]{64}")
_MACROS = {"green_binary", "fossil_fuel_binary"}
_DISPLAY = {
    "green_binary": "Green messaging",
    "decreasing_emissions": "Decreasing emissions",
    "renewable_energy": "Renewable energy",
    "other_viable_solutions": "Other viable solutions",
    "false_solutions": "False solutions",
    "recycling_waste_management": "Recycling and waste management",
    "nature_conservation_references": "Nature conservation references",
    "broad_environmental_concepts": "Broad environmental concepts",
    "green_policies_politics": "Green policies and politics",
    "fossil_fuel_binary": "Fossil fuel messaging",
    "fossil_fuel_explicit": "Explicit fossil fuel references",
    "petrochemicals_other_ff_derivatives_and_industry": "Petrochemicals, other fossil fuel derivatives and industry",
    "fossil_fuel_implicit": "Implicit fossil fuel references",
}
_EXPLANATIONS = {
    "green_explanation": "Historical green-messaging explanation",
    "fossil_fuel_explanation": "Historical fossil-fuel explanation",
    "explanation": "Historical classification explanation",
}
NOTE = (
    "These are the original automated social export labels, not reviewed "
    "greenwashing findings or fact checks. True and False describe what that "
    "export recorded. Unknown means no usable annotation is bound to this "
    "stored text. The labels can overlap and use a separate scheme from native "
    "advertising labels and CLAIMS2."
)


def social_label_metadata():
    """Return source code names and levels without suggesting a new taxonomy."""
    return [
        {"key": key, "label": _DISPLAY[key],
         "level": "macro" if key in _MACROS else "subcode"}
        for key in SOCIAL_LABELS
    ]


def social_state_id(key, state):
    """Encode exactly one of the 39 supported historical source states."""
    if key not in SOCIAL_LABELS or state not in SOCIAL_STATES:
        raise ValueError("Unsupported historical social label state")
    return f"{SCHEME}:{key}:{state}"


def parse_social_state_id(value):
    """Decode this scheme strictly; return None for other label schemes.

    A typo in our namespace is an invalid filter, never an ignored request.
    Human-friendly display names are intentionally not accepted as IDs.
    """
    if not isinstance(value, str):
        raise ValueError("Historical label IDs must be strings")
    if value != SCHEME and not value.startswith(f"{SCHEME}:"):
        return None
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError("Unsupported historical social label state")
    _, key, state = parts
    if social_state_id(key, state) != value:
        raise ValueError("Unsupported historical social label state")
    return key, state


def social_state_options():
    """Fixed public filter choices; multiple choices use OR, not AND."""
    return [
        {"label": f"{item['label']} · {_STATE_DISPLAY[state]}",
         "value": social_state_id(item["key"], state)}
        for item in social_label_metadata() for state in SOCIAL_STATES
    ]


def _is_hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def social_annotation_details(row):
    """Return thirteen source states, or None for a non-social record.

    Only one exact scheme may apply. All values and provenance must be valid;
    a partial or stale annotation cannot establish a recorded False. Both DB
    ``{ordinal, payload}`` entries and prepared annotation objects are accepted.
    Input dictionaries are never changed, and unknown/private fields are omitted.
    """
    if row.get("dataset") != "social":
        return None
    body = row.get("body")
    matched = (
        isinstance(body, str) and _is_hash(row.get("body_hash"))
        and hashlib.sha256(body.encode("utf-8")).hexdigest() == row["body_hash"]
    )
    candidates = []
    entries = row.get("annotations", [])
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload", entry)
            if isinstance(payload, dict) and payload.get("version") == SCHEME:
                candidates.append((entry, payload))
    state = "missing"
    accepted = None
    if not matched:
        state = "body_mismatch"
    elif len(candidates) > 1:
        state = "ambiguous"
    elif len(candidates) == 1:
        entry, payload = candidates[0]
        values = payload.get("values")
        valid = (
            payload.get("status") == STATUS
            and payload.get("basis") == "supplied_source_post_id_and_exact_body"
            and payload.get("taxonomy_mapping") == "source_keys_preserved_no_native_label_mapping"
            and isinstance(values, dict) and set(values) == set(SOCIAL_LABELS)
            and all(type(values[key]) is bool for key in SOCIAL_LABELS)
            and _is_hash(payload.get("source_sha256"))
            and type(payload.get("source_row")) is int and payload["source_row"] > 0
            and payload.get("body_sha256") == row["body_hash"]
            and ("version_id" not in entry or entry["version_id"] == row.get("version_id"))
            and ("version_id" not in payload or payload["version_id"] == row.get("version_id"))
        )
        if valid:
            # The preserved positive list and explicit values must agree. Never
            # guess False from the absence of a key in a positive-only list.
            valid = payload.get("labels") == [key for key in SOCIAL_LABELS if values[key]]
        state = "bound" if valid else "invalid"
        accepted = payload if valid else None
    result = {
        "scheme": SCHEME, "status": STATUS, "validation_state": state,
        "note": NOTE, "values": [], "explanations": [],
        "provenance": {
            "record_id": row.get("record_id"), "version_id": row.get("version_id"),
            "body_hash": row["body_hash"] if matched else "",
        },
    }
    for item in social_label_metadata():
        key = item["key"]
        value = accepted["values"][key] if accepted is not None else None
        result["values"].append({
            **item,
            "state": "unknown" if value is None else ("source_true" if value else "source_false"),
            "value": value,
        })
    if accepted is not None:
        result["provenance"].update(
            source_sha256=accepted["source_sha256"], source_row=accepted["source_row"],
        )
        explanations = accepted.get("explanations")
        storage = accepted.get("explanation_storage")
        escaped_fields = (
            storage.get("escaped_fields", [])
            if isinstance(storage, dict) and storage.get("scheme") == "json-string-nul-v1"
            else []
        )
        if isinstance(explanations, dict):
            for key, label in _EXPLANATIONS.items():
                text = explanations.get(key)
                if isinstance(text, str) and text.strip():
                    result["explanations"].append({
                        "key": key, "label": label, "text": text[:2000],
                        "truncated": len(text) > 2000,
                        **({"storage_note": "Original U+0000 characters are displayed as literal \\u0000. Exact original strings are preserved in the source data."}
                           if isinstance(escaped_fields, list) and key in escaped_fields else {}),
                    })
    return result
