"""Single-call Responses transport for private full-text assessment drafts.

Clients and budgets are injected. This module never constructs a connection,
retries a call, approves classifications or publishes results. Call preparation
is fully offline; execution is for a separately authorized, capped runner.
"""

from __future__ import annotations

import inspect
import json
from copy import deepcopy
from decimal import Decimal

from .budget import LimitReached, price
from .content_assessment import materialize_assessment
from .evaluate import canonical_digest, strict_json_loads
from .research_agent import _field, _require_strict_objects, _usage

MODEL = "gpt-5.6-luna"
MAX_VERIFIED_INPUT = 272_000
FORMAT = "content-provider-call-v1"


def _serialize(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def build_provider_call(request, *, model=MODEL, max_output_tokens=6000):
    """Validate a frozen ready request and build a deterministic conservative call."""
    materialize_assessment(request, None)
    if request["status"] != "ready":
        raise ValueError("provider_requires_ready_request")
    if model != MODEL:
        raise ValueError("provider_model_has_no_verified_price")
    if type(max_output_tokens) is not int or not 1000 <= max_output_tokens <= 16000:
        raise ValueError("provider_output_limit_must_be_1000_to_16000")
    schema = deepcopy(request["response_schema"])
    _require_strict_objects(schema)
    kwargs = {
        "model": model,
        "input": [{"role": "system", "content": request["model_input"]["system"]},
                  {"role": "user", "content": _serialize(request["model_input"]["data"])}],
        "text": {"format": {"type": "json_schema", "name": "client_article_assessment_v1",
                             "schema": schema, "strict": True}},
        "max_output_tokens": max_output_tokens, "reasoning": {"effort": "none"},
        "store": False, "service_tier": "default", "truncation": "disabled",
    }
    upper = len(_serialize(kwargs).encode("utf-8")) + 4096
    if upper > MAX_VERIFIED_INPUT:
        raise ValueError("provider_verified_input_pricing_bound_exceeded")
    reserve = price(model, upper, max_output_tokens) * Decimal("1.25")
    return {"format": FORMAT, "request_sha256": request["request_sha256"], "model": model,
            "max_output_tokens": max_output_tokens, "kwargs": kwargs,
            "call_sha256": canonical_digest(kwargs), "upper_input_tokens": upper,
            "reserved_usd": str(reserve)}


def _base_envelope(request, call_spec):
    return {
        **{field: request[field] for field in ("request_sha256", "record_id", "version_id",
                                              "body_sha256", "payload_sha256", "definitions_sha256")},
        "status": "draft_ai_unreviewed", "review_required": True, "publication_status": "hold",
        "provider_response_private": None,
        "provider_call": {
            "reservation_id": None, "prompt_sha256": canonical_digest(call_spec["kwargs"]["input"]),
            "call_sha256": call_spec["call_sha256"],
            "schema_sha256": canonical_digest(request["response_schema"]),
            "requested_model": call_spec["model"], "provider_model": None,
            "response_id": None, "response_status": None, "usage": None,
            "cost_usd": None, "exposure_usd": "0", "reserved_usd": call_spec["reserved_usd"],
            "state": "none", "dispatched": False, "reason": None, "error_type": None,
        },
    }


def _unknown_result(envelope, request, reason, *, error_type=None):
    assessment = materialize_assessment(request, None)
    assessment.update(processing_status=reason, reason=reason)
    envelope.update(processing_status=reason, assessment=assessment)
    envelope["provider_call"].update(reason=reason, error_type=error_type)
    return envelope


def _safe_string(value):
    return value if isinstance(value, str) else None


def _private_response(response):
    """Retain the provider dump privately; do not echo transport exception text."""
    try:
        raw = deepcopy(response) if isinstance(response, dict) else response.model_dump(mode="json")
        _serialize(raw)
        return raw
    except Exception as exc:
        return {"raw_response_unavailable": True, "error_type": type(exc).__name__}


def _strict_usage(response):
    # The shared helper validates counts; avoid its `or 0` normalizing a false
    # Boolean into an apparently valid zero cached/write token count.
    details = _field(_field(response, "usage"), "input_tokens_details")
    for key in ("cached_tokens", "cache_write_tokens"):
        value = _field(details, key)
        if value is not None and type(value) is not int:
            raise ValueError("invalid_provider_detail_tokens")
    i, o, cached, writes, _ = _usage(response)
    return i, o, cached, writes, {
        "input_tokens": i, "output_tokens": o,
        "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": writes},
    }


