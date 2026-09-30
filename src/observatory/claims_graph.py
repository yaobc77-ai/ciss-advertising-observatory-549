"""Typed CLAIMS2 edges over a bounded, source-bound article graph.

Only the deliberate ClaimsStore public projection is consumed. Definitions
belong to a taxonomy; review status belongs to an assignment, not to a company
or the quoted statement's external truth. No missing result is a negative.
"""

import re
from copy import deepcopy

from .knowledge_graph import PREDICATES, _hash, _id, _valid_span


def extend_claims_graph(graph, rows, matches):
    result = deepcopy(graph)
    nodes = {item["id"]: item for item in result["nodes"]}
    edges = {item["id"]: item for item in result["edges"]}
    lookup = {row["record_id"]: row for row in rows}

    def add_node(kind, identity, label, properties, record_id):
        identifier = _id(kind, *identity)
        item = {"id": identifier, "type": kind, "label": label,
                "properties": properties, "record_ids": [record_id]}
        if identifier in nodes:
            existing = nodes[identifier]
            if any(existing[key] != item[key] for key in ("type", "label", "properties")):
                raise ValueError("Conflicting CLAIMS graph identity")
            existing["record_ids"] = sorted(set(existing["record_ids"]) | {record_id})
        else:
            nodes[identifier] = item
        return identifier

    def add_edge(source, target, predicate, claim, field):
        provenance = {key: claim[key] for key in (
            "record_id", "version_id", "candidate_key", "run_id", "taxonomy_version", "review_version",
        )}
        provenance.update(field=field, method="claims2_published_assignment", status=claim["review_state"])
        identifier = _id("edge", source, predicate, target, claim["candidate_key"])
        if predicate == "located_in":
            # Location describes literal text, independently of whichever
            # assignment happened to create the shared span first.
            provenance = {"record_id": claim["record_id"], "version_id": claim["version_id"],
                          "field": "body", "method": "exact_character_validation", "status": "location_verified"}
            identifier = _id("edge", source, predicate, target, claim["record_id"], claim["version_id"], "body", None)
        edges[identifier] = {"id": identifier, "source": source, "target": target,
                             "predicate": predicate, "label": PREDICATES[predicate]["label"],
                             "record_ids": [claim["record_id"]], "provenance": provenance}

    version = matches.get("claims_version")
    if matches.get("available") and not _hash(version):
        raise ValueError("CLAIMS graph requires a publication version")
    if not matches.get("available") and matches.get("records"):
        raise ValueError("Unavailable CLAIMS results cannot publish graph edges")
    for record in matches.get("records", []):
        row = lookup.get(record.get("record_id"))
        if row is None:
            raise ValueError("CLAIMS graph result is outside the selected article page")
        for claim in record.get("claims", []):
            span = {key: claim.get(key) for key in ("body_hash", "version_id", "start", "end", "quote")}
            if claim.get("record_id") != row["record_id"] or claim.get("dataset") != row["dataset"] or (
                claim.get("body_hash") != row.get("body_hash")
                or not _valid_span(span, row["body"], row.get("body_hash"), row["version_id"])
            ):
                raise ValueError("CLAIMS graph evidence does not match the selected original text")
            for key in ("candidate_key", "taxonomy_version", "review_version"):
                if not _hash(claim.get(key)):
                    raise ValueError("CLAIMS graph requires immutable publication identities")
            run = claim.get("run_id")
            if not isinstance(run, str) or not run.strip() or claim.get("review_state") not in {
                "automatic_unverified", "human_supported",
            }:
                raise ValueError("CLAIMS graph requires a valid run and review status")
            if not re.fullmatch(r"NC_[1-9][0-9]*", claim.get("nc_id") or "") or (
                not isinstance(claim.get("nc_definition"), str) or not claim["nc_definition"].strip()
            ):
                raise ValueError("CLAIMS graph requires a defined subclaim")
            sc = claim.get("sc_id")
            if sc is not None and (not re.fullmatch(r"SC_[1-9][0-9]*", sc) or (
                not isinstance(claim.get("sc_definition"), str) or not claim["sc_definition"].strip()
            )):
                raise ValueError("CLAIMS graph requires a defined mapped superclaim")
            rid, dataset, vid = row["record_id"], row["dataset"], row["version_id"]
            article, text = _id("Article", dataset, rid), _id("TextVersion", dataset, rid, vid)
            if article not in nodes or text not in nodes:
                raise ValueError("CLAIMS graph requires the original article and text nodes")
            assignment = add_node("ClaimAssignment", (claim["candidate_key"],),
                                  f"{claim['nc_id']} assignment", {
                **{key: claim[key] for key in ("candidate_key", "version_id", "body_hash", "run_id",
                                              "taxonomy_version", "review_version", "review_state")},
                "system": "CLAIMS2", "mapping_status": "mapped" if sc else "unmapped",
                "meaning": "Taxonomy assignment; not independent fact checking.",
            }, rid)
            subclaim = add_node("Subclaim", (claim["taxonomy_version"], claim["nc_id"]), claim["nc_id"], {
                "nc_id": claim["nc_id"], "definition": claim["nc_definition"],
                "taxonomy_version": claim["taxonomy_version"], "system": "CLAIMS2",
            }, rid)
            evidence = add_node("EvidenceSpan", (dataset, rid, vid, claim["start"], claim["end"]),
                                "Located source passage", {
                **span, "verification_status": "exact_character_match", "semantic_support_status": "not_verified",
            }, rid)
            add_edge(article, assignment, "has_claim_assignment", claim, "claims2_results")
            add_edge(assignment, subclaim, "assigns_subclaim", claim, "claims2_results.nc_id")
            add_edge(assignment, evidence, "cites_claim_evidence", claim, "claims2_results.quote")
            # A shared literal span has exactly one text-location edge even
            # when multiple labels or historical annotations cite it.
            location = next((edge for edge in edges.values() if edge["source"] == evidence
                             and edge["predicate"] == "located_in"), None)
            if location is None:
                add_edge(evidence, text, "located_in", claim, "record_versions.body")
            if sc:
                superclaim = add_node("Superclaim", (claim["taxonomy_version"], sc), sc, {
                    "sc_id": sc, "definition": claim["sc_definition"],
                    "taxonomy_version": claim["taxonomy_version"], "system": "CLAIMS2",
                }, rid)
                add_edge(subclaim, superclaim, "subclaim_of", claim, "taxonomy.claim_superclaim_map")
    result["nodes"] = sorted(nodes.values(), key=lambda item: item["id"])
    result["edges"] = sorted(edges.values(), key=lambda item: item["id"])
    result["claims2"] = {
        "available": bool(matches.get("available")), "claims_version": version,
        "shown_match_records": len(matches.get("records", [])),
        "shown_assignments": sum(len(record.get("claims", [])) for record in matches.get("records", [])),
        "coverage": "Published matches for this article page only; missing results are not negative classifications.",
        "classification_completion_known": False,
    }
    return result
