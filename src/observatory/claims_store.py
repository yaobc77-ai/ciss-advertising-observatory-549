"""Transactional CLAIMS2 publication; article and retrieval versions stay immutable.

Files prepare the human assertions; the database rechecks source eligibility at
the publication boundary. Retractions retain history and cannot be undone by
reimport. Public reads return a deliberate projection, never raw model payloads.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from psycopg.types.json import Jsonb

from .chunking import retrieval_spans
from .models import Filters


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _identity(publication):
    """Only the original assignment and its evidence define candidate identity."""
    return {name: publication[name] for name in (
        "candidate_key", "record_id", "version_id", "body_hash", "dataset", "run_id",
        "taxonomy_bundle_fingerprint", "nc_id", "sc_id", "mapping_state",
        "expected_definition_sha256", "evidence",
    )}


def _source_check(row, publication, dataset):
    evidence = publication["evidence"]
    if not row or row["dataset"] != dataset or row["record_id"] != publication["record_id"]:
        raise ValueError("CLAIMS source record or dataset does not match")
    if not row["active"] or row["current_version"] != publication["version_id"]:
        raise ValueError("CLAIMS source is no longer the active current version")
    actual_hash = hashlib.sha256(row["body"].encode("utf-8")).hexdigest()
    if actual_hash != row["body_hash"] or actual_hash != publication["body_hash"]:
        raise ValueError("CLAIMS source body hash does not match")
    start, end = evidence["start"], evidence["end"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(row["body"]):
        raise ValueError("CLAIMS evidence offsets are invalid")
    if row["body"][start:end] != evidence["quote"]:
        raise ValueError("CLAIMS evidence does not equal the original quote")
    payload = row["payload"]
    if payload.get("record_id") != row["record_id"] or payload.get("dataset") != dataset:
        raise ValueError("CLAIMS source payload identity does not match")
    spans = retrieval_spans(row["body"], retrieval_ranges=payload.get("retrieval_ranges"),
                            retrieval_end=payload.get("retrieval_end"))
    if not payload.get("retrievable") or not payload.get("countable") or not any(
        left <= start < end <= right for left, right in spans
    ):
        raise ValueError("CLAIMS evidence is outside the currently accepted source scope")


class ClaimsStore:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _available(conn):
        return conn.execute("SELECT to_regclass('claims2_results') AS name").fetchone()["name"] is not None

    @staticmethod
    def _insert_immutable(conn, table, key_name, key, payload, hash_name):
        # Identifiers come only from the literal call sites below.
        fingerprint = _hash(payload)
        existing = conn.execute(
            f"SELECT {hash_name} FROM {table} WHERE {key_name}=%s", (key,),
        ).fetchone()
        if existing and existing[hash_name] != fingerprint:
            raise ValueError(f"Immutable {table} identity has conflicting content")
        return fingerprint, existing is not None

    def import_prepared(self, prepared, *, apply=False):
        from .claims_publication import validate_prepared_import

        validate_prepared_import(prepared)
        summary = {**prepared.summary(), "dry_run": not apply, "inserted": 0, "unchanged": 0, "review_revisions": 0,
                   "publication_applied": False, "model_calls": 0}
        if not prepared.publications:
            return summary
        bundle = prepared.taxonomy_bundle
        taxonomy = {"manifest": bundle.to_manifest(), "subclaims": dict(bundle.subclaims),
                    "superclaims": dict(bundle.superclaims), "mapping": dict(bundle.claim_superclaim_map)}
        run_key = _hash({"schema_version": 1, "run_id": prepared.run_id,
                         "taxonomy": bundle.bundle_fingerprint, "dataset": prepared.dataset})
        import_key = _hash({"run_key": run_key, "audit": prepared.audit_manifest_sha256,
                            "review": prepared.review_manifest_sha256})
        with self.db.connect() as conn:
            if not apply:
                conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            else:
                # Same lock as article imports, retrieval publication and migration.
                conn.execute("SELECT pg_advisory_xact_lock(54901)")
            available = self._available(conn)
            if apply and not available:
                raise ValueError("Apply the ordered database migrations before CLAIMS import")
            # Validate every source before making the first write.
            for item in prepared.publications:
                source = conn.execute(
                    "SELECT r.record_id,r.dataset,r.current_version,r.active,v.body,v.body_hash,v.payload "
                    "FROM records r JOIN record_versions v ON v.version_id=%s AND v.record_id=r.record_id "
                    "WHERE r.record_id=%s", (item["version_id"], item["record_id"]),
                ).fetchone()
                _source_check(source, item, prepared.dataset)
            taxonomy_hash, taxonomy_exists = _hash(taxonomy), False
            run_hash, run_exists = _hash(prepared.run_metadata), False
            existing_results, existing_reviews = {}, {}
            if available:
                taxonomy_hash, taxonomy_exists = self._insert_immutable(
                    conn, "claims2_taxonomies", "bundle_fingerprint", bundle.bundle_fingerprint,
                    taxonomy, "payload_sha256",
                )
                run_hash, run_exists = self._insert_immutable(
                    conn, "claims2_runs", "run_key", run_key, prepared.run_metadata, "metadata_sha256",
                )
                for item in prepared.publications:
                    existing_results[item["candidate_key"]] = self._insert_immutable(
                        conn, "claims2_results", "candidate_key", item["candidate_key"], _identity(item), "payload_sha256",
                    )
                    review_key = _hash(item)
                    existing_reviews[item["candidate_key"]] = (review_key, conn.execute(
                        "SELECT 1 FROM claims2_reviews WHERE review_key=%s", (review_key,),
                    ).fetchone() is not None)
                    current = conn.execute(
                        "SELECT review_state FROM claims2_reviews WHERE candidate_key=%s "
                        "ORDER BY revision DESC LIMIT 1", (item["candidate_key"],),
                    ).fetchone()
                    if current and current["review_state"] == "human_supported" and item["review_state"] != "human_supported":
                        raise ValueError("A supported review cannot be downgraded by reimport; retract the candidate instead")
            if not apply:
                summary["source_checks_passed"] = len(prepared.publications)
                summary["database_schema_ready"] = available
                summary["existing_identity_checks_passed"] = available
                summary["planned_insertions"] = sum(not value[1] for value in existing_results.values()) if available else None
                summary["planned_review_revisions"] = sum(not value[1] for value in existing_reviews.values()) if available else None
                summary["planned_unchanged"] = sum(value[1] for value in existing_reviews.values()) if available else None
                return summary
            if not taxonomy_exists:
                conn.execute("INSERT INTO claims2_taxonomies(bundle_fingerprint,payload,payload_sha256) VALUES (%s,%s,%s)",
                             (bundle.bundle_fingerprint, Jsonb(taxonomy), taxonomy_hash))
            if not run_exists:
                conn.execute("INSERT INTO claims2_runs(run_key,upstream_run_id,dataset,bundle_fingerprint,metadata,metadata_sha256) "
                             "VALUES (%s,%s,%s,%s,%s,%s)",
                             (run_key, prepared.run_id, prepared.dataset, bundle.bundle_fingerprint,
                              Jsonb(prepared.run_metadata), run_hash))
            conn.execute("INSERT INTO claims2_imports(import_key,run_key,audit_manifest_sha256,audit_report_sha256,review_manifest_sha256) "
                         "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                         (import_key, run_key, prepared.audit_manifest_sha256, prepared.audit_report_sha256,
                          prepared.review_manifest_sha256))
            for item in prepared.publications:
                fingerprint, exists = existing_results[item["candidate_key"]]
                review_key, review_exists = existing_reviews[item["candidate_key"]]
                if review_exists:
                    summary["unchanged"] += 1
                    continue
                evidence = item["evidence"]
                if not exists:
                    conn.execute("INSERT INTO claims2_results(candidate_key,run_key,import_key,record_id,version_id,body_hash,"
                                 "nc_id,sc_id,start_char,end_char,quote,payload,payload_sha256) "
                                 "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                 (item["candidate_key"], run_key, import_key, item["record_id"], item["version_id"],
                                  item["body_hash"], item["nc_id"], item["sc_id"], evidence["start"], evidence["end"],
                                  evidence["quote"], Jsonb(_identity(item)), fingerprint))
                    summary["inserted"] += 1
                conn.execute("INSERT INTO claims2_reviews(review_key,candidate_key,import_key,review_state,payload,payload_sha256) "
                             "VALUES (%s,%s,%s,%s,%s,%s)",
                             (review_key, item["candidate_key"], import_key, item["review_state"], Jsonb(item), review_key))
                summary["review_revisions"] += 1
            summary.update(publication_applied=True, run_key=run_key, import_key=import_key,
                           source_checks_passed=len(prepared.publications))
        return summary

    def retract(self, candidate_key, *, reviewer, reason, reviewed_at):
        if not re.fullmatch(r"[0-9a-f]{64}", candidate_key) or not reviewer.strip() or not reason.strip():
            raise ValueError("Retraction requires a candidate key, reviewer and reason")
        moment = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        if moment.utcoffset() is None:
            raise ValueError("Retraction time must include a timezone")
        event_key = _hash({"candidate_key": candidate_key, "reviewer": reviewer, "reason": reason,
                           "reviewed_at": moment.isoformat()})
        with self.db.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(54901)")
            if not self._available(conn) or not conn.execute(
                "SELECT 1 FROM claims2_results WHERE candidate_key=%s", (candidate_key,),
            ).fetchone():
                raise ValueError("Cannot retract an unknown published candidate")
            row = conn.execute("INSERT INTO claims2_retractions(event_key,candidate_key,reviewer,reason,reviewed_at) "
                               "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING event_key",
                               (event_key, candidate_key, reviewer, reason, moment)).fetchone()
        return {"event_key": event_key, "recorded": row is not None, "retracted": True, "model_calls": 0}

    @staticmethod
    def _version(conn):
        results = conn.execute("SELECT c.candidate_key,c.payload_sha256,r.current_version,r.active "
                               "FROM claims2_results c JOIN records r USING(record_id) ORDER BY c.candidate_key").fetchall()
        events = conn.execute("SELECT event_key FROM claims2_retractions ORDER BY event_key").fetchall()
        reviews = conn.execute("SELECT DISTINCT ON(candidate_key) candidate_key,review_key "
                               "FROM claims2_reviews ORDER BY candidate_key,revision DESC").fetchall()
        return _hash({"schema_version": 1, "results": results, "reviews": reviews, "retractions": events})

    def matches(self, filters: Filters, *, nc_ids=None, sc_ids=None, taxonomy=None,
                review_state=None, offset=0, limit=20):
        offset, limit = self.db._page_bounds(offset, limit)
        where, params = self.db.where(filters)
        conditions = [where, "c.body_hash=v.body_hash", "c.version_id=r.current_version",
                      "(v.payload->>'retrievable')::boolean",
                      "substring(v.body FROM c.start_char+1 FOR c.end_char-c.start_char)=c.quote",
                      "NOT EXISTS (SELECT 1 FROM claims2_retractions x WHERE x.candidate_key=c.candidate_key)"]
        for field, values, prefix in (("nc_id", nc_ids, "NC"), ("sc_id", sc_ids, "SC")):
            if values:
                if not isinstance(values, list) or not all(isinstance(value, str) and re.fullmatch(prefix + r"_[1-9][0-9]*", value) for value in values):
                    raise ValueError("Select valid CLAIMS2 category IDs")
                conditions.append(f"c.{field}=ANY(%s)")
                params.append(values)
        if taxonomy:
            if not re.fullmatch(r"[0-9a-f]{64}", taxonomy):
                raise ValueError("Select a taxonomy fingerprint")
            conditions.append("run.bundle_fingerprint=%s")
            params.append(taxonomy)
        if review_state:
            if review_state not in {"automatic_unverified", "human_supported"}:
                raise ValueError("Select a supported review state")
            conditions.append("review.review_state=%s")
            params.append(review_state)
        join = ("FROM claims2_results c JOIN records r USING(record_id) "
                "JOIN record_versions v ON v.version_id=c.version_id "
                "JOIN LATERAL (SELECT review_state,review_key FROM claims2_reviews "
                "WHERE candidate_key=c.candidate_key ORDER BY revision DESC LIMIT 1) review ON true "
                "JOIN claims2_runs run USING(run_key) "
                "JOIN claims2_taxonomies tax ON tax.bundle_fingerprint=run.bundle_fingerprint")
        query = join + " WHERE " + " AND ".join(conditions)
        with self.db.connect() as conn:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if not self._available(conn):
                return {"available": False, "claims_version": None, "total_records": 0,
                        "total_matches": 0, "records": [], "offset": offset, "limit": limit}
            version = self._version(conn)
            totals = conn.execute("SELECT count(DISTINCT c.record_id) AS total_records,count(*) AS total_matches " + query, params).fetchone()
            selected = conn.execute("SELECT DISTINCT c.record_id " + query + " ORDER BY c.record_id LIMIT %s OFFSET %s",
                                    params + [limit, offset]).fetchall()
            ids = [row["record_id"] for row in selected]
            rows = []
            if ids:
                rows = conn.execute("SELECT c.candidate_key,c.record_id,c.version_id,c.nc_id,c.sc_id,review.review_state,"
                                    "review.review_key AS review_version,"
                                    "c.quote,c.start_char AS start,c.end_char AS end,run.upstream_run_id AS run_id,"
                                    "run.bundle_fingerprint AS taxonomy_version,"
                                    "tax.payload->'subclaims'->>c.nc_id AS nc_definition,"
                                    "tax.payload->'superclaims'->>c.sc_id AS sc_definition,"
                                    "v.payload->>'title' AS title,v.payload->>'publisher' AS publisher,"
                                    "v.payload->>'sponsor' AS sponsor,v.payload->>'url' AS url,r.dataset "
                                    + query + " AND c.record_id=ANY(%s) ORDER BY c.record_id,c.candidate_key",
                                    params + [ids]).fetchall()
        grouped = {key: {"record_id": key, "claims": []} for key in ids}
        for row in rows:
            grouped[row["record_id"]]["claims"].append(row)
        return {"available": True, "claims_version": version, **totals,
                "records": list(grouped.values()), "offset": offset, "limit": limit,
                "meaning": "Published taxonomy assignments; not independent fact checking.",
                "coverage": "Records with published matches only; unmatched records are not classified negatives."}