class _OutputFailure(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _output_text(response):
    outputs = _field(response, "output")
    if not isinstance(outputs, list):
        raise _OutputFailure("provider_malformed_output")
    texts = []
    for item in outputs:
        if _field(item, "type") == "reasoning":
            continue
        if (_field(item, "type") != "message"
                or _field(item, "role", "assistant") != "assistant"):
            raise _OutputFailure("provider_malformed_output")
        content = _field(item, "content")
        if not isinstance(content, list):
            raise _OutputFailure("provider_malformed_output")
        for block in content:
            kind = _field(block, "type")
            if kind == "refusal":
                raise _OutputFailure("provider_refused")
            if kind != "output_text" or not isinstance(_field(block, "text"), str):
                raise _OutputFailure("provider_malformed_output")
            texts.append(_field(block, "text"))
    if len(texts) != 1 or not texts[0].strip():
        raise _OutputFailure("provider_malformed_output")
    return texts[0]


def _mark_uncertain(envelope, request, budget, reason, exc=None):
    audit = envelope["provider_call"]
    audit.update(state="uncertain", cost_usd=None, exposure_usd=audit["reserved_usd"])
    error_type = type(exc).__name__ if exc is not None else None
    try:
        budget.uncertain(audit["reservation_id"], error_type or reason)
    except Exception as tracking:
        audit["uncertain_tracking_error_type"] = type(tracking).__name__
    return _unknown_result(envelope, request, reason, error_type=error_type)


def execute_provider_call(request, call_spec, *, client, budget, visitor):
    """Dispatch once after reserve; preserve a 24-unknown draft for every failure.

    Invalid local call configuration raises before reserving. An unknown outcome
    never cancels its reservation, retries or fabricates a negative assessment.
    """
    if not isinstance(call_spec, dict):
        raise ValueError("provider_call_spec_required")
    expected = build_provider_call(request, model=call_spec.get("model"),
                                   max_output_tokens=call_spec.get("max_output_tokens"))
    if canonical_digest(call_spec) != canonical_digest(expected):
        raise ValueError("provider_call_binding_changed")
    if not isinstance(visitor, str) or not visitor.strip() or len(visitor) > 200:
        raise ValueError("provider_visitor_required")
    try:
        create = getattr(getattr(client, "responses", None), "create", None)
        retries = _field(client, "max_retries", 0)
        budget_methods = {method: getattr(budget, method, None)
                          for method in ("reserve", "settle", "uncertain")}
    except Exception:
        raise ValueError("provider_local_configuration_unavailable") from None
    if not callable(create) or inspect.iscoroutinefunction(create):
        raise ValueError("synchronous_provider_create_required")
    if type(retries) is not int or retries != 0:
        raise ValueError("provider_client_retries_must_be_disabled")
    if any(not callable(method) or inspect.iscoroutinefunction(method) for method in budget_methods.values()):
        raise ValueError("provider_budget_contract_required")
    request, call_spec = deepcopy(request), deepcopy(call_spec)
    envelope = _base_envelope(request, call_spec)
    audit = envelope["provider_call"]
    reserve = Decimal(call_spec["reserved_usd"])
    try:
        rid = budget.reserve(reserve, visitor, "generation", call_spec["model"])
    except LimitReached as exc:
        return _unknown_result(envelope, request, "budget_limited", error_type=type(exc).__name__)
    except Exception as exc:
        return _unknown_result(envelope, request, "budget_unavailable", error_type=type(exc).__name__)
    if not isinstance(rid, str) or not rid.strip():
        audit.update(state="uncertain", exposure_usd=str(reserve))
        return _unknown_result(envelope, request, "budget_unavailable", error_type="InvalidReservationId")
    audit.update(reservation_id=rid, state="pending", exposure_usd=str(reserve), dispatched=True)
    try:
        response = create(**deepcopy(call_spec["kwargs"]))
    except Exception as exc:
        return _mark_uncertain(envelope, request, budget, "provider_unavailable", exc)
    envelope["provider_response_private"] = _private_response(response)
    try:
        audit.update(response_id=_safe_string(_field(response, "id")),
                     response_status=_safe_string(_field(response, "status")),
                     provider_model=_safe_string(_field(response, "model")))
    except Exception as exc:
        return _mark_uncertain(envelope, request, budget, "provider_unavailable", exc)
    try:
        i, o, cached, writes, usage = _strict_usage(response)
        audit["usage"] = usage
    except Exception as exc:
        return _mark_uncertain(envelope, request, budget, "provider_usage_unavailable", exc)
    if audit["provider_model"] != MODEL:
        return _mark_uncertain(envelope, request, budget, "model_mismatch")
    if i > MAX_VERIFIED_INPUT:
        return _mark_uncertain(envelope, request, budget, "pricing_bound_exceeded")
    try:
        actual = price(call_spec["model"], i, o, cached, writes)
        budget.settle(rid, actual, {**usage, "observatory_request": {
            "stage": "content_assessment", "request_sha256": request["request_sha256"],
            "prompt_sha256": audit["prompt_sha256"], "call_sha256": audit["call_sha256"],
            "schema_sha256": audit["schema_sha256"], "provider_model": audit["provider_model"],
            "response_id": audit["response_id"],
        }})
    except Exception as exc:
        return _mark_uncertain(envelope, request, budget, "budget_settlement_unavailable", exc)
    audit.update(state="settled", cost_usd=str(actual), exposure_usd=str(actual))
    if i > call_spec["upper_input_tokens"] or o > call_spec["max_output_tokens"]:
        return _unknown_result(envelope, request, "accounting_bound_exceeded")
    if audit["response_status"] != "completed":
        return _unknown_result(envelope, request, "provider_incomplete")
    try:
        text = _output_text(response)
        parsed = strict_json_loads(text)
    except _OutputFailure as exc:
        return _unknown_result(envelope, request, exc.reason, error_type=type(exc).__name__)
    except Exception as exc:
        return _unknown_result(envelope, request, "provider_malformed_output", error_type=type(exc).__name__)
    try:
        assessment = materialize_assessment(request, parsed)
    except Exception as exc:
        return _unknown_result(envelope, request, "assessment_validation_failed", error_type=type(exc).__name__)
    envelope.update(processing_status="processed", assessment=assessment)
    audit["reason"] = "completed"
    return envelope
