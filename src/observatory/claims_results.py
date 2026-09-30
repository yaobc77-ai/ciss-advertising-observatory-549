"""Decode saved upstream CLAIMS responses without approving their classifications.

The raw content is data, not instructions. Category/snippet pairs come from the
same indexed response, never from the lossy flattened SQLite snippet list.
Decoding is not source association, citation validation, or semantic review.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .claims_taxonomy import TaxonomyBundle

_ACTION_FIELDS = {
    "match_existing_category": "matched_categories",
    "create_new_category": "new_categories",
    "update_existing_category": "updated_categories",
}
_CATEGORY_FIELDS = tuple(_ACTION_FIELDS.values())
_META_ACTIONS = frozenset({
    "final_count", "Note", "summary", "confirmation_of_completeness",
    "final_statement", "note", "confirmation", "confirm_completeness",
    "confirmation_statement", "confirm_extraction", "confirmation_summary",
    "final", "confirm_extraction_count", "confirmation_complete",
    "final_comment", "final_confirmation", "confirm_extraction_complete",
    "confirm", "final_note", "none", "final_summary",
})
_META_KEYS = frozenset({
    "confirmation", "note", "count_claims", "final_statement", "final_note",
    "content", "summary", "confirmation_note", "total_claims_extracted", "total_claims",
})
_FENCE = re.compile(
    r"(?:\A|\n)```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```(?=\Z|\r?\n)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SavedResultIssue:
    code: str
    pointer: str
    severity: str = "review"


@dataclass(frozen=True)
class SavedClaimCandidate:
    response_index: int
    category_index: int
    action_type: str
    category_field: str
    nc_id: str
    sc_id: str | None
    mapping_state: str
    historical_inline_definition: str | None
    current_candidate_definition: str | None
    definition_drift: bool
    raw_response_superclaim_id: str | None
    historical_superclaim_definition: str | None
    current_candidate_superclaim_definition: str | None
    raw_response_current_superclaim_definition: str | None
    superclaim_definition_drift: bool
    source_snippet: str | None
    rationale: str | None
    raw_json_pointer: str
    report_state: str
    errors: tuple[SavedResultIssue, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SavedResponseState:
    response_index: int
    action_type: str | None
    state: str
    candidate_count: int


@dataclass(frozen=True)
class SavedClaimsResult:
    original_id: str | None
    recorded_model: str | None
    timestamp: str | None
    raw_response_sha256: str | None
    input_text_sha256: str | None
    taxonomy_bundle_fingerprint: str
    report_state: str
    claims: tuple[SavedClaimCandidate, ...]
    response_states: tuple[SavedResponseState, ...]
    errors: tuple[SavedResultIssue, ...]
    method: str = "saved_upstream_llm_response"
    raw_content_pointer: str = "/choices/0/message/content"
    review_required: bool = True
    association_is_approved: bool = False
    semantics_is_approved: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-safe data; claim pointers address the decoded content JSON."""
        return asdict(self)


class _MalformedResponse(ValueError):
    pass


def _json_object(text: str) -> dict[str, Any]:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise _MalformedResponse("duplicate_json_key")
            result[key] = value
        return result

    def constant(_value):
        raise _MalformedResponse("nonstandard_json_constant")

    def check(value):
        if isinstance(value, str):
            value.encode("utf-8")
        elif isinstance(value, dict):
            for key, item in value.items():
                check(key)
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
        elif isinstance(value, float) and not math.isfinite(value):
            raise _MalformedResponse("nonfinite_json_number")

    try:
        obj = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(obj, dict):
            raise _MalformedResponse("json_object_required")
        check(obj)
        return obj
    except _MalformedResponse:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _MalformedResponse("invalid_json") from exc


def _identifier(value: str, prefix: str) -> tuple[str, str | None] | None:
    """Accept exact IDs or upstream's paired inline definition syntax only."""
    pattern = rf"{prefix}_[1-9][0-9]*"
    if re.fullmatch(pattern, value):
        return value, None
    match = re.fullmatch(rf"<({pattern})>([\s\S]+)<\1>", value)
    if match and match[2].strip():
        return match[1], match[2]
    return None


