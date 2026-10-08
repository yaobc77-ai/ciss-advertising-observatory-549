"""Reviewed client-question membership, separate from model drafts and CLAIMS2.

Approval records are named human assertions, not identity authentication. Preparing
this immutable artifact does not configure a server, publish a site, or create gold.
All character positions are Python Unicode offsets into the frozen original body.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from .content_assessment import (
    QUESTION_IDS,
    _validate_definitions,
    build_article_request,
)
from .evaluate import EvaluationInvalid, strict_json_loads

REVIEW_FORMAT = "content-membership-review-v1"
PUBLICATION_FORMAT = "content-membership-publication-v1"
MAX_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 20_000
MAX_BODY = 2_000_000
Sha = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[StrictStr, Field(min_length=1, max_length=250)]
ShortText = Annotated[StrictStr, Field(max_length=4000)]
Range = Annotated[list[StrictInt], Field(min_length=2, max_length=2)]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_digest(value) -> str:
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(data):
    try:
        return strict_json_loads(data)
    except EvaluationInvalid as exc:
        raise ValueError("invalid_publication_or_review_json") from exc


def _timestamp(value: str) -> str:
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("timezone_timestamp_required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timezone_timestamp_required") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone_timestamp_required")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Approval(StrictModel):
    status: Literal["pending", "approved"]
    reviewer: Annotated[StrictStr, Field(max_length=200)]
    reviewed_at: StrictStr | None
    approval_record: ShortText

    @model_validator(mode="after")
    def assertion(self):
        if self.status == "approved":
            if not self.reviewer.strip() or not self.approval_record.strip():
                raise ValueError("named_approval_assertion_required")
            _timestamp(self.reviewed_at)
        elif self.reviewer or self.reviewed_at is not None or self.approval_record:
            raise ValueError("pending_cannot_claim_approval")
        return self


class Evidence(StrictModel):
    start: Annotated[StrictInt, Field(ge=0, le=MAX_BODY)]
    end: Annotated[StrictInt, Field(ge=1, le=MAX_BODY)]
    quote: Annotated[StrictStr, Field(min_length=1, max_length=8000)]

    @model_validator(mode="after")
    def bounds(self):
        if self.start >= self.end or not self.quote.strip():
            raise ValueError("nonempty_evidence_required")
        return self


EvidenceList = Annotated[list[Evidence], Field(max_length=100)]


class ReviewedSlot(StrictModel):
    question_id: Literal[*QUESTION_IDS]
    record_id: Identifier
    version_id: Sha
    body_sha256: Sha
    request_sha256: Sha
    definitions_sha256: Sha
    final_membership: Literal["relevant", "not_relevant", "unknown", "pending"]
    original_source_complete: StrictBool
    full_text_reviewed: StrictBool
    support_evidence: EvidenceList
    limiting_evidence: EvidenceList
    consensus_evidence: EvidenceList
    speaker: ShortText
    treatment: Literal["mention", "reported", "supported", "challenged", "mixed",
                       "not_applicable", "uncertain"]
    temporal_status: Literal["achieved", "planned", "mixed", "not_applicable", "uncertain"]
    rationale: ShortText
    consensus_position: Literal["consistent_with_consensus", "skepticism_of_consensus",
                                "mixed", "uncertain", "not_applicable"]
    review: Approval

    @model_validator(mode="after")
    def judgment(self):
        if not self.speaker.strip() or not self.rationale.strip():
            raise ValueError("speaker_and_review_rationale_required")
        if self.final_membership == "pending":
            if (self.review.status != "pending" or self.original_source_complete
                    or self.full_text_reviewed or self.support_evidence
                    or self.limiting_evidence or self.consensus_evidence):
                raise ValueError("pending_slot_cannot_claim_review_or_evidence")
        elif self.review.status != "approved":
            raise ValueError("final_membership_requires_named_review")
        if self.final_membership == "relevant" and not self.support_evidence:
            raise ValueError("relevant_requires_original_support")
        if self.final_membership == "not_relevant":
            if not self.original_source_complete or not self.full_text_reviewed:
                raise ValueError("negative_requires_complete_original_and_full_text_review")
            if self.support_evidence:
                raise ValueError("negative_cannot_have_positive_support")
        if self.question_id != "Q15" or self.final_membership == "not_relevant":
            if self.consensus_position != "not_applicable" or self.consensus_evidence:
                raise ValueError("consensus_applies_only_to_nonnegative_Q15")
        elif self.consensus_position == "not_applicable":
            raise ValueError("Q15_consensus_must_remain_uncertain_or_supported")
        elif self.consensus_position != "uncertain" and not self.consensus_evidence:
            raise ValueError("specific_consensus_requires_original_evidence")
        for evidence in (self.support_evidence, self.limiting_evidence, self.consensus_evidence):
            ids = [(e.start, e.end, e.quote) for e in evidence]
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate_evidence")
        if {(e.start, e.end) for e in self.support_evidence} & {
            (e.start, e.end) for e in self.limiting_evidence
        }:
            raise ValueError("same_span_cannot_support_and_limit")
        return self


class ReviewDocument(StrictModel):
    format_version: Literal[REVIEW_FORMAT]
    created_at: StrictStr
    packet_manifest_sha256: Sha
    definitions_sha256: Sha
    scope_sha256: Sha
    max_input_tokens: Annotated[StrictInt, Field(ge=1, le=1_000_000)]
    definition_review: Approval
    publication_review: Approval
    slots: Annotated[list[ReviewedSlot], Field(min_length=24, max_length=MAX_RECORDS * 24)]

    @model_validator(mode="after")
    def timestamp(self):
        _timestamp(self.created_at)
        return self


class FrozenSource(StrictModel):
    record_id: Identifier
    dataset: Literal["native"]
    version_id: Sha
    payload_sha256: Sha
    body_sha256: Sha
    body: Annotated[StrictStr, Field(max_length=MAX_BODY)]
    body_length: Annotated[StrictInt, Field(ge=0, le=MAX_BODY)]
    retrievable: StrictBool
    retrieval_ranges: Annotated[list[Range], Field(max_length=20_000)]
    request_sha256: Sha
    request_status: Literal["ready", "not_requested"]
    request_reason: ShortText | None

    @model_validator(mode="after")
    def identity(self):
        if (len(self.body) != self.body_length or _sha(self.body.encode("utf-8")) != self.body_sha256
                or self.payload_sha256 != self.version_id):
            raise ValueError("frozen_source_identity_differs")
        previous = 0
        for left, right in self.retrieval_ranges:
            if not 0 <= left < right <= self.body_length or left < previous:
                raise ValueError("frozen_source_ranges_invalid")
            previous = right
        if self.request_status == "ready" and self.request_reason is not None:
            raise ValueError("ready_request_cannot_have_skip_reason")
        if self.request_status == "not_requested" and not self.request_reason:
            raise ValueError("skipped_request_requires_reason")
        return self


class PublicQuestion(StrictModel):
    question_id: Literal[*QUESTION_IDS]
    question_exact: Annotated[StrictStr, Field(min_length=1, max_length=4000)]
    definition: Annotated[StrictStr, Field(min_length=1, max_length=12_000)]
    source_ref: Annotated[StrictStr, Field(min_length=1, max_length=4000)]


def _check_slots(sources, slots, definitions_sha256):
    identities = set()
    for slot in slots:
        key = (slot.record_id, slot.question_id)
        if key in identities or slot.record_id not in sources:
            raise ValueError("duplicate_or_out_of_scope_slot")
        identities.add(key)
        source = sources[slot.record_id]
        for field in ("version_id", "body_sha256", "request_sha256"):
            if getattr(slot, field) != getattr(source, field):
                raise ValueError("slot_source_or_request_binding_differs")
        if slot.definitions_sha256 != definitions_sha256:
            raise ValueError("slot_definition_binding_differs")
        for evidence in slot.support_evidence + slot.limiting_evidence + slot.consensus_evidence:
            if (not source.retrievable or source.body[evidence.start:evidence.end] != evidence.quote
                    or not any(left <= evidence.start < evidence.end <= right
                               for left, right in source.retrieval_ranges)):
                raise ValueError("evidence_must_match_allowed_original_source")
        if slot.final_membership == "not_relevant" and (
            not source.retrievable or not source.body.strip()
            or source.request_reason in {"body_error", "body_unavailable_or_placeholder",
                                         "body_error_placeholder", "no_locatable_passages"}
        ):
            raise ValueError("missing_or_placeholder_source_cannot_be_negative")
    if identities != {(rid, qid) for rid in sources for qid in QUESTION_IDS}:
        raise ValueError("complete_record_times_24_scope_required")


class Publication(StrictModel):
    format_version: Literal[PUBLICATION_FORMAT]
    created_at: StrictStr
    review_sha256: Sha
    review_json_utf8: Annotated[StrictStr, Field(min_length=1, max_length=MAX_BYTES)]
    packet_manifest_sha256: Sha
    definitions_sha256: Sha
    scope_sha256: Sha
    max_input_tokens: Annotated[StrictInt, Field(ge=1, le=1_000_000)]
    definitions_document: dict
    definition_review: Approval
    publication_review: Approval
    questions: Annotated[list[PublicQuestion], Field(min_length=24, max_length=24)]
    sources: Annotated[list[FrozenSource], Field(min_length=1, max_length=MAX_RECORDS)]
    slots: Annotated[list[ReviewedSlot], Field(min_length=24, max_length=MAX_RECORDS * 24)]
    status: Literal["partial", "complete"]
    reviewed_units: Annotated[StrictInt, Field(ge=1, le=MAX_RECORDS * 24)]
    classification_complete: StrictBool
    semantic_gold: StrictBool
    quality_scores: None

    @model_validator(mode="after")
    def bindings(self):
        _timestamp(self.created_at)
        if self.semantic_gold is not False:
            raise ValueError("membership_publication_is_not_evaluation_gold")
        review_bytes = self.review_json_utf8.encode("utf-8")
        if len(review_bytes) > MAX_BYTES or _sha(review_bytes) != self.review_sha256:
            raise ValueError("frozen_review_bytes_sha256_differs")
        review = ReviewDocument.model_validate(_parse(review_bytes))
        for field in ("packet_manifest_sha256", "definitions_sha256", "scope_sha256", "max_input_tokens"):
            if getattr(self, field) != getattr(review, field):
                raise ValueError("published_review_metadata_differs")
        if (self.definition_review != review.definition_review or self.publication_review != review.publication_review
                or self.slots != review.slots):
            raise ValueError("published_slots_or_approval_differs_from_review_bytes")
        if self.definition_review.status != "approved" or self.publication_review.status != "approved":
            raise ValueError("definition_and_publication_approval_assertions_required")
        definitions = _validate_definitions(self.definitions_document)
        if _canonical_digest(self.definitions_document) != self.definitions_sha256:
            raise ValueError("approved_definition_binding_differs")
        expected_questions = [{"question_id": q["question_id"], "question_exact": q["question_exact"],
                               "definition": q["definition_draft"], "source_ref": q["source_ref"]}
                              for q in sorted(definitions, key=lambda q: q["question_id"])]
        if [q.model_dump() for q in self.questions] != expected_questions:
            raise ValueError("published_questions_or_definitions_differ")
        sources = {s.record_id: s for s in self.sources}
        if len(sources) != len(self.sources) or list(sources) != sorted(sources):
            raise ValueError("unique_sorted_frozen_sources_required")
        scope = [{"record_id": s.record_id, "version_id": s.version_id, "body_sha256": s.body_sha256}
                 for s in self.sources]
        if _canonical_digest(scope) != self.scope_sha256:
            raise ValueError("frozen_scope_binding_differs")
        _check_slots(sources, self.slots, self.definitions_sha256)
        reviewed = sum(s.final_membership != "pending" for s in self.slots)
        complete = all(s.final_membership in {"relevant", "not_relevant"} for s in self.slots)
        if (self.reviewed_units != reviewed or reviewed < 1 or self.classification_complete != complete
                or self.status != ("complete" if complete else "partial")):
            raise ValueError("derived_publication_coverage_differs")
        return self


def _requests(source: dict, max_input_tokens: int):
    """Rebuild each frozen request; source is the existing validated packet result."""
    if type(max_input_tokens) is not int or not 1 <= max_input_tokens <= 1_000_000:
        raise ValueError("bounded_input_token_budget_required")
    if not isinstance(source, dict) or not isinstance(source.get("selected"), list):
        raise ValueError("validated_content_packet_required")
    selected = sorted(source["selected"], key=lambda r: r["record_id"])
    if not 1 <= len(selected) <= MAX_RECORDS or len({r["record_id"] for r in selected}) != len(selected):
        raise ValueError("unique_nonempty_source_scope_required")
    if any(not isinstance(r.get("body"), str) or len(r["body"]) > MAX_BODY for r in selected):
        raise ValueError("bounded_frozen_source_body_required")
    requests = [build_article_request(r, source["definitions"], max_input_tokens=max_input_tokens)
                for r in selected]
    scope = _canonical_digest([{k: r[k] for k in ("record_id", "version_id", "body_sha256")}
                              for r in selected])
    if source["summary"]["selection_sha256"] != scope:
        raise ValueError("content_packet_scope_binding_differs")
    return requests, scope


def _pending() -> dict:
    return {"status": "pending", "reviewer": "", "reviewed_at": None, "approval_record": ""}


def make_review_template(source: dict, *, max_input_tokens=24000) -> dict:
    """Retain every record × 24 as pending; do not import AI not_found as negative."""
    requests, scope = _requests(source, max_input_tokens)
    slots = []
    for request in requests:
        binding = {k: request[k] for k in ("record_id", "version_id", "body_sha256",
                                           "request_sha256", "definitions_sha256")}
        for qid in QUESTION_IDS:
            slots.append({**binding, "question_id": qid, "final_membership": "pending",
                          "original_source_complete": False, "full_text_reviewed": False,
                          "support_evidence": [], "limiting_evidence": [], "consensus_evidence": [],
                          "speaker": "unknown", "treatment": "uncertain", "temporal_status": "uncertain",
                          "rationale": "Awaiting named full-text review; no article judgment is approved.",
                          "consensus_position": "uncertain" if qid == "Q15" else "not_applicable",
                          "review": _pending()})
    value = {"format_version": REVIEW_FORMAT, "created_at": _now(),
             "packet_manifest_sha256": source["manifest_sha256"],
             "definitions_sha256": _canonical_digest(source["definitions"]), "scope_sha256": scope,
             "max_input_tokens": max_input_tokens, "definition_review": _pending(),
             "publication_review": _pending(), "slots": slots}
    return ReviewDocument.model_validate(value).model_dump()


def _frozen_sources(requests):
    sources = []
    for request in requests:
        record = request["record"]
        sources.append({"record_id": record["record_id"], "dataset": "native",
                        "version_id": record["version_id"], "payload_sha256": record["payload_sha256"],
                        "body_sha256": record["body_sha256"], "body": record["body"],
                        "body_length": len(record["body"]), "retrievable": record["payload"]["retrievable"],
                        "retrieval_ranges": record["retrieval_ranges"],
                        "request_sha256": request["request_sha256"],
                        "request_status": request["status"], "request_reason": request["reason"]})
    return sources


def validate_review(source: dict, review: dict) -> dict:
    """Validate bindings and final assertions without manufacturing any approval."""
    reviewed = ReviewDocument.model_validate(review)
    requests, scope = _requests(source, reviewed.max_input_tokens)
    expected = {"packet_manifest_sha256": source["manifest_sha256"], "scope_sha256": scope,
                "definitions_sha256": _canonical_digest(source["definitions"])}
    if any(getattr(reviewed, k) != value for k, value in expected.items()):
        raise ValueError("review_does_not_bind_original_packet")
    sources = [FrozenSource.model_validate(row) for row in _frozen_sources(requests)]
    _check_slots({s.record_id: s for s in sources}, reviewed.slots, reviewed.definitions_sha256)
    counts = Counter(s.final_membership for s in reviewed.slots)
    return {"status": "valid_review_material", "selected_records": len(sources),
            "review_units": len(sources) * 24, "reviewed_units": len(sources) * 24 - counts["pending"],
            "membership_counts": dict(counts), "definition_approved": reviewed.definition_review.status == "approved",
            "publication_approved": reviewed.publication_review.status == "approved",
            "publication_ready": reviewed.definition_review.status == reviewed.publication_review.status == "approved"
                                 and counts["pending"] < len(sources) * 24,
            "semantic_gold": False, "quality_scores": None, "model_calls": 0, "database_writes": 0}


def build_publication(source: dict, review: dict, *, review_sha256: str, review_bytes: bytes | None = None) -> dict:
    """Validate a separate named review against the original complete packet scope."""
    validate_review(source, review)
    if review_bytes is None:
        review_bytes = json.dumps(review, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                  allow_nan=False).encode("utf-8")
    if (not isinstance(review_bytes, bytes) or len(review_bytes) > MAX_BYTES
            or _sha(review_bytes) != review_sha256 or _parse(review_bytes) != review):
        raise ValueError("original_review_bytes_and_sha256_required")
    reviewed = ReviewDocument.model_validate(review)
    requests, scope = _requests(source, reviewed.max_input_tokens)
    expected = {"packet_manifest_sha256": source["manifest_sha256"], "scope_sha256": scope,
                "definitions_sha256": _canonical_digest(source["definitions"])}
    sources = _frozen_sources(requests)
    slots = [s.model_dump() for s in reviewed.slots]
    complete = all(s["final_membership"] in {"relevant", "not_relevant"} for s in slots)
    questions = [{"question_id": q["question_id"], "question_exact": q["question_exact"],
                  "definition": q["definition_draft"], "source_ref": q["source_ref"]}
                 for q in sorted(_validate_definitions(source["definitions"]), key=lambda q: q["question_id"])]
    value = {"format_version": PUBLICATION_FORMAT, "created_at": _now(), "review_sha256": review_sha256,
             "review_json_utf8": review_bytes.decode("utf-8"),
             **expected, "max_input_tokens": reviewed.max_input_tokens,
             "definitions_document": deepcopy(source["definitions"]),
             "definition_review": reviewed.definition_review.model_dump(),
             "publication_review": reviewed.publication_review.model_dump(),
             "questions": questions, "sources": sources, "slots": slots,
             "status": "complete" if complete else "partial", "classification_complete": complete,
             "reviewed_units": sum(s["final_membership"] != "pending" for s in slots),
             "semantic_gold": False, "quality_scores": None}
    return Publication.model_validate(value).model_dump()


def load_publication(path, expected_sha256: str, expected_review_sha256: str) -> dict:
    """Load only a server-selected bounded artifact pinned to publication and review."""
    for value in (expected_sha256, expected_review_sha256):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("publication_and_review_sha256_required")
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES or _sha(data) != expected_sha256:
        raise ValueError("publication_size_or_sha256_differs")
    value = Publication.model_validate(_parse(data)).model_dump()
    if value["review_sha256"] != expected_review_sha256:
        raise ValueError("publication_review_sha256_differs")
    value["_verified_canonical_sha256"] = _canonical_digest(value)
    value["_verified_sha256"] = expected_sha256
    return value


def _validated_publication(publication):
    if not isinstance(publication, dict):
        raise ValueError("validated_publication_required")
    value = {k: v for k, v in publication.items() if k not in {"_verified_sha256", "_verified_canonical_sha256"}}
    canonical = _canonical_digest(value)
    if "_verified_sha256" in publication:
        if (not re.fullmatch(r"[0-9a-f]{64}", str(publication["_verified_sha256"]))
                or publication.get("_verified_canonical_sha256") != canonical):
            raise ValueError("verified_publication_mutated")
    validated = Publication.model_validate(value).model_dump()
    return validated, publication.get("_verified_sha256", canonical)


def _current_matches(row, frozen):
    """Bad current convenience hashes/ranges fail closed even with unchanged version."""
    if not isinstance(row, dict) or row.get("dataset") != "native" or not isinstance(row.get("body"), str):
        return False
    body = row["body"]
    if (len(body) > MAX_BODY or row.get("record_id") != frozen["record_id"]
            or row.get("version_id") != frozen["version_id"]
            or _sha(body.encode("utf-8")) != row.get("body_hash")
            or row.get("body_hash") != frozen["body_sha256"]
            or type(row.get("retrievable")) is not bool or row["retrievable"] != frozen["retrievable"]):
        return False
    ranges = row.get("retrieval_ranges")
    if ranges is None:
        end = row.get("retrieval_end")
        end = len(body) if end is None else end
        if type(end) is not int or not 0 <= end <= len(body):
            return False
        ranges = [[0, end]] if end else []
    if not isinstance(ranges, (list, tuple)):
        return False
    normalized, previous = [], 0
    for pair in ranges:
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or any(type(n) is not int for n in pair)):
            return False
        left, right = pair
        if not 0 <= left < right <= len(body) or left < previous:
            return False
        previous = right
        normalized.append([left, right])
    return normalized == frozen["retrieval_ranges"]


def _safe_metadata(row, show_source_links):
    metadata = row.get("publicmetadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    safe = {"record_id": row["record_id"], "dataset": row.get("dataset")}
    for key in ("title", "publisher", "sponsor", "date", "source_date", "effective_date", "date_basis",
                "inferred_date", "inferred_date_tier", "inferred_tier", "keyword", "platform", "account", "sponsor_basis"):
        value = metadata.get(key)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        if value is None or isinstance(value, str) and len(value) <= 4000:
            safe[key] = value
    if show_source_links:
        for key in ("url", "archive_url"):
            value = metadata.get(key)
            if isinstance(value, str) and len(value) <= 4000 and not any(c.isspace() for c in value):
                try:
                    parts = urlsplit(value)
                    if parts.scheme in {"http", "https"} and parts.netloc and not parts.username and not parts.password:
                        safe[key] = value
                except ValueError:
                    pass
    return safe


def read_match_page(publication, current_source_rows, question_id, offset=0, limit=20,
                    show_source_links=True) -> dict:
    """Read complete current selection coverage and a page of reviewed positives.

    No source lookup, model call or writes. A missing/stale/pending/insufficient
    record contributes unknown, never negative. Current source rows must be the
    full trusted filter selection, not a search result page or retrieval shortlist.
    """
    if question_id not in QUESTION_IDS:
        raise ValueError("client_question_id_required")
    if (type(offset) is not int or offset < 0 or offset > MAX_RECORDS
            or type(limit) is not int or not 1 <= limit <= 20 or type(show_source_links) is not bool):
        raise ValueError("bounded_page_and_link_parameters_required")
    value, publication_version = _validated_publication(publication)
    question = next(q for q in value["questions"] if q["question_id"] == question_id)
    if not isinstance(current_source_rows, list) or len(current_source_rows) > MAX_RECORDS:
        raise ValueError("bounded_complete_current_selection_required")
    current = {}
    for row in current_source_rows:
        if (not isinstance(row, dict) or not isinstance(row.get("record_id"), str)
                or not row["record_id"].strip() or row["record_id"] in current):
            raise ValueError("unique_current_source_ids_required")
        current[row["record_id"]] = row
    frozen = {s["record_id"]: s for s in value["sources"]}
    slots = {s["record_id"]: s for s in value["slots"] if s["question_id"] == question_id}
    unknown, positive, negatives, reviewed, stale = Counter(), [], 0, 0, 0
    for rid in sorted(current):
        row = current[rid]
        if rid not in frozen:
            unknown["new"] += 1
            continue
        if not _current_matches(row, frozen[rid]):
            unknown["stale"] += 1
            stale += 1
            continue
        slot = slots[rid]
        state = slot["final_membership"]
        if state == "pending":
            unknown["pending"] += 1
            continue
        reviewed += 1
        if state == "unknown":
            unknown["insufficient"] += 1
        elif state == "not_relevant":
            negatives += 1
        else:
            positive.append((row, slot))
    records, refs = [], []
    for row, slot in positive[offset:offset + limit]:
        binding = {"record_id": row["record_id"], "version_id": row["version_id"],
                   "body_sha256": row["body_hash"], "dataset": "native",
                   "question_id": question_id, "publication_version": publication_version,
                   "review_version": value["review_sha256"], "definitions_sha256": value["definitions_sha256"],
                   "source_ref": question["source_ref"]}
        record = {**_safe_metadata(row, show_source_links), **binding, "membership": "relevant"}
        for key in ("speaker", "treatment", "temporal_status", "rationale", "consensus_position",
                    "original_source_complete", "full_text_reviewed"):
            record[key] = slot[key]
        for key in ("support_evidence", "limiting_evidence", "consensus_evidence"):
            record[key] = [{**binding, **e} for e in slot[key]]
            refs.extend({**e, "evidence_role": key} for e in record[key])
        records.append(record)
    end = min(offset + len(records), len(positive))
    classification_complete = not sum(unknown.values())
    scope_matches = set(current) == set(frozen) and stale == 0
    return {"status": "ok", "available": True, "publication_version": publication_version,
            "review_version": value["review_sha256"], "definitions_sha256": value["definitions_sha256"],
            "question": deepcopy(question),
            "totals": {"scope_records": len(current), "relevant": len(positive),
                       "not_relevant": negatives, "unknown": sum(unknown.values())},
            "coverage": {"classification_complete": classification_complete,
                         "scope_matches_frozen": scope_matches, "reviewed_records": reviewed,
                         "unknown_reasons": {key: unknown[key] for key in ("new", "stale", "pending", "insufficient")},
                         "frozen_scope_records": len(frozen),
                         "current_only_records": len(set(current) - set(frozen)),
                         "frozen_not_in_selection": len(set(frozen) - set(current)),
                         "publication_status": value["status"]},
            "records": records, "offset": offset, "limit": limit, "shown": len(records),
            "page_complete": offset == 0 and len(records) == len(positive),
            "next_offset": end if end < len(positive) else None, "source_refs": refs,
            "page_note": "This page contains all matches only when page_complete is true. "
                         "No next page does not establish classification completeness. "
                         "Unknown records are never negative; complete means the current selection only.",
            "meaning": "Reviewed occurrence and article treatment, not an external factual verdict. "
                       "Named review assertions are not authenticated identity or evaluation scores."}
