"""Bind excerpts to one saved source observation, without adjudicating its text."""

from hashlib import sha256

from .models import Evidence
from .social_source_retrieval import source_version_id, validated_observations
from .source_text_quality import current_text_quality_codes

FIELDS = ("source_observation_id", "source_version_id", "source_body_hash")


def _digest(body):
    return sha256(body.encode("utf-8")).hexdigest()


def public_observations(payload):
    """Only validated, retrievable social sources reach public read adapters."""
    if not isinstance(payload, dict) or payload.get("retrievable") is not True:
        return []
    return validated_observations(payload)


def evidence_source(row, item):
    """Return the exact current source or raise for drift, forgery or ambiguity."""
    if type(item.start) is not int or type(item.end) is not int:
        raise ValueError("Source offsets must be exact integers")
    item = Evidence.model_validate(item.model_dump(mode="python"))
    if (row.get("dataset") != item.dataset or row.get("record_id") != item.record_id
            or row.get("version_id") != item.version_id or row.get("retrievable") is not True):
        raise ValueError("Evidence is outside the current retrievable record")
    if item.source_observation_id is None:
        body = row.get("body")
        if not isinstance(body, str) or _digest(body) != row.get("body_hash"):
            raise ValueError("Current canonical body hash does not match")
        source = {"body": body, "body_hash": row["body_hash"]}
    else:
        matches = [source for source in row.get("social_source_observations", [])
                   if isinstance(source, dict) and all(source.get(key) == getattr(item, key) for key in FIELDS)]
        if len(matches) != 1:
            raise ValueError("Saved source observation is missing or ambiguous")
        source = matches[0]
        body = source.get("body")
        if (not isinstance(body, str) or _digest(body) != item.source_body_hash
                or source.get("observation_count") != item.source_observation_count
                or source.get("source_conflicts") != item.source_conflicts
                or source.get("quality_codes") != item.source_quality_codes):
            raise ValueError("Saved observation text or quality binding changed")
        source = {**source, "body_hash": item.source_body_hash}
    if (not 0 <= item.start < item.end <= len(body)
            or body[item.start:item.end] != item.text):
        raise ValueError("Excerpt does not match its exact saved source interval")
    return source


def bind_search_row(row):
    """Remove private payloads and populate trusted observation metadata."""
    result = dict(row)
    payload = result.pop("observation_payload", None)
    issues = result.pop("source_text_issues", None)
    if result.get("dataset") == "native":
        result["source_text_quality_codes"] = current_text_quality_codes(issues)
    present = [result.get(key) is not None for key in FIELDS]
    if any(present) and not all(present):
        raise ValueError("Incomplete observation provenance in chunk")
    if not any(present):
        for key in FIELDS:
            result.pop(key, None)
        return Evidence.model_validate(result)
    if (not isinstance(payload, dict) or payload.get("record_id") != result.get("record_id")
            or payload.get("dataset") != result.get("dataset")
            or source_version_id(payload) != result.get("version_id")
            or payload.get("url") != result.get("url")):
        raise ValueError("Observation chunk is not bound to its enclosing record version")
    sources = public_observations(payload)
    matches = [source for source in sources if all(source[key] == result[key] for key in FIELDS)]
    if len(matches) != 1:
        raise ValueError("Chunk does not belong to a validated source observation")
    source = matches[0]
    result.update(source_observation_count=source["observation_count"],
                  source_conflicts=source["source_conflicts"], source_quality_codes=source["quality_codes"])
    item = Evidence.model_validate(result)
    evidence_source({"record_id": item.record_id, "version_id": item.version_id,
                     "dataset": item.dataset, "retrievable": payload.get("retrievable"),
                     "social_source_observations": sources}, item)
    return item