def _candidate(response, response_index, field, category_index, value, taxonomy):
    pointer = f"/responses/{response_index}/{field}/{category_index}"
    parsed = _identifier(value, "NC")
    if parsed is None:
        return None, [SavedResultIssue("invalid_subclaim_identifier", pointer)]
    nc_id, historical_definition = parsed
    definition = taxonomy.subclaims.get(nc_id)
    raw_sc_id = taxonomy.raw_claim_superclaim_map.get(nc_id)
    errors = []
    if definition is None:
        mapping_state = "dangling_subclaim" if raw_sc_id else "unknown_subclaim"
        errors.append(SavedResultIssue("unknown_subclaim", pointer))
    elif raw_sc_id is None:
        mapping_state = "unmapped"
        errors.append(SavedResultIssue("unmapped_subclaim", pointer))
    elif raw_sc_id not in taxonomy.superclaims:
        mapping_state = "dangling_superclaim"
        errors.append(SavedResultIssue("dangling_superclaim", pointer))
    else:
        mapping_state = "mapped"
    definition_drift = historical_definition is not None and historical_definition != definition
    if definition_drift:
        errors.append(SavedResultIssue("subclaim_definition_drift", pointer))

    response_pointer = f"/responses/{response_index}"
    superclaim = response.get("super_claim")
    parsed_sc = _identifier(superclaim, "SC") if isinstance(superclaim, str) else None
    response_sc_id, historical_sc_definition = parsed_sc if parsed_sc else (None, None)
    if parsed_sc is None:
        errors.append(SavedResultIssue("invalid_response_superclaim", response_pointer + "/super_claim"))
    elif response_sc_id not in taxonomy.superclaims:
        errors.append(SavedResultIssue("unknown_response_superclaim", response_pointer + "/super_claim"))
    if response_sc_id is not None and raw_sc_id is not None and response_sc_id != raw_sc_id:
        errors.append(SavedResultIssue("response_superclaim_mapping_conflict", response_pointer + "/super_claim"))
    response_current_sc_definition = taxonomy.superclaims.get(response_sc_id)
    sc_drift = historical_sc_definition is not None and historical_sc_definition != response_current_sc_definition
    if sc_drift:
        errors.append(SavedResultIssue("superclaim_definition_drift", response_pointer + "/super_claim"))
    snippet = response.get("source_snippet")
    if not isinstance(snippet, str) or not snippet.strip():
        errors.append(SavedResultIssue("missing_source_snippet", response_pointer + "/source_snippet"))
        snippet = None
    rationale = response.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        errors.append(SavedResultIssue("missing_rationale", response_pointer + "/rationale"))
        rationale = None
    candidate = SavedClaimCandidate(
        response_index, category_index, response["action_type"], field, nc_id, raw_sc_id,
        mapping_state, historical_definition, definition, definition_drift, response_sc_id,
        historical_sc_definition, taxonomy.superclaims.get(raw_sc_id),
        response_current_sc_definition, sc_drift, snippet, rationale, pointer,
        "needs_review" if errors else "candidate", tuple(errors),
    )
    return candidate, errors


