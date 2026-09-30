"""Read a versioned CLAIMS taxonomy candidate without approving or changing it.

Definitions and histories remain upstream data. Structurally malformed inputs
fail; incomplete references remain explicit audit issues. In particular, an
unmapped subclaim is never assigned a guessed superclaim.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

TAXONOMY_FILES = (
    "greenwashing_codebook.json",
    "greenwashing_superclaims.json",
    "claim_superclaim_map.json",
    "greenwashing_claim_history.json",
)
_NC_ID = re.compile(r"NC_[1-9][0-9]*")
_SC_ID = re.compile(r"SC_[1-9][0-9]*")
_EVENT_FIELDS = frozenset({
    "timestamp", "action", "text", "source_article_id", "article_url",
    "source_snippet", "metadata",
})


class ClaimsTaxonomyError(ValueError):
    """The supplied bundle cannot be interpreted using this reader's schema."""


@dataclass(frozen=True)
class TaxonomyIssue:
    code: str
    subclaim_id: str | None = None
    superclaim_id: str | None = None

    def to_dict(self) -> dict[str, str]:
        return {
            key: value for key, value in {
                "code": self.code,
                "subclaim_id": self.subclaim_id,
                "superclaim_id": self.superclaim_id,
            }.items() if value is not None
        }


@dataclass(frozen=True)
class TaxonomyBundle:
    """An immutable, unapproved snapshot of the four exact input files."""

    source_directory: Path
    file_hashes: Mapping[str, str]
    bundle_fingerprint: str
    subclaims: Mapping[str, str]
    superclaims: Mapping[str, str]
    raw_claim_superclaim_map: Mapping[str, str]
    claim_superclaim_map: Mapping[str, str]
    history: Mapping[str, Mapping[str, Any]]
    history_last_updated: str
    issues: tuple[TaxonomyIssue, ...]
    authority_status: str = field(default="candidate_not_approved", init=False)

    def to_manifest(self) -> dict[str, Any]:
        """Return JSON-safe provenance and integrity findings, without excerpts."""
        issue_codes = {issue.code for issue in self.issues}
        return {
            "schema_version": 1,
            "kind": "claims_taxonomy_candidate",
            "authority_status": self.authority_status,
            "bundle_fingerprint": self.bundle_fingerprint,
            "bundle_fingerprint_algorithm": "observatory.claims-taxonomy-bundle.v1",
            "file_hashes": dict(self.file_hashes),
            "counts": {
                "subclaims": len(self.subclaims),
                "superclaims": len(self.superclaims),
                "raw_mapping_entries": len(self.raw_claim_superclaim_map),
                "valid_mapping_entries": len(self.claim_superclaim_map),
                "history_claims": len(self.history),
                "history_events": sum(len(item["history"]) for item in self.history.values()),
            },
            "structurally_valid": True,
            "mapping_complete": set(self.claim_superclaim_map) == set(self.subclaims),
            "mapping_references_valid": not issue_codes.intersection({
                "dangling_mapping_subclaim", "dangling_mapping_superclaim",
            }),
            "history_consistent": not issue_codes.intersection({
                "missing_claim_history", "dangling_history_subclaim", "history_definition_mismatch",
            }),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ClaimsTaxonomyError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ClaimsTaxonomyError(f"Nonstandard JSON constant: {value}")


def _check_json_values(value: Any, location: str) -> None:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ClaimsTaxonomyError(f"{location}: invalid Unicode string") from exc
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_json_values(key, location)
            _check_json_values(item, location)
    elif isinstance(value, list):
        for item in value:
            _check_json_values(item, location)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ClaimsTaxonomyError(f"{location}: JSON number is not finite")


def _object(data: bytes, filename: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ClaimsTaxonomyError) as exc:
        raise ClaimsTaxonomyError(f"{filename}: {exc}") from exc
    if not isinstance(value, dict):
        raise ClaimsTaxonomyError(f"{filename}: top-level value must be an object")
    _check_json_values(value, filename)
    return value


def _id(value: Any, pattern: re.Pattern[str], location: str) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ClaimsTaxonomyError(f"{location}: invalid identifier {value!r}")


def _text(value: Any, location: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ClaimsTaxonomyError(f"{location}: expected {'nonempty ' if nonempty else ''}string")


def _definitions(value: dict[str, Any], pattern: re.Pattern[str], filename: str) -> None:
    if not value:
        raise ClaimsTaxonomyError(f"{filename}: definitions must not be empty")
    for claim_id, definition in value.items():
        _id(claim_id, pattern, filename)
        _text(definition, f"{filename}/{claim_id}")


def _timestamp(value: Any, location: str) -> None:
    _text(value, location)
    try:
        if "T" not in value and " " not in value:
            raise ValueError("Time component is missing")
        datetime.fromisoformat(value)
    except ValueError as exc:
        raise ClaimsTaxonomyError(f"{location}: expected ISO timestamp") from exc


def _history(value: dict[str, Any], filename: str) -> dict[str, Any]:
    if set(value) != {"claims", "last_updated"}:
        raise ClaimsTaxonomyError(f"{filename}: expected claims and last_updated fields")
    _timestamp(value["last_updated"], f"{filename}/last_updated")
    claims = value["claims"]
    if not isinstance(claims, dict):
        raise ClaimsTaxonomyError(f"{filename}/claims: expected object")
    for claim_id, item in claims.items():
        _id(claim_id, _NC_ID, filename)
        location = f"{filename}/claims/{claim_id}"
        if not isinstance(item, dict) or set(item) != {"current_text", "history"}:
            raise ClaimsTaxonomyError(f"{location}: expected current_text and history fields")
        _text(item["current_text"], f"{location}/current_text")
        if not isinstance(item["history"], list):
            raise ClaimsTaxonomyError(f"{location}/history: expected list")
        for index, event in enumerate(item["history"]):
            event_location = f"{location}/history/{index}"
            if not isinstance(event, dict) or set(event) != _EVENT_FIELDS:
                raise ClaimsTaxonomyError(f"{event_location}: unsupported event structure")
            _timestamp(event["timestamp"], f"{event_location}/timestamp")
            for event_field in _EVENT_FIELDS - {"timestamp", "metadata"}:
                # Missing provenance is allowed only as a literal upstream string;
                # this reader does not turn unknown/N/A into source evidence.
                _text(
                    event[event_field], f"{event_location}/{event_field}",
                    nonempty=event_field in {"action", "text"},
                )
            if not isinstance(event["metadata"], dict):
                raise ClaimsTaxonomyError(f"{event_location}/metadata: expected object")
    return claims


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def load_taxonomy_bundle(directory: str | Path) -> TaxonomyBundle:
    """Read and validate a candidate directory; perform no writes or inference.

    The bundle fingerprint binds file names to hashes of the original bytes,
    including whitespace. Directory location has no effect on the fingerprint.
    """
    try:
        root = Path(directory).resolve(strict=True)
        if not root.is_dir():
            raise ClaimsTaxonomyError("Taxonomy bundle must be a directory")
        contents = {}
        for filename in TAXONOMY_FILES:
            path = (root / filename).resolve(strict=True)
            if not path.is_relative_to(root) or not path.is_file():
                raise ClaimsTaxonomyError(f"{filename}: file must remain inside bundle directory")
            contents[filename] = path.read_bytes()
    except OSError as exc:
        raise ClaimsTaxonomyError(f"Taxonomy bundle could not be read: {exc}") from exc

    values = {name: _object(data, name) for name, data in contents.items()}
    subclaims = values[TAXONOMY_FILES[0]]
    superclaims = values[TAXONOMY_FILES[1]]
    mapping = values[TAXONOMY_FILES[2]]
    history_value = values[TAXONOMY_FILES[3]]
    _definitions(subclaims, _NC_ID, TAXONOMY_FILES[0])
    _definitions(superclaims, _SC_ID, TAXONOMY_FILES[1])
    for subclaim_id, superclaim_id in mapping.items():
        _id(subclaim_id, _NC_ID, TAXONOMY_FILES[2])
        _id(superclaim_id, _SC_ID, f"{TAXONOMY_FILES[2]}/{subclaim_id}")
    history = _history(history_value, TAXONOMY_FILES[3])

    issues = []
    valid_mapping = {}
    for subclaim_id, superclaim_id in sorted(mapping.items()):
        if subclaim_id not in subclaims:
            issues.append(TaxonomyIssue("dangling_mapping_subclaim", subclaim_id, superclaim_id))
        if superclaim_id not in superclaims:
            issues.append(TaxonomyIssue("dangling_mapping_superclaim", subclaim_id, superclaim_id))
        if subclaim_id in subclaims and superclaim_id in superclaims:
            valid_mapping[subclaim_id] = superclaim_id
    for subclaim_id in sorted(set(subclaims) - set(valid_mapping)):
        issues.append(TaxonomyIssue("unmapped_subclaim", subclaim_id))
    for subclaim_id in sorted(set(subclaims) - set(history)):
        issues.append(TaxonomyIssue("missing_claim_history", subclaim_id))
    for subclaim_id, item in sorted(history.items()):
        if subclaim_id not in subclaims:
            issues.append(TaxonomyIssue("dangling_history_subclaim", subclaim_id))
        elif item["current_text"] != subclaims[subclaim_id]:
            issues.append(TaxonomyIssue("history_definition_mismatch", subclaim_id))

    file_hashes = {name: hashlib.sha256(contents[name]).hexdigest() for name in sorted(contents)}
    fingerprint_payload = json.dumps(
        {"schema_version": 1, "files": file_hashes}, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    fingerprint = hashlib.sha256(
        b"observatory.claims-taxonomy-bundle.v1\0" + fingerprint_payload,
    ).hexdigest()
    return TaxonomyBundle(
        source_directory=root,
        file_hashes=_freeze(file_hashes),
        bundle_fingerprint=fingerprint,
        subclaims=_freeze(subclaims),
        superclaims=_freeze(superclaims),
        raw_claim_superclaim_map=_freeze(mapping),
        claim_superclaim_map=_freeze(valid_mapping),
        history=_freeze(history),
        history_last_updated=history_value["last_updated"],
        issues=tuple(issues),
    )
