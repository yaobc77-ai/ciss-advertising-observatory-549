"""A complete selected-corpus map with witnessed source associations.

This map is a view of the existing typed source graph. Its company/outlet links
are explicitly derived summaries of article paths, never assertions of payment,
ownership, lobbying, endorsement, or a verified business relationship. The
caller supplies one complete read snapshot of eligible native-ad records.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy

from .knowledge_graph import IDENTITY_POLICY, NODE_TYPES, PREDICATES, build_graph

SCHEMA_VERSION = "advertising-collection-map-v1"
SUMMARY_PREDICATE = "derived_source_association"
SUMMARY_DEFINITION = {
    "label": "co-listed in source records",
    "domain": ["SponsorCandidate"],
    "range": ["Outlet"],
    "description": (
        "The same eligible article records list this sponsor candidate and outlet. "
        "This derived count does not independently verify sponsorship, payment, "
        "corporate identity, endorsement, or a business relationship."
    ),
    "derivation": ["Article --source_lists_sponsor--> SponsorCandidate",
                   "Article --published_in--> Outlet"],
}
_CORE_FIELDS = ("record_id", "version_id", "dataset", "title", "date", "sponsor", "publisher",
                "created_at", "issues", "url", "archive_url")
_PUBLIC_FIELDS = ("record_id", "version_id", "dataset", "title", "date", "sponsor", "publisher")
_SOURCE_PREDICATES = {"source_lists_sponsor", "published_in"}


def _summary_id(source, target):
    identity = json.dumps([SCHEMA_VERSION, source, SUMMARY_PREDICATE, target], separators=(",", ":"))
    return "sourceassociation:" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def build_collection_map(rows, *, links_enabled=True):
    """Project all supplied eligible native articles into two selectable views.

    ``nodes`` includes every Article, SponsorCandidate and Outlet. ``article_edges``
    retains original typed source edges; ``summary_edges`` carries one counted,
    version-witnessed association per source sponsor/outlet pair. There is no
    Top-N truncation. Node ``record_count`` is article membership, not edge degree.

    Input duplicates are rejected, even when identical, because this view must
    expose an upstream snapshot/query error rather than silently change counts.
    Source versions/materials and annotations remain in the existing inspector;
    private imports, source paths and arbitrary annotation payloads are omitted.
    """
    rows = list(rows)
    seen = set()
    core_rows = []
    source_rows = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Collection map rows must be source record objects")
        if row.get("dataset") != "native":
            raise ValueError("The collection map currently supports native records only")
        if row.get("countable") is False or row.get("active") is False:
            raise ValueError("Collection map input must contain only active eligible records")
        identity = (row.get("dataset"), row.get("record_id"))
        if identity in seen:
            raise ValueError("Duplicate source record in collection map snapshot")
        seen.add(identity)
        # Only metadata enters the source-field projection. The overview does
        # not load/validate body bytes or turn historical labels into map edges.
        core_rows.append({key: deepcopy(row.get(key)) for key in _CORE_FIELDS})
        source_rows[row.get("record_id")] = row

    source_graph = build_graph(core_rows, links_enabled=links_enabled)
    node_lookup = {node["id"]: node for node in source_graph["nodes"]}
    article_nodes = [node for node in source_graph["nodes"] if node["type"] == "Article"]
    nodes = [deepcopy(node) for node in source_graph["nodes"]
             if node["type"] in {"Article", "SponsorCandidate", "Outlet"}]
    for node in nodes:
        node["record_count"] = len(node["record_ids"])
        node["properties"]["record_count"] = node["record_count"]

    article_edges = [deepcopy(edge) for edge in source_graph["edges"]
                     if edge["predicate"] in _SOURCE_PREDICATES]
    paths = defaultdict(dict)
    for edge in article_edges:
        if edge["predicate"] in paths[edge["source"]]:
            raise ValueError("Ambiguous source identity path in collection map")
        paths[edge["source"]][edge["predicate"]] = edge

    summaries = {}
    missing_sponsor, missing_outlet, associated = 0, 0, 0
    records = []
    artifact_edges = defaultdict(dict)
    for edge in source_graph["edges"]:
        if edge["predicate"] in {"has_source_reference", "has_archive_reference"}:
            artifact_edges[edge["source"]][edge["predicate"]] = node_lookup[edge["target"]]["properties"]["url"]

    for article in article_nodes:
        properties = article["properties"]
        record_id, version_id = properties["record_id"], properties["version_id"]
        row = source_rows[record_id]
        public = {field: row.get(field) for field in _PUBLIC_FIELDS}
        source_paths = paths[article["id"]]
        for field, predicate in (("sponsor", "source_lists_sponsor"), ("publisher", "published_in")):
            path = source_paths.get(predicate)
            public[field] = node_lookup[path["target"]]["properties"]["source_value"] if path else ""
        public["title"] = properties["title"]
        public["date"] = properties["published_at"]
        public["article_node_id"] = article["id"]
        public["detail_url"] = properties["detail_url"]
        public["retrievable"] = row.get("retrievable") is True
        body_hash = row.get("body_hash")
        public["body_hash"] = body_hash if isinstance(body_hash, str) and re.fullmatch(r"[0-9a-f]{64}", body_hash) else ""
        public["body_hash_status"] = "stored_reference_not_checked_in_overview"
        public["url"] = artifact_edges[article["id"]].get("has_source_reference", "")
        public["archive_url"] = artifact_edges[article["id"]].get("has_archive_reference", "")
        records.append(public)
        sponsor_path = paths[article["id"]].get("source_lists_sponsor")
        outlet_path = paths[article["id"]].get("published_in")
        missing_sponsor += sponsor_path is None
        missing_outlet += outlet_path is None
        if sponsor_path is None or outlet_path is None:
            continue
        associated += 1
        pair = sponsor_path["target"], outlet_path["target"]
        if pair not in summaries:
            summaries[pair] = {
                "id": _summary_id(*pair), "source": pair[0], "target": pair[1],
                "predicate": SUMMARY_PREDICATE, "label": SUMMARY_DEFINITION["label"],
                "count": 0, "record_ids": [], "witnesses": [],
                "provenance": {"method": "derived_from_article_source_fields",
                               "status": "source_association_unverified",
                               "basis_predicates": ["source_lists_sponsor", "published_in"]},
            }
        summary = summaries[pair]
        summary["count"] += 1
        summary["record_ids"].append(record_id)
        summary["witnesses"].append({
            "record_id": record_id, "version_id": version_id, "dataset": "native",
            "article_node_id": article["id"],
            "source_edge_ids": [sponsor_path["id"], outlet_path["id"]],
        })
    summary_edges = sorted(summaries.values(), key=lambda edge: edge["id"])
    for edge in summary_edges:
        edge["record_ids"].sort()
        edge["witnesses"].sort(key=lambda witness: witness["record_id"])

    total = len(article_nodes)
    result = {
        "schema_version": SCHEMA_VERSION,
        "source_schema_version": source_graph["schema_version"],
        "identity_policy": deepcopy(IDENTITY_POLICY),
        "node_types": {kind: deepcopy(NODE_TYPES[kind]) for kind in ("Article", "SponsorCandidate", "Outlet")},
        "predicate_definitions": {**{key: deepcopy(PREDICATES[key]) for key in sorted(_SOURCE_PREDICATES)},
                                  SUMMARY_PREDICATE: deepcopy(SUMMARY_DEFINITION)},
        "nodes": nodes,
        "article_edges": article_edges,
        "summary_edges": summary_edges,
        "records": sorted(records, key=lambda row: row["record_id"]),
        "coverage": {
            "total_records": total, "shown_records": total, "truncated": False,
            "associated_records": associated, "missing_sponsor_records": missing_sponsor,
            "missing_outlet_records": missing_outlet,
            "known_sponsor_records": total - missing_sponsor,
            "known_outlet_records": total - missing_outlet,
            "retrievable_records": sum(row["retrievable"] for row in records),
            "scope": "complete_supplied_selection", "counting_unit": "eligible_native_record",
        },
        "counts": {"articles": total, "sponsors": sum(node["type"] == "SponsorCandidate" for node in nodes),
                   "outlets": sum(node["type"] == "Outlet" for node in nodes),
                   "source_edges": len(article_edges), "summary_edges": len(summary_edges)},
        # No body was loaded by this metadata-only view. A body validation
        # warning from build_graph's omitted TextVersion is not a data defect.
        "warnings": [deepcopy(warning) for warning in source_graph["warnings"]
                     if warning["code"] != "body_hash_unverified"],
        "limitations": [
            "Associations are derived from sponsor/outlet fields co-listed in article records; payment and business relationships are not independently verified.",
            "Source names and display aliases do not resolve or merge real-world organizations. Associations, conferences and companies remain separate source categories.",
            "Every supplied eligible record counts, including articles without searchable text. Record counts are not unique-text counts.",
            "The source graph inspector provides text versions, source references and historical annotations; this overview does not classify or verify greenwashing.",
            "Article body bytes are not loaded or hash-validated in the collection overview. Stored version and body-hash references are checked by the record/evidence inspector.",
            "Completeness applies to the supplied filtered snapshot, not to all advertising published elsewhere.",
        ],
    }
    validate_collection_map(result)
    return result


def validate_collection_map(graph):
    """Check counted associations against their exact article-path witnesses."""
    nodes = {node["id"]: node for node in graph["nodes"]}
    if len(nodes) != len(graph["nodes"]):
        raise ValueError("Duplicate collection map node")
    edges = {edge["id"]: edge for edge in graph["article_edges"]}
    if len(edges) != len(graph["article_edges"]):
        raise ValueError("Duplicate article source edge")
    records = {row["record_id"]: row for row in graph["records"]}
    if len(records) != len(graph["records"]):
        raise ValueError("Duplicate public collection map record")
    article_ids = {node["id"] for node in nodes.values() if node["type"] == "Article"}
    if len(records) != len(article_ids) or len(records) != graph["coverage"]["total_records"]:
        raise ValueError("Collection map coverage does not match article membership")
    for node in nodes.values():
        if node["record_count"] != len(set(node["record_ids"])):
            raise ValueError("Collection map entity count is not article membership")
        if any(record_id not in records for record_id in node["record_ids"]):
            raise ValueError("Collection map entity refers to an unknown record")
    summary_ids, witnessed_articles = set(), set()
    for summary in graph["summary_edges"]:
        if summary["id"] in summary_ids:
            raise ValueError("Duplicate collection map summary edge")
        summary_ids.add(summary["id"])
        if (summary["predicate"] != SUMMARY_PREDICATE or summary["source"] not in nodes or summary["target"] not in nodes
                or nodes[summary["source"]]["type"] != "SponsorCandidate" or nodes[summary["target"]]["type"] != "Outlet"):
            raise ValueError("Invalid derived source association endpoints")
        if (summary["count"] != len(summary["witnesses"]) or summary["count"] != len(set(summary["record_ids"]))
                or sorted(summary["record_ids"]) != sorted(witness["record_id"] for witness in summary["witnesses"])):
            raise ValueError("Derived source association count lacks exact record witnesses")
        for witness in summary["witnesses"]:
            record = records.get(witness["record_id"])
            article_id = witness["article_node_id"]
            if (record is None or record["version_id"] != witness["version_id"] or witness["dataset"] != "native"
                    or record["article_node_id"] != article_id or article_id in witnessed_articles):
                raise ValueError("Derived source association has conflicting record/version witnesses")
            source_edges = [edges.get(identifier) for identifier in witness["source_edge_ids"]]
            if len(source_edges) != 2 or any(edge is None for edge in source_edges):
                raise ValueError("Derived source association lacks its source edge witnesses")
            targets = {edge["predicate"]: edge["target"] for edge in source_edges}
            if targets != {"source_lists_sponsor": summary["source"], "published_in": summary["target"]}:
                raise ValueError("Derived source association conflicts with witnessed article path")
            if any(edge["source"] != article_id or edge["provenance"]["record_id"] != witness["record_id"]
                   or edge["provenance"]["version_id"] != witness["version_id"] for edge in source_edges):
                raise ValueError("Derived source association conflicts with source provenance")
            witnessed_articles.add(article_id)
    if len(witnessed_articles) != graph["coverage"]["associated_records"]:
        raise ValueError("Associated coverage is inconsistent with summary witnesses")
    return []
