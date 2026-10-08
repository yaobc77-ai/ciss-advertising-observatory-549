"""Evidence-preserving graph projection of existing records, with no inference.

Source values identify *candidates*, not resolved real-world organizations. A
label assignment is an annotation record, never a factual verdict. This module
does not read private raw payloads or invent evidence for historical labels.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime
from urllib.parse import quote, urlsplit

from .analytics import sponsor_display, sponsor_metadata
from .entities import registry as entity_registry

SCHEMA_VERSION = "advertising-source-graph-v2"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,119}\Z")
_MISSING = {"", "(unknown)", "unknown", "n/a", "none", "null"}
NODE_TYPES = {
    "Article": {"description": "An imported article record, not a deduplicated real-world publication.",
                "identity_fields": ["dataset", "record_id"]},
    "TextVersion": {"description": "A stored text version with a body hash and capture limitations.",
                    "identity_fields": ["dataset", "record_id", "version_id"]},
    "SponsorCandidate": {"description": "An exact source sponsor-field value; organization identity is unresolved.",
                         "identity_fields": ["dataset", "source_field", "source_value"]},
    "Outlet": {"description": "An exact source publisher-field value, not a verified legal entity.",
               "identity_fields": ["dataset", "source_field", "source_value"]},
    "Organization": {"description": "A registry organization or outlet joining exact source spellings across collections; identity is AI-proposed until reviewed.",
                     "identity_fields": ["registry_id"]},
    "SourceArtifact": {"description": "An online URL reference or a hash-bound reviewed capture.",
                       "identity_fields": ["kind", "url_or_record_asset_hash"]},
    "Annotation": {"description": "An imported category-assignment record with explicit text scope.",
                   "identity_fields": ["dataset", "record_id", "version_id", "ordinal"]},
    "Label": {"description": "A source category scoped to a recorded or explicitly unknown codebook.",
              "identity_fields": ["dataset", "scheme", "label_key"]},
    "EvidenceSpan": {"description": "A character-located quotation; semantic support is not inferred.",
                     "identity_fields": ["dataset", "record_id", "version_id", "start", "end"]},
    "ClaimAssignment": {"description": "A published CLAIMS2 assignment with a current source and review revision.",
                        "identity_fields": ["candidate_key"]},
    "Subclaim": {"description": "An NC definition in one exact CLAIMS2 taxonomy bundle.",
                 "identity_fields": ["taxonomy_version", "nc_id"]},
    "Superclaim": {"description": "An SC definition in one exact CLAIMS2 taxonomy bundle.",
                   "identity_fields": ["taxonomy_version", "sc_id"]},
}
IDENTITY_POLICY = {
    "entity_resolution": "exact_source_values_kept_registry_organizations_join_listed_spellings",
    "missing_values": "omitted_entities_with_article_missing_flags",
    "display_aliases": "presentation_only_not_identity_assertions",
    "type_hints": "provisional_not_verified_organization_types",
    "absence": "missing_relations_do_not_imply_factual_absence",
    "annotation_scope": "hash_bound_current_text_or_unbound_historical_record",
}
_BODY_LIMITATIONS = {
    "body_partial_recovery", "body_truncated_suspected", "body_short", "body_footer_only",
    "body_question_only", "body_garbled", "body_video_placeholder", "body_numeric",
    "body_source_partial", "body_source_completeness_unestablished",
}


def _predicate(label, domain, range_, description):
    return {"label": label, "domain": [domain], "range": [range_],
            "description": description}


PREDICATES = {
    "has_text_version": _predicate("has text version", "Article", "TextVersion",
        "The stored text version for this source record; completeness is not implied."),
    "source_lists_sponsor": _predicate("source lists sponsor", "Article", "SponsorCandidate",
        "The stored sponsor field lists this candidate. Identity and payment are not independently verified."),
    "published_in": _predicate("source lists outlet", "Article", "Outlet",
        "The stored publisher field names this outlet candidate; it does not imply endorsement."),
    "identifies_organization": _predicate("identified as organization", "SponsorCandidate", "Organization",
        "The entity registry lists this exact source spelling for the organization. Review status is on the edge."),
    "identifies_outlet": _predicate("identified as outlet", "Outlet", "Organization",
        "The entity registry lists this exact publisher spelling for the outlet."),
    "supplemented_sponsor": _predicate("supplemented sponsor", "Article", "Organization",
        "The source lists no sponsor; the registry supplements one from stated evidence. The source field stays missing."),
    "has_source_reference": _predicate("has original reference", "Article", "SourceArtifact",
        "A source URL, not a guaranteed immutable or currently available capture."),
    "has_archive_reference": _predicate("has archive reference", "Article", "SourceArtifact",
        "A stored archive URL; its contents have not been verified by this graph projection."),
    "derived_from": _predicate("recovered from reviewed capture", "TextVersion", "SourceArtifact",
        "The public attachment registry binds this hash-checked capture to the text version."),
    "has_annotation_record": _predicate("has annotation record", "Article", "Annotation",
        "An imported annotation associated with the article. It may target an earlier body."),
    "annotates": _predicate("annotates this version", "Annotation", "TextVersion",
        "The annotation body hash matches the unchanged stored text version."),
    "assigns_label": _predicate("assigns category", "Annotation", "Label",
        "An annotation assigns a category under its recorded scheme; this is not a factual verdict."),
    "has_evidence": _predicate("cites located text", "Annotation", "EvidenceSpan",
        "A version- and hash-bound exact character span. Matching text does not prove semantic support."),
    "located_in": _predicate("located in", "EvidenceSpan", "TextVersion",
        "Python Unicode character positions within this exact text version."),
    "has_claim_assignment": _predicate("has published CLAIMS2 match", "Article", "ClaimAssignment",
        "A published taxonomy assignment, not an independent finding of false or misleading advertising."),
    "assigns_subclaim": _predicate("assigns subclaim", "ClaimAssignment", "Subclaim",
        "The saved assignment uses this NC definition under its exact taxonomy version."),
    "subclaim_of": _predicate("mapped to superclaim", "Subclaim", "Superclaim",
        "The pinned taxonomy maps this NC to this SC. An unmapped NC has no inferred parent."),
    "cites_claim_evidence": _predicate("cites original passage", "ClaimAssignment", "EvidenceSpan",
        "An exact source passage for the assignment. Location and semantic review are distinct."),
}


def _id(kind, *identity):
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return f"{kind.lower()}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"


def _text(value):
    return value if isinstance(value, str) else ""


def _key(value):
    value = _text(value)
    return value if _KEY.fullmatch(value) else ""


def _hash(value):
    value = _text(value)
    return value if _HASH.fullmatch(value) else ""


def _date(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    value = _text(value)
    try:
        return date.fromisoformat(value).isoformat() if value else None
    except ValueError:
        return None


def _url(value):
    value = _text(value)
    if any(c.isspace() or ord(c) < 32 for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not (
            parsed.username or parsed.password
        ):
            return value
    except ValueError:
        pass
    return ""


def _missing(value):
    return not isinstance(value, str) or value.strip().casefold() in _MISSING


def _payload(annotation):
    if not isinstance(annotation, Mapping):
        return {}
    payload = annotation.get("payload", annotation)
    return payload if isinstance(payload, Mapping) else {}


def _public_annotation(payload):
    """Allow-list imported labels; explanations/source paths never leave here."""
    version = _key(payload.get("version")) or "unrecorded"
    codebook = _key(payload.get("codebook_version"))
    basis = _text(payload.get("basis"))
    labels = payload.get("labels", [])
    labels = sorted({label for label in labels if isinstance(label, str)
                     and _LABEL.fullmatch(label)}) if isinstance(labels, list) else []
    return {
        "annotation_version": version,
        "codebook_version": codebook or "unavailable",
        "codebook_status": "recorded_unverified" if codebook else "unavailable",
        "recorded_body_hash": _hash(payload.get("body_sha256")),
        "recorded_version_id": _key(payload.get("version_id")),
        "status": "historical_automatic_unverified" if version.startswith("claims-")
        or payload.get("status") == "historical_automatic_unverified" else "imported_unverified",
        "source_sha256": _hash(payload.get("source_sha256")),
        "source_row": payload.get("source_row") if type(payload.get("source_row")) is int
        and payload["source_row"] > 0 else None,
        "basis": basis if basis in {
            "url_and_exact_body", "exact_body_hash", "exact_character_offsets",
        } else "not_recorded",
        "labels": labels,
    }


def build_graph(rows, *, links_enabled=True, attachments_by_record=None, claims2=None):
    """Return a bounded, deterministic, JSON-safe graph of supplied public rows.

    The caller controls paging. Exact dataset/field/source-value identities are
    preserved; display aliases never merge nodes. Conflicting current record
    rows fail closed. Private ``raw`` and arbitrary provenance are ignored.
    """
    nodes, edges, warnings, seen = {}, {}, {}, {}
    attachments_by_record = attachments_by_record or {}

    def warn(code, record_id, message):
        warnings[(code, record_id)] = {"code": code, "record_id": record_id, "message": message}

    def node(kind, identity, label, properties, record_id):
        identifier = _id(kind, *identity)
        item = {"id": identifier, "type": kind, "label": label,
                "properties": properties, "record_ids": [record_id]}
        if identifier in nodes:
            old = nodes[identifier]
            if any(old[key] != item[key] for key in ("type", "label", "properties")):
                raise ValueError("Conflicting properties for the same graph identity")
            old["record_ids"] = sorted(set(old["record_ids"]) | {record_id})
        else:
            nodes[identifier] = item
        return identifier

    def organization(entity, record_id):
        return node("Organization", (entity.id,), entity.name, {
            "registry_id": entity.id, "name": entity.name, "organization_type": entity.type,
            "aliases": list(entity.aliases), "former_names": list(entity.former_names),
            "source_values": {key: list(values) for key, values in entity.source_values.items()},
            "registry_record_counts": dict(entity.record_counts), "basis": entity.basis,
            "review_status": entity.review_status, "registry_version": entity_registry().version,
        }, record_id)

    def edge(source, target, predicate, record_id, version_id, field, *,
             method="source_field_projection", status="source_recorded_unverified", annotation_id=None):
        provenance = {"record_id": record_id, "version_id": version_id,
                      "field": field, "method": method, "status": status}
        if annotation_id:
            provenance["annotation_id"] = annotation_id
            annotation_properties = nodes[annotation_id]["properties"]
            provenance["annotation_source"] = {
                key: annotation_properties[key] for key in ("source_sha256", "source_row", "basis")
                if annotation_properties.get(key) is not None
            }
        identifier = _id("edge", source, predicate, target, record_id, version_id, field, annotation_id)
        edges[identifier] = {"id": identifier, "source": source, "target": target,
                             "predicate": predicate, "label": PREDICATES[predicate]["label"],
                             "provenance": provenance, "record_ids": [record_id]}

    for row in rows:
        record_id, version_id = _key(row.get("record_id")), _key(row.get("version_id"))
        dataset = _key(row.get("dataset"))
        if not record_id or not version_id or not dataset:
            raise ValueError("Graph records require safe record, version and dataset identifiers")
        identity = (dataset, record_id)
        if identity in seen:
            if seen[identity] != row:
                raise ValueError("Conflicting rows for the same current record")
            continue
        seen[identity] = row
        body = _text(row.get("body"))
        issue_codes = {issue.get("code") for issue in (row.get("issues") or [])
                       if isinstance(issue, Mapping) and isinstance(issue.get("code"), str)}
        limitations = sorted(issue_codes & _BODY_LIMITATIONS)
        digest = _hash(row.get("body_hash"))
        digest_valid = bool(digest) and hashlib.sha256(body.encode("utf-8")).hexdigest() == digest
        title = _text(row.get("title")) or "Untitled article"
        article = node("Article", (dataset, record_id), title, {
            "record_id": record_id, "version_id": version_id, "dataset": dataset, "title": title,
            "published_at": _date(row.get("date")), "missing_sponsor": _missing(row.get("sponsor")),
            "missing_outlet": _missing(row.get("publisher")),
            "detail_url": f"/records/{quote(record_id, safe='')}",
        }, record_id)
        text_version = node("TextVersion", (dataset, record_id, version_id), "Stored text version", {
            "version_id": version_id, "body_hash": digest, "body_characters": len(body),
            "body_status": "missing" if not body.strip() else "partial_or_quality_limited" if limitations else "stored_text",
            "completeness": "not_established", "quality_codes": limitations,
            "stored_at": _date(row.get("created_at")),
            "hash_status": "matched" if digest_valid else "missing_or_mismatched",
        }, record_id)
        edge(article, text_version, "has_text_version", record_id, version_id, "version_id")
        if not digest_valid:
            warn("body_hash_unverified", record_id, "The stored body hash is missing or mismatched; no exact evidence spans can be asserted.")
        if "historical_annotations_prior_body" in issue_codes:
            warn("historical_annotations_prior_body", record_id,
                 "Earlier annotations describe a previous body and are not classifications of the recovered current text.")
        for field, kind, predicate in (("sponsor", "SponsorCandidate", "source_lists_sponsor"),
                                        ("publisher", "Outlet", "published_in")):
            raw = row.get(field)
            if _missing(raw):
                warn(f"missing_{field}", record_id, f"No {field} entity is created for missing source values.")
                continue
            properties = {"source_value": raw, "dataset": dataset, "source_field": field,
                          "identity_status": "source_candidate_not_resolved",
                          "role": "listed_sponsor" if field == "sponsor" else "listed_outlet"}
            if field == "sponsor":
                hint = sponsor_metadata(raw)["entity_type"]
                properties["type_hint"] = hint if hint in {"conference/event", "industry association"} else "unresolved"
                properties["type_hint_status"] = "provisional_display_hint"
            entity = node(kind, (dataset, field, raw), sponsor_display(raw) if field == "sponsor" else raw,
                          properties, record_id)
            edge(article, entity, predicate, record_id, version_id, field)
            owner = entity_registry().for_source(dataset, field, raw)
            if owner is not None:
                edge(entity, organization(owner, record_id),
                     "identifies_organization" if field == "sponsor" else "identifies_outlet",
                     record_id, version_id, field, method="entity_registry", status=owner.review_status)
        fill = entity_registry().supplemented.get((dataset, record_id, "sponsor"))
        if fill and _missing(row.get("sponsor")) and fill.get("version_id") == version_id:
            owner = entity_registry().entities[fill["organization"]]
            edge(article, organization(owner, record_id), "supplemented_sponsor", record_id, version_id, "sponsor",
                 method="entity_registry_" + fill["method"], status=fill["review_status"])

        for field, kind, predicate in (("url", "web_reference", "has_source_reference"),
                                      ("archive_url", "archive_reference", "has_archive_reference")):
            url = _url(row.get(field))
            if not url or not links_enabled:
                continue
            source = node("SourceArtifact", (kind, url),
                          "Original web reference" if field == "url" else "Archive URL reference", {
                              "kind": kind, "url": url, "immutable_capture": False,
                              "verification_status": "url_recorded_contents_unverified",
                          }, record_id)
            edge(article, source, predicate, record_id, version_id, field)

        for attachment in attachments_by_record.get(record_id, []) if links_enabled else []:
            if not isinstance(attachment, Mapping):
                continue
            asset_id, asset_hash = _key(attachment.get("asset_id")), _hash(attachment.get("sha256"))
            path = f"/records/{quote(record_id, safe='')}/attachments/{quote(asset_id, safe='')}"
            if not asset_id or not asset_hash or attachment.get("url") != path or (
                attachment.get("identity_status") != "reviewed_local_capture" or not digest_valid
            ):
                warn("attachment_not_bound", record_id, "An attachment without the reviewed public binding was omitted.")
                continue
            source = node("SourceArtifact", ("reviewed_capture", record_id, asset_id, asset_hash),
                          "Reviewed PDF capture", {"kind": "reviewed_pdf_capture", "url": path,
                          "sha256": asset_hash, "immutable_capture": True,
                          "verification_status": "reviewed_local_capture"}, record_id)
            edge(text_version, source, "derived_from", record_id, version_id, "public_attachments",
                 method="reviewed_attachment_registry", status="reviewed_source_binding")

        annotations = row.get("annotations") or []
        for fallback_ordinal, annotation in enumerate(annotations):
            payload = _payload(annotation)
            public = _public_annotation(payload)
            ordinal = annotation.get("ordinal", fallback_ordinal) if isinstance(annotation, Mapping) else fallback_ordinal
            if type(ordinal) is not int or ordinal < 0:
                raise ValueError("Annotation ordinal must be a nonnegative integer")
            same_body = digest_valid and public["recorded_body_hash"] == digest
            same_version = payload.get("version_id") in (None, "", version_id)
            bound = same_body and same_version
            scope = "current_text_hash_bound" if bound else "prior_or_unavailable_body"
            ann_id = node("Annotation", (dataset, record_id, version_id, ordinal),
                          f"{public['annotation_version']} annotation", {
                              **{k: v for k, v in public.items() if k != "labels"},
                              "ordinal": ordinal, "scope_status": scope,
                              "evidence_status": "no_validated_spans",
                          }, record_id)
            ann_field = f"annotations[{ordinal}]"
            edge(article, ann_id, "has_annotation_record", record_id, version_id, ann_field,
                 method="imported_annotation", status=public["status"], annotation_id=ann_id)
            if bound:
                edge(ann_id, text_version, "annotates", record_id, version_id, ann_field + ".body_sha256",
                     method="exact_body_hash_match", status="text_binding_verified", annotation_id=ann_id)
            else:
                warn("annotation_not_current_text", record_id, "An annotation is retained as history but is not linked to the current text version.")
            for label in public["labels"]:
                scheme = public["codebook_version"] if public["codebook_version"] != "unavailable" else (
                    "unknown-codebook:" + public["annotation_version"]
                )
                label_id = node("Label", (dataset, scheme, label), label.replace("_", " "), {
                    "label_key": label, "scheme": scheme, "dataset": dataset,
                    "codebook_version": public["codebook_version"], "codebook_status": public["codebook_status"],
                    "meaning_status": "imported_category_not_a_factual_verdict",
                }, record_id)
                edge(ann_id, label_id, "assigns_label", record_id, version_id, ann_field + ".labels",
                     method="imported_annotation", status=public["status"], annotation_id=ann_id)
            evidence = payload.get("evidence", [])
            evidence = evidence if isinstance(evidence, list) else []
            for span in evidence:
                if not bound or not _valid_span(span, body, digest, version_id):
                    warn("evidence_span_rejected", record_id, "A supplied evidence span lacked an exact version, hash, or character match and was omitted.")
                    continue
                span_id = node("EvidenceSpan", (dataset, record_id, version_id, span["start"], span["end"]),
                               "Located source passage", {"version_id": version_id, "body_hash": digest,
                               "start": span["start"], "end": span["end"], "quote": span["quote"],
                               "verification_status": "exact_character_match",
                               "semantic_support_status": "not_verified"}, record_id)
                edge(ann_id, span_id, "has_evidence", record_id, version_id, ann_field + ".evidence",
                     method="exact_character_validation", status="location_verified", annotation_id=ann_id)
                edge(span_id, text_version, "located_in", record_id, version_id, "body",
                     method="exact_character_validation", status="location_verified")
                nodes[ann_id]["properties"]["evidence_status"] = "has_validated_spans"

    graph = {"schema_version": SCHEMA_VERSION, "node_types": deepcopy(NODE_TYPES),
             "identity_policy": dict(IDENTITY_POLICY), "predicate_definitions": deepcopy(PREDICATES),
             "nodes": sorted(nodes.values(), key=lambda item: item["id"]),
             "edges": sorted(edges.values(), key=lambda item: item["id"]),
             "warnings": [warnings[key] for key in sorted(warnings)]}
    if claims2 is not None:
        from .claims_graph import extend_claims_graph

        graph = extend_claims_graph(graph, list(seen.values()), claims2)
    validate_graph(graph, rows=list(seen.values()))
    return graph


def relationship_counts(graph):
    """Project article paths into the existing sponsor × publisher count shape.

    Each article contributes once. Missing dimensions become empty count
    categories consumed by analytics, never graph entities. This is an equality
    check for the projection's scope, not a replacement for full-selection SQL.
    """
    validate_graph(graph)
    nodes = {node["id"]: node for node in graph["nodes"]}
    article_paths = {}
    for edge in graph["edges"]:
        if edge["predicate"] in {"source_lists_sponsor", "published_in"}:
            article_paths.setdefault(edge["source"], {}).setdefault(edge["predicate"], []).append(edge)
    counts = Counter()
    for article in (node for node in nodes.values() if node["type"] == "Article"):
        dimensions = []
        for predicate, missing in (("source_lists_sponsor", "missing_sponsor"),
                                   ("published_in", "missing_outlet")):
            paths = article_paths.get(article["id"], {}).get(predicate, [])
            if len(paths) > 1 or bool(paths) == bool(article["properties"].get(missing)):
                raise ValueError("Article path is ambiguous or inconsistent with its missing-field state")
            dimensions.append(nodes[paths[0]["target"]]["properties"]["source_value"] if paths else "")
        counts[tuple(dimensions)] += 1
    return [{"sponsor": sponsor, "publisher": publisher, "count": count}
            for (sponsor, publisher), count in sorted(counts.items())]


def _valid_span(span, body, digest, version_id):
    if not isinstance(span, Mapping):
        return False
    start, end = span.get("start"), span.get("end")
    return (type(start) is int and type(end) is int and 0 <= start < end <= len(body)
            and span.get("body_sha256", span.get("body_hash")) == digest
            and span.get("version_id") == version_id and span.get("quote") == body[start:end])


def validate_graph(graph, *, rows=None):
    """Raise on dangling/ill-typed links or falsely located evidence.

    Structural validity does not verify source facts or category semantics.
    When rows are supplied, validate evidence again against the source body.
    """
    nodes = {node["id"]: node for node in graph["nodes"]}
    if len(nodes) != len(graph["nodes"]):
        raise ValueError("Duplicate graph node identifiers")
    if any(node.get("type") not in NODE_TYPES for node in nodes.values()):
        raise ValueError("Unknown graph node type")
    edge_ids = set()
    for edge in graph["edges"]:
        if edge["id"] in edge_ids:
            raise ValueError("Duplicate graph edge identifiers")
        edge_ids.add(edge["id"])
        if edge.get("source") not in nodes or edge.get("target") not in nodes:
            raise ValueError("Graph edge has a missing endpoint")
        definition = PREDICATES.get(edge.get("predicate"))
        if not definition:
            raise ValueError("Unknown graph predicate")
        if nodes[edge["source"]]["type"] not in definition["domain"] or (
            nodes[edge["target"]]["type"] not in definition["range"]
        ):
            raise ValueError("Graph edge violates predicate domain or range")
        provenance = edge.get("provenance") or {}
        if not all(provenance.get(key) for key in ("record_id", "version_id", "field", "method", "status")):
            raise ValueError("Graph edge lacks source provenance")
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        record_id = provenance["record_id"]
        if edge.get("record_ids") != [record_id] or any(
            record_id not in endpoint.get("record_ids", []) for endpoint in (source, target)
        ):
            raise ValueError("Graph edge record provenance conflicts with endpoint membership")
        for endpoint in (source, target):
            if endpoint["type"] in {"Article", "TextVersion", "EvidenceSpan", "ClaimAssignment"} and (
                endpoint["properties"].get("version_id") != provenance["version_id"]
            ):
                raise ValueError("Graph edge version provenance conflicts with endpoint version")
        if edge["predicate"] == "annotates" and (
            source["properties"].get("scope_status") != "current_text_hash_bound"
            or source["properties"].get("recorded_body_hash") != target["properties"].get("body_hash")
            or source["properties"].get("recorded_version_id") not in {"", target["properties"].get("version_id")}
        ):
            raise ValueError("Annotation cannot bind to this text version")
    lookup = {(str(row["record_id"]), str(row["version_id"])): row for row in rows or []}
    for node in nodes.values():
        if node["type"] != "EvidenceSpan":
            continue
        span = node["properties"]
        if span.get("verification_status") != "exact_character_match" or not _hash(span.get("body_hash")):
            raise ValueError("Evidence lacks a verified body binding")
        if type(span.get("start")) is not int or type(span.get("end")) is not int or not (
            0 <= span["start"] < span["end"] and isinstance(span.get("quote"), str)
            and len(span["quote"]) == span["end"] - span["start"]
        ):
            raise ValueError("Evidence has invalid character positions")
        locations = [edge for edge in graph["edges"] if edge["source"] == node["id"]
                     and edge["predicate"] == "located_in"]
        if len(locations) != 1 or nodes[locations[0]["target"]]["properties"].get("body_hash") != span["body_hash"]:
            raise ValueError("Evidence lacks exactly one matching text location")
        if rows is not None:
            for record_id in node["record_ids"]:
                row = lookup.get((record_id, span.get("version_id")))
                if row is None or hashlib.sha256(_text(row.get("body")).encode("utf-8")).hexdigest() != span["body_hash"] or not _valid_span(
                    span, _text(row.get("body")), span["body_hash"], str(row["version_id"])
                ):
                    raise ValueError("Evidence does not match its source text")
    return []


ORGANIZATION_NOTE = (
    "Organizations join exact source spellings across collections. Native articles and social "
    "posts are separate units and are never summed. Trade associations and events are listed "
    "with their own type; whether they belong in a company count awaits the client's decision. "
    "Identities are AI-proposed until a named reviewer approves them. A collected company post "
    "is not a verified paid advertisement."
)


def organization_overview(counts):
    """Every registry organization and outlet with countable records per collection.

    ``counts`` maps (dataset, field, exact spelling) to countable current records,
    so organizations found only in the social collection appear as well.
    """
    current = entity_registry()
    items = []
    for entity in current.entities.values():
        field = "sponsor" if entity.kind == "organization" else "publisher"
        per_collection = {}
        for key, values in entity.source_values.items():
            dataset = key.split(".")[0]
            per_collection[dataset] = per_collection.get(dataset, 0) + sum(
                counts.get((dataset, field, value), 0) for value in values)
        items.append({**entity.public(), "kind": entity.kind, "aliases": list(entity.aliases),
                      "former_names": list(entity.former_names),
                      "source_values": {key: list(values) for key, values in entity.source_values.items()},
                      "countable_records": per_collection, "basis": entity.basis})
    items.sort(key=lambda item: (-sum(item["countable_records"].values()), item["id"]))
    return {"registry_version": current.version, "review": dict(current.review),
            "note": ORGANIZATION_NOTE, "organizations": items}