def parse_saved_result(row: dict, taxonomy: TaxonomyBundle) -> SavedClaimsResult:
    """Decode one saved row. Nothing returned is approved, calibrated, or negative.

    Malformed envelopes, refusal, and incomplete generation quarantine the row.
    Unknown actions, no-label results, mapping errors and drift need review.
    ``raw_json_pointer`` addresses JSON decoded from ``raw_content_pointer``.
    Source identity and literal quotation checks remain the caller's task.
    """
    if not isinstance(row, dict) or not isinstance(taxonomy, TaxonomyBundle):
        raise TypeError("Expected a saved row dictionary and TaxonomyBundle")
    original_id = row.get("original_id")
    raw_text = row.get("raw_response")
    input_text = row.get("text")
    model = timestamp = raw_hash = input_hash = None
    errors = []

    def result(state, claims=(), responses=()):
        return SavedClaimsResult(
            original_id if isinstance(original_id, str) else None, model, timestamp,
            raw_hash, input_hash, taxonomy.bundle_fingerprint, state,
            tuple(claims), tuple(responses), tuple(errors),
        )

    if not isinstance(original_id, str) or not original_id.strip():
        errors.append(SavedResultIssue("invalid_original_id", "/original_id", "quarantine"))
    if not isinstance(input_text, str) or not input_text.strip():
        errors.append(SavedResultIssue("invalid_saved_input_text", "/text", "quarantine"))
    if not isinstance(raw_text, str) or not raw_text.strip():
        errors.append(SavedResultIssue("missing_raw_response", "/raw_response", "quarantine"))
    try:
        if isinstance(input_text, str):
            input_hash = hashlib.sha256(input_text.encode("utf-8")).hexdigest()
        if isinstance(raw_text, str):
            raw_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    except UnicodeError:
        errors.append(SavedResultIssue("invalid_saved_unicode", "/", "quarantine"))
    if errors:
        return result("quarantined")
    try:
        raw = _json_object(raw_text)
        model = raw.get("model")
        if not isinstance(model, str) or not model.strip():
            model = None
            raise _MalformedResponse("missing_recorded_model")
        created = raw.get("created")
        if type(created) is not int or created < 0:
            raise _MalformedResponse("invalid_recorded_timestamp")
        try:
            timestamp = datetime.fromtimestamp(created, timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError) as exc:
            raise _MalformedResponse("invalid_recorded_timestamp") from exc
        choices = raw.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise _MalformedResponse("single_saved_choice_required")
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            raise _MalformedResponse("generation_not_complete")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise _MalformedResponse("invalid_saved_message")
        if message.get("refusal") is not None:
            raise _MalformedResponse("model_refusal")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise _MalformedResponse("missing_saved_content")
        content = content.strip()
        fences = list(_FENCE.finditer(content))
        if len(fences) > 1:
            raise _MalformedResponse("multiple_json_fences")
        if fences:
            fence = fences[0]
            if content[:fence.start()].strip() or content[fence.end():].strip():
                errors.append(SavedResultIssue("json_fence_surrounding_content", "/choices/0/message/content"))
            content = fence[1]
        elif content.startswith("```"):
            raise _MalformedResponse("invalid_json_fence")
        decoded = _json_object(content)
        responses = decoded.get("responses")
        if not isinstance(responses, list):
            raise _MalformedResponse("responses_list_required")
        for response in responses:
            if not isinstance(response, dict):
                raise _MalformedResponse("response_object_required")
            for field in _CATEGORY_FIELDS:
                if field in response and (
                    not isinstance(response[field], list)
                    or any(not isinstance(value, str) for value in response[field])
                ):
                    raise _MalformedResponse("category_string_list_required")
            action = response.get("action_type")
            if action is not None and not isinstance(action, str):
                raise _MalformedResponse("action_string_required")
            if action in _ACTION_FIELDS and _ACTION_FIELDS[action] not in response:
                raise _MalformedResponse("action_category_field_missing")
    except _MalformedResponse as exc:
        errors.append(SavedResultIssue(str(exc), "/raw_response", "quarantine"))
        return result("quarantined")

    candidates = []
    response_states = []
    for index, response in enumerate(responses):
        action = response.get("action_type")
        pointer = f"/responses/{index}"
        has_categories = any(response.get(field) for field in _CATEGORY_FIELDS)
        count_before = len(candidates)
        if action in _ACTION_FIELDS:
            field = _ACTION_FIELDS[action]
            for category_index, value in enumerate(response[field]):
                candidate, issues = _candidate(response, index, field, category_index, value, taxonomy)
                errors.extend(issues)
                if candidate is not None:
                    candidates.append(candidate)
            state = "candidate_claims" if len(candidates) > count_before else "no_claims_emitted"
            if state == "no_claims_emitted":
                errors.append(SavedResultIssue("no_claims_emitted", pointer))
        elif action == "no_relevant_claim":
            state = "contradictory_no_relevant_claim" if has_categories else "no_relevant_claim_unreviewed"
            if has_categories:
                errors.append(SavedResultIssue("no_relevant_claim_has_categories", pointer))
        elif action in _META_ACTIONS or (action is None and set(response) <= _META_KEYS and response):
            state = "meta_only"
            if has_categories:
                errors.append(SavedResultIssue("meta_response_has_categories", pointer))
        else:
            state = "unknown_action"
            errors.append(SavedResultIssue("unknown_action", pointer + "/action_type"))
        response_states.append(SavedResponseState(index, action, state, len(candidates) - count_before))
    if not candidates:
        errors.append(SavedResultIssue("no_reviewable_claim_candidates", "/responses"))
    state = "needs_review" if errors or not candidates else "candidate_results"
    return result(state, candidates, response_states)
