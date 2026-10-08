"""Small PostgreSQL repository. Every public read follows records.current_version."""

import hashlib
import json
import re

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import (
    Evidence,
    Filters,
    ImportBatch,
    validate_historical_label_scope,
    validate_postgres_text,
)
from .social_annotation_sql import (
    social_annotation_projection_sql,
    social_source_snapshot_sql,
)
from .social_annotations import (
    NOTE as SOCIAL_LABEL_NOTE,
)
from .social_annotations import (
    SCHEME as SOCIAL_LABEL_SCHEME,
)
from .social_annotations import (
    STATUS as SOCIAL_LABEL_STATUS,
)
from .social_annotations import (
    social_label_metadata,
    social_state_id,
    social_state_options,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ImportPreconditionFailed(ValueError):
    """A reviewed update no longer targets the active source it was bound to."""


def _guarded_import_inputs(batch, snapshot_dataset, expected_current):
    """Freeze reviewed input and its complete old-source bindings before locking."""
    if snapshot_dataset is not None:
        raise ValueError("Guarded updates cannot replace a dataset snapshot")
    if not isinstance(expected_current, dict):
        raise ValueError("Expected current sources must be a dictionary")
    frozen = batch.model_copy(deep=True)
    identifiers = [record.record_id for record in frozen.records]
    if (any(not isinstance(value, str) or not value for value in identifiers)
            or len(set(identifiers)) != len(identifiers)
            or set(expected_current) != set(identifiers)):
        raise ValueError("Expected current sources must exactly cover unique batch record IDs")
    fields = {"version_id", "body_sha256", "url", "dataset"}
    bindings, targets = {}, {}
    for record in frozen.records:
        supplied = expected_current[record.record_id]
        if (not isinstance(supplied, dict) or set(supplied) != fields
                or any(not isinstance(supplied[key], str) for key in fields)
                or any(not re.fullmatch(r"[0-9a-f]{64}", supplied[key])
                       for key in ("version_id", "body_sha256"))
                or supplied["dataset"] not in ("native", "social")
                or supplied["dataset"] != record.dataset):
            raise ValueError("Invalid expected current source binding")
        bindings[record.record_id] = dict(supplied)
        payload = record.model_dump(mode="json")
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        targets[record.record_id] = {
            "payload": payload, "version_id": digest(serialized), "body_sha256": digest(record.body),
        }
    return frozen, bindings, targets


def _check_current_import_sources(conn, bindings, targets):
    """Lock every target and reject the entire batch before its first mutation.

    An exact target version already present is an idempotent retry, never a
    permission to revive withdrawn records or accept a different payload.
    """
    rows = conn.execute(
        "SELECT r.record_id,r.dataset,r.active,r.current_version,v.body_hash,v.body,v.payload "
        "FROM records r JOIN record_versions v "
        "ON v.version_id=r.current_version AND v.record_id=r.record_id "
        "WHERE r.record_id=ANY(%s) ORDER BY r.record_id FOR UPDATE OF r,v",
        (sorted(bindings),),
    ).fetchall()
    actual = {row["record_id"]: row for row in rows}
    if len(actual) != len(rows) or set(actual) != set(bindings):
        raise ImportPreconditionFailed("Reviewed import targets are missing or changed")
    already_applied = 0
    for record_id, expected in bindings.items():
        row, target = actual[record_id], targets[record_id]
        payload = row["payload"]
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if (row["active"] is not True or not isinstance(payload, dict)
                or payload.get("record_id") != record_id or payload.get("dataset") != row["dataset"]
                or payload.get("body") != row["body"] or digest(row["body"]) != row["body_hash"]
                or digest(serialized) != row["current_version"]):
            raise ImportPreconditionFailed("Reviewed import targets are missing or changed")
        original_matches = (
            row["current_version"] == expected["version_id"]
            and row["body_hash"] == expected["body_sha256"]
            and row["dataset"] == expected["dataset"] and payload.get("url") == expected["url"]
        )
        applied_matches = (
            row["current_version"] == target["version_id"]
            and row["body_hash"] == target["body_sha256"]
            and row["dataset"] == target["payload"]["dataset"]
            and payload.get("url") == target["payload"]["url"]
        )
        if not original_matches and not applied_matches:
            raise ImportPreconditionFailed("Reviewed import targets are missing or changed")
        already_applied += int(applied_matches and not original_matches)
    return already_applied


# Supplemented dates need no manual review; a row someone rejects is ignored.
# A web-search date (tier C) is used only when its evidence page is on the same
# site as the advertisement, checked automatically when the row is created.
USABLE_INFERENCE = ("(d.review_state<>'rejected' AND (d.tier<>'C' OR d.review_state='accepted' "
                    "OR d.evidence->>'host_match'='true'))")
INFERRED_DATE = ("(SELECT d.inferred_date FROM date_inferences d WHERE d.version_id=v.version_id "
                 f"AND {USABLE_INFERENCE} AND d.precision='day' ORDER BY d.tier,d.method LIMIT 1)")


class Database:
    def __init__(self, url: str):
        self.url = url

    def connect(self, vector=False):
        if not self.url:
            raise RuntimeError("OBS_DATABASE_URL is not configured")
        conn = psycopg.connect(self.url, row_factory=dict_row, connect_timeout=5)
        if vector:
            register_vector(conn)
        return conn

    def initialize(self):
        from .indexing import bootstrap
        from .migrations import run_migrations

        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(54901)")
            run_migrations(conn)
            bootstrap(conn)

    def import_batch(
        self, batch: ImportBatch, snapshot_dataset=None, *,
        expected_current: dict[str, dict[str, str]] | None = None,
    ):
        from .chunking import retrieval_spans
        from .indexing import (
            SOCIAL_OBSERVATION_PROFILE,
            active_profile,
            record_preparation,
            store_record_chunks,
        )
        from .social_source_retrieval import source_policy_enabled

        # RecordInput can be mutated after construction. Validate the complete
        # batch before any connection or write, including report-only fields.
        for record in batch.records:
            validate_postgres_text(record.model_dump())
        validate_postgres_text([item.model_dump() for item in batch.rejected])
        validate_postgres_text(batch.candidates)
        validate_postgres_text(batch.source_hashes)
        bindings, targets = None, None
        if expected_current is not None:
            batch, bindings, targets = _guarded_import_inputs(batch, snapshot_dataset, expected_current)
        report = {
            "input_records": len(batch.records),
            "new_versions": 0,
            "unchanged": 0,
            "rejected": [r.model_dump() for r in batch.rejected],
            "candidates": batch.candidates,
            "source_hashes": batch.source_hashes,
            "issues": [],
            "deactivated": [],
        }
        if snapshot_dataset is not None:
            if (
                snapshot_dataset not in ("native", "social")
                or not batch.records
                or any(r.dataset != snapshot_dataset for r in batch.records)
            ):
                raise ValueError(
                    "A dataset snapshot must contain nonempty records of one explicit dataset"
                )
        with self.connect() as conn:
            # Imports and reads share one atomic publication boundary.
            conn.execute("SELECT pg_advisory_xact_lock(54901)")
            if bindings is not None:
                report["already_applied"] = _check_current_import_sources(conn, bindings, targets)
                report["precondition_checked"] = len(bindings)
            profile_id = active_profile(conn)
            touched_profiles = {profile_id}
            for record in batch.records:
                # Records are mutable after validation; reject invalid edited ranges
                # even when this particular record is not currently retrievable.
                if record.retrieval_ranges is not None:
                    retrieval_spans(record.body, retrieval_ranges=record.retrieval_ranges)
                if targets is None:
                    payload = record.model_dump(mode="json")
                    serialized = json.dumps(
                        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    version = digest(serialized)
                else:
                    payload = targets[record.record_id]["payload"]
                    version = targets[record.record_id]["version_id"]
                record_profile = (SOCIAL_OBSERVATION_PROFILE
                                  if source_policy_enabled(payload) else profile_id)
                touched_profiles.add(record_profile)
                current = conn.execute(
                    "SELECT current_version,dataset FROM records WHERE record_id=%s",
                    (record.record_id,),
                ).fetchone()
                if current and current["dataset"] != record.dataset:
                    raise ValueError("A record ID cannot change datasets")
                report["issues"].extend(
                    {"record_id": record.record_id, **i.model_dump()}
                    for i in record.issues
                )
                if current and current["current_version"] == version:
                    conn.execute(
                        "UPDATE records SET active=true WHERE record_id=%s",
                        (record.record_id,),
                    )
                    store_record_chunks(conn, record_profile, {
                        "record_id": record.record_id, "version_id": version,
                        "body": record.body, "payload": payload,
                    })
                    report["unchanged"] += 1
                    continue
                conn.execute(
                    "INSERT INTO records(record_id,dataset,current_version) VALUES (%s,%s,%s) ON CONFLICT(record_id) DO UPDATE SET current_version=excluded.current_version,active=true",
                    (record.record_id, record.dataset, version),
                )
                conn.execute(
                    "INSERT INTO record_versions(version_id,record_id,body,body_hash,payload) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        version,
                        record.record_id,
                        record.body,
                        digest(record.body),
                        Jsonb(payload),
                    ),
                )
                for i, ann in enumerate(record.annotations):
                    conn.execute(
                        "INSERT INTO annotations VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        (version, i, Jsonb(ann)),
                    )
                store_record_chunks(conn, record_profile, {
                    "record_id": record.record_id, "version_id": version,
                    "body": record.body, "payload": payload,
                })
                report["new_versions"] += 1
            if snapshot_dataset:
                retired = conn.execute(
                    "UPDATE records SET active=false WHERE dataset=%s AND active AND NOT (record_id=ANY(%s)) RETURNING record_id",
                    (snapshot_dataset, [r.record_id for r in batch.records]),
                ).fetchall()
                report["deactivated"] = [r["record_id"] for r in retired]
            for prepared_profile in sorted(touched_profiles):
                record_preparation(conn, prepared_profile)
            report["index_profile"] = profile_id
            conn.execute("INSERT INTO imports(report) VALUES (%s)", (Jsonb(report),))
        return report

    @staticmethod
    def where(filters: Filters):
        # model_copy and direct assignment can bypass the model validator.
        validate_historical_label_scope(filters.dataset, filters.labels)
        if filters.accounts and filters.dataset != "social":
            raise ValueError("Account filters require the social collection")
        terms = ["r.active", "(v.payload->>'countable')::boolean"]
        params = []
        if filters.dataset != "all":
            terms.append("r.dataset=%s")
            params.append(filters.dataset)
        if filters.record_ids:
            terms.append("r.record_id=ANY(%s)")
            params.append(filters.record_ids)
        for field, values in [
            ("publisher", filters.publishers),
            ("sponsor", filters.sponsors),
            ("platform", filters.platforms),
            ("account", filters.accounts),
            ("keyword", filters.keywords),
        ]:
            if values:
                terms.append(
                    f"COALESCE(NULLIF(v.payload->>'{field}',''),'(Unknown)')=ANY(%s)"
                )
                params.append(values)
        # Source date, or (only when requested) an unreviewed day-precision inference.
        published = (f"COALESCE(NULLIF(v.payload->>'published_at','')::date,{INFERRED_DATE})"
                     if filters.include_inferred_dates else "NULLIF(v.payload->>'published_at','')::date")
        if filters.date_presence == "known":
            terms.append(f"{published} IS NOT NULL")
        elif filters.date_presence == "missing":
            terms.append(f"{published} IS NULL")
        dates = []
        if filters.date_from:
            dates.append(f"{published}>=%s")
            params.append(filters.date_from)
        if filters.date_to:
            dates.append(f"{published}<=%s")
            params.append(filters.date_to)
        if dates:
            expression = " AND ".join(dates)
            if filters.include_unknown_dates:
                expression = f"(({expression}) OR {published} IS NULL)"
            terms.append(f"({expression})")
        elif not filters.include_unknown_dates:
            terms.append(f"{published} IS NOT NULL")
        if filters.labels:
            if filters.dataset == "social":
                terms.append(f"({social_annotation_projection_sql()}->'states') ?| %s")
            else:
                terms.append(
                    "r.dataset='native' AND EXISTS (SELECT 1 FROM annotations a WHERE a.version_id=v.version_id AND a.payload->>'version'='claims-calibrated' AND jsonb_typeof(a.payload->'labels')='array' AND (a.payload->'labels') ?| %s)"
                )
            params.append(filters.labels)
        return " AND ".join(terms), params

    def _public_query(self, filters: Filters):
        where, params = self.where(filters)
        source_date = "NULLIF(v.payload->>'published_at','')"
        effective_date = (f"COALESCE({source_date},di.inferred_date::text)"
                          if filters.include_inferred_dates else source_date)
        inferred_enabled = "true" if filters.include_inferred_dates else "false"
        social_states = (
            f"CASE WHEN r.dataset='social' THEN {social_annotation_projection_sql()}->'states' "
            "ELSE '[]'::jsonb END" if filters.dataset != "native" else "'[]'::jsonb"
        )
        # Explicit projection prevents raw data, disclosure and local paths escaping.
        sql = f"""SELECT r.record_id,r.dataset,v.version_id,
         v.payload->>'url' AS url,v.payload->>'archive_url' AS archive_url,
         v.payload->>'publisher' AS publisher,v.payload->>'title' AS title,
         v.payload->>'published_at' AS date,v.payload->>'sponsor' AS sponsor,
         {source_date} AS source_date,{effective_date} AS effective_date,
         v.payload->>'keyword' AS keyword,v.payload->>'platform' AS platform,
         v.payload->>'account' AS account,(v.payload->>'retrievable')::boolean AS retrievable,
         CASE WHEN r.dataset='social' AND v.payload#>>'{{raw,social_admission,scheme}}'
                   ='collected-company-posts-unique-url-v1'
                   AND v.payload#>>'{{raw,social_admission,scope}}'='collected_company_posts'
              THEN 'collected_company_posts'
              WHEN r.dataset='social' THEN 'unknown' END AS collection_scope,
         CASE WHEN r.dataset='social' AND v.payload#>>'{{raw,social_admission,scheme}}'
                   ='collected-company-posts-unique-url-v1'
                   AND v.payload#>>'{{raw,social_admission,scope}}'='collected_company_posts'
                   AND v.payload#>>'{{raw,social_admission,count_unit}}'='platform_canonical_original_post_url'
              THEN 'platform_canonical_original_post_url'
              WHEN r.dataset='social' THEN 'source_record' END AS count_unit,
         CASE WHEN r.dataset='social' THEN 'not_verified' END AS paid_ad_status,
         CASE WHEN NULLIF(v.payload->>'published_at','') IS NOT NULL THEN 'source'
              WHEN {inferred_enabled} AND di.method IS NOT NULL THEN 'inferred:' || di.method ELSE 'missing' END AS date_basis,
         di.inferred_date::text AS inferred_date,di.tier AS inferred_tier,
         CASE WHEN r.dataset='native' THEN COALESCE((
             SELECT jsonb_agg(to_jsonb(label) ORDER BY label COLLATE "C") FROM (
                 SELECT DISTINCT item #>> '{{}}' AS label FROM annotations a
                 CROSS JOIN LATERAL jsonb_array_elements(CASE
                     WHEN jsonb_typeof(a.payload->'labels')='array' THEN a.payload->'labels'
                     ELSE '[]'::jsonb END) AS item
                 WHERE a.version_id=v.version_id AND a.payload->>'version'='claims-calibrated'
                     AND jsonb_typeof(item)='string'
             ) AS native_labels
         ),'[]'::jsonb) ELSE '[]'::jsonb END AS labels,
         {social_states} AS social_historical_states,
         CASE WHEN r.dataset='social' THEN '{SOCIAL_LABEL_SCHEME}' END AS social_historical_scheme,
         CASE WHEN r.dataset='social' THEN '{SOCIAL_LABEL_STATUS}' END AS social_historical_status
         FROM records r JOIN record_versions v ON v.version_id=r.current_version
         LEFT JOIN LATERAL (SELECT d.method,d.tier,d.inferred_date FROM date_inferences d
              WHERE d.version_id=v.version_id AND {USABLE_INFERENCE}
                AND d.precision='day'
                AND NULLIF(v.payload->>'published_at','') IS NULL
              ORDER BY d.tier,d.method LIMIT 1) di ON true
         WHERE {where}"""
        return sql, params

    def public_rows(self, filters: Filters):
        sql, params = self._public_query(filters)
        with self.connect() as conn:
            return conn.execute(sql + " ORDER BY date DESC NULLS LAST,record_id", params).fetchall()

    def original_record_metadata(self, filters: Filters, record_id: str):
        """Read whitelisted original cells for exactly one current scoped row.

        Original Excel cells and cleaned source copies keep separate origins.
        The SQL projection never selects a complete raw row or source path.
        """
        from .original_metadata import metadata_origins_sql

        if filters.record_ids and record_id not in filters.record_ids:
            return None
        scoped = filters.model_copy(deep=True)
        scoped.record_ids = [record_id]
        public_sql, params = self._public_query(scoped)
        sql = f"""SELECT p.*,v.body_hash,
            v.payload->>'disclosure' AS display_disclosure,
            {metadata_origins_sql()} AS metadata_origins,
            CASE WHEN p.dataset='social'
                THEN v.payload#>'{{raw,social_admission,conflicting_fields}}'
                ELSE '[]'::jsonb END AS metadata_conflicting_fields
            FROM ({public_sql}) p JOIN record_versions v
                ON v.version_id=p.version_id AND v.record_id=p.record_id"""
        with self.connect() as conn:
            return conn.execute(sql, params).fetchone()

    def find_records(self, filters: Filters, title: str, limit=10):
        """Prefer exact stored titles; return all ambiguity within a bounded page.

        Matching is literal and case insensitive. SQL wildcards have no special
        meaning, and no source body or classification is consulted.
        """
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise ValueError("A bounded literal title is required")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("Title candidate limit must be between one and ten")
        public_sql, params = self._public_query(filters)
        original_title = (
            "CASE WHEN jsonb_typeof(v.payload#>'{raw,metadata,title}')='string' "
            "THEN v.payload#>>'{raw,metadata,title}' "
            "WHEN jsonb_typeof(v.payload#>'{raw,metadata,Title}')='string' "
            "THEN v.payload#>>'{raw,metadata,Title}' ELSE '' END"
        )
        sql = f"""WITH scoped AS (
            SELECT p.*,v.body_hash,{original_title} AS original_title
            FROM ({public_sql}) p JOIN record_versions v
                ON v.version_id=p.version_id AND v.record_id=p.record_id
            ), candidates AS (
            SELECT *,CASE WHEN lower(btrim(COALESCE(title,'')))=lower(%s)
                OR lower(btrim(original_title))=lower(%s) THEN 'exact'
                ELSE 'literal_contains' END AS title_match
            FROM scoped WHERE strpos(lower(COALESCE(title,'')),lower(%s))>0
                OR strpos(lower(original_title),lower(%s))>0
            ), preferred AS (
            SELECT * FROM candidates WHERE title_match='exact'
                OR NOT EXISTS (SELECT 1 FROM candidates WHERE title_match='exact')
            ) SELECT *,count(*) OVER() AS candidate_count FROM preferred
            ORDER BY title,record_id LIMIT %s"""
        query = title.strip()
        with self.connect() as conn:
            rows = conn.execute(sql, [*params, query, query, query, query, limit]).fetchall()
        return {"rows": rows, "total_candidates": rows[0]["candidate_count"] if rows else 0,
                "match_type": rows[0]["title_match"] if rows else None}

    def versioned_record(self, filters: Filters, record_id: str):
        """Read one current native/social source without widening trusted filters.

        A single statement binds the public metadata, body and annotations to
        the same version. Only known affiliation/admission fields are read
        from raw; private import payloads and source paths are never projected.
        """
        if filters.record_ids and record_id not in filters.record_ids:
            return None
        scoped = filters.model_copy(deep=True)
        scoped.record_ids = [record_id]
        public_sql, params = self._public_query(scoped)
        sql = f"""SELECT p.*,v.body,v.body_hash,v.created_at,
            (v.payload->'retrieval_ranges') AS retrieval_ranges,
            (v.payload->>'retrieval_end')::integer AS retrieval_end,
            COALESCE((SELECT jsonb_agg(jsonb_build_object('code',i->>'code'))
                FROM jsonb_array_elements(v.payload->'issues') i),'[]'::jsonb) AS issues,
            COALESCE((SELECT jsonb_agg(jsonb_build_object('ordinal',a.ordinal,
                'payload',a.payload) ORDER BY a.ordinal)
                FROM annotations a WHERE a.version_id=p.version_id),'[]'::jsonb) AS annotations,
            CASE WHEN p.dataset='social' AND v.payload#>>'{{raw,sponsor_basis}}'
                ='company_affiliation_not_verified_paid_sponsor'
                THEN 'company_affiliation_not_verified_paid_sponsor'
                ELSE 'not_recorded' END AS sponsor_basis,
            CASE WHEN p.dataset='social' THEN jsonb_build_object(
                'scheme',v.payload#>>'{{raw,social_admission,scheme}}',
                'scope',v.payload#>>'{{raw,social_admission,scope}}',
                'count_unit',v.payload#>>'{{raw,social_admission,count_unit}}',
                'paid_ad_status',v.payload#>>'{{raw,social_admission,paid_ad_status}}',
                'member_count',v.payload#>'{{raw,social_admission,member_count}}',
                'conflicting_fields',v.payload#>'{{raw,social_admission,conflicting_fields}}',
                'retrieval_status',v.payload#>>'{{raw,social_admission,retrieval_status}}')
                END AS social_admission,
            CASE WHEN p.dataset='social' THEN v.payload END AS observation_payload
            FROM ({public_sql}) p JOIN record_versions v
                ON v.version_id=p.version_id AND v.record_id=p.record_id"""
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        if row:
            from .social_source_binding import public_observations
            from .social_source_retrieval import source_version_id

            payload = row.pop("observation_payload", None)
            if (isinstance(payload, dict) and payload.get("retrievable") is True
                    and isinstance(payload.get("raw"), dict)
                    and "social_admission" in payload["raw"]):
                if source_version_id(payload) != row["version_id"]:
                    raise ValueError("Current source payload version does not match")
                row["social_source_observations"] = public_observations(payload)
                if payload["raw"].get("social_source_retrieval"):
                    row["social_admission"] = {**row["social_admission"],
                        "previous_retrieval_status": row["social_admission"].get("retrieval_status"),
                        "retrieval_status": "enabled_source_observations"}
        return row

    def claims_source_candidates(self, excerpt, limit=5, *, filters=None):
        """Find literal legacy excerpts without declaring an article identity.

        Search current eligible native text only. Accepted source intervals
        matter here too: a navigation match cannot establish source evidence.
        At most 100 matching articles are inspected and five returned; any
        incomplete scan is explicit. No normalization or model call occurs.
        """
        from .chunking import retrieval_spans

        if not isinstance(excerpt, str) or not 40 <= len(excerpt) <= 2000 or not excerpt.strip():
            raise ValueError("Use an original excerpt of 40–2000 characters")
        _, limit = self._page_bounds(0, limit)
        limit = min(limit, 5)
        filters = filters or Filters(dataset="native")
        if filters.dataset != "native":
            raise ValueError("Legacy CLAIMS source discovery uses the native collection")
        where, params = self.where(filters)
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            rows = conn.execute(
                "SELECT r.record_id,v.version_id,v.body,v.body_hash,v.payload,"
                "count(*) OVER() AS literal_record_matches "
                "FROM records r JOIN record_versions v ON v.version_id=r.current_version WHERE "
                + where + " AND (v.payload->>'retrievable')::boolean AND strpos(v.body,%s)>0 "
                "ORDER BY r.record_id LIMIT 100", [*params, excerpt],
            ).fetchall()
        candidates, scanned = [], 0
        for row in rows:
            scanned += 1
            if digest(row["body"]) != row["body_hash"]:
                continue
            payload = row["payload"]
            spans = retrieval_spans(row["body"], retrieval_ranges=payload.get("retrieval_ranges"),
                                    retrieval_end=payload.get("retrieval_end"))
            start = row["body"].find(excerpt)
            accepted, location_count = [], 0
            while start >= 0:
                end = start + len(excerpt)
                if any(left <= start < end <= right for left, right in spans):
                    location_count += 1
                    if len(accepted) < 10:
                        accepted.append({"start": start, "end": end})
                start = row["body"].find(excerpt, start + 1)
            if accepted:
                candidates.append({
                    "source_kind": "local_current_article", "source_association": "needs_review",
                    "record_id": row["record_id"], "version_id": row["version_id"], "body_hash": row["body_hash"],
                    "url": payload.get("url", ""), "title": payload.get("title", ""),
                    "publisher": payload.get("publisher", ""), "quote": excerpt,
                    "locations": accepted, "location_count": location_count,
                    "locations_complete": location_count <= 10, "location_status": "exact_original_characters",
                })
            if len(candidates) >= limit:
                break
        total = rows[0]["literal_record_matches"] if rows else 0
        return {"status": "ok", "candidates": candidates, "literal_record_matches": total,
                "scanned_records": scanned, "scan_complete": total <= scanned, "candidate_limit": limit,
                "filters": filters.model_dump(mode="json"),
                "meaning": "Text matches propose article identities; source association still needs review."}

    def knowledge_map_rows(self, filters: Filters):
        """Complete filtered graph identities from one read-only SQL snapshot.

        This excludes body, raw imports and annotation payloads. Text and
        materials are inspected through the existing version-bound detail path.
        """
        if filters.dataset != "native":
            raise ValueError("The collection knowledge graph supports native records")
        select, params = self._public_query(filters)
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return conn.execute(
                "SELECT selected.*,v.body_hash FROM (" + select + ") AS selected "
                "JOIN record_versions v ON v.version_id=selected.version_id "
                "ORDER BY selected.date DESC NULLS LAST,selected.record_id",
                params,
            ).fetchall()

    def content_match_sources(self, filters: Filters):
        """Read the whole filtered native denominator and unchanged source text.

        A single read-only statement binds metadata, current version, body and
        permitted intervals. Missing/unsearchable bodies stay in the scope.
        Raw imports, annotation payloads and local file references are omitted.
        """
        if filters.dataset != "native":
            raise ValueError("The client content questions currently cover native records")
        select, params = self._public_query(filters)
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            rows = conn.execute(
                "SELECT selected.*,v.body,v.body_hash,"
                "v.payload->'retrieval_ranges' AS retrieval_ranges,"
                "(v.payload->>'retrieval_end')::integer AS retrieval_end "
                "FROM (" + select + ") AS selected "
                "JOIN record_versions v ON v.version_id=selected.version_id "
                "AND v.record_id=selected.record_id "
                "ORDER BY selected.record_id LIMIT 10001", params,
            ).fetchall()
        if len(rows) > 10000:
            raise ValueError("Content review scope exceeds the supported source bound")
        return rows

    @staticmethod
    def _page_bounds(offset, limit):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Offset must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Limit must be a positive integer")
        return offset, min(limit, 100)

    def public_page(self, filters: Filters, offset=0, limit=20, sort_by="date", descending=True):
        """Return one bounded page and a matching count from the same snapshot."""
        offset, limit = self._page_bounds(offset, limit)
        order = self._page_order(sort_by, descending)
        select, params = self._public_query(filters)
        sql = (
            f"WITH filtered AS ({select}) SELECT * FROM filtered "
            f"ORDER BY {order} LIMIT %s OFFSET %s"
        )
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            total = conn.execute(
                f"SELECT count(*) AS total FROM ({select}) AS filtered", params
            ).fetchone()["total"]
            offset = min(offset, max(0, ((total - 1) // limit) * limit))
            rows = conn.execute(sql, [*params, limit, offset]).fetchall()
        return {"rows": rows, "total": total, "offset": offset}

    def knowledge_page(self, filters: Filters, offset=0, limit=5):
        """Internal graph input, bound to current versions in one read snapshot.

        Body and annotation payloads are for validation by the graph builder,
        never a browser response. Raw imports and private provenance stay here.
        """
        if filters.dataset != "native":
            raise ValueError("The knowledge graph currently supports the native collection")
        offset, limit = self._page_bounds(offset, limit)
        limit = min(limit, 20)
        where, params = self.where(filters)
        base = f"""FROM records r JOIN record_versions v ON v.version_id=r.current_version
            WHERE {where}"""
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            total = conn.execute("SELECT count(*) AS total " + base, params).fetchone()["total"]
            offset = min(offset, max(0, ((total - 1) // limit) * limit))
            rows = conn.execute("""SELECT r.record_id,r.dataset,v.version_id,
                v.body,v.body_hash,v.created_at,
                v.payload->>'title' AS title,v.payload->>'publisher' AS publisher,
                v.payload->>'sponsor' AS sponsor,v.payload->>'published_at' AS date,
                v.payload->>'url' AS url,v.payload->>'archive_url' AS archive_url,
                (v.payload->>'retrievable')::boolean AS retrievable,
                COALESCE((SELECT jsonb_agg(jsonb_build_object('code',i->>'code'))
                    FROM jsonb_array_elements(v.payload->'issues') i),'[]'::jsonb) AS issues,
                COALESCE((SELECT jsonb_agg(jsonb_build_object('ordinal',a.ordinal,
                    'payload',a.payload) ORDER BY a.ordinal)
                    FROM annotations a WHERE a.version_id=v.version_id),'[]'::jsonb) AS annotations
                """ + base + " ORDER BY (v.payload->>'published_at') DESC NULLS LAST,r.record_id LIMIT %s OFFSET %s",
                [*params, limit, offset],
            ).fetchall()
            from .claims_store import ClaimsStore

            # The source page and its derived assignments must describe the
            # same snapshot, including a concurrent retraction or body update.
            selected = filters.model_copy(update={"record_ids": [row["record_id"] for row in rows]}) if rows else filters
            claims2 = ClaimsStore(self).matches(selected, limit=20, _connection=conn)
        return {"rows": rows, "total": total, "offset": offset, "limit": limit, "claims2": claims2}

    @staticmethod
    def _page_order(sort_by, descending):
        columns = {
            "date", "title", "sponsor", "publisher", "platform", "account",
            "keyword", "record_id", "dataset", "retrievable",
        }
        if sort_by not in columns:
            raise ValueError("Unsupported sort column")
        if not isinstance(descending, bool):
            raise ValueError("Descending must be a boolean")
        direction = "DESC" if descending else "ASC"
        # Only allowlisted identifiers and a fixed direction enter SQL text.
        return f"{sort_by} {direction} NULLS LAST,record_id ASC"

    def source_value_counts(self):
        """Countable current records per collection and exact sponsor/publisher spelling."""
        where, params = self.where(Filters(dataset="all"))
        sql = ("SELECT r.dataset,v.payload->>'sponsor' AS sponsor,v.payload->>'publisher' AS publisher,"
               "count(*) AS n FROM records r JOIN record_versions v ON v.version_id=r.current_version "
               f"WHERE {where} GROUP BY 1,2,3")
        counts = {}
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            for row in conn.execute(sql, params).fetchall():
                for field in ("sponsor", "publisher"):
                    if row[field]:
                        key = (row["dataset"], field, row[field])
                        counts[key] = counts.get(key, 0) + row["n"]
        return counts

    def facets(self, dataset):
        """Fetch distinct filter options, without transferring article rows."""
        select, params = self._public_query(Filters(dataset=dataset))
        # This CTE is used twice and is materialized. Keep only facet fields
        # so PostgreSQL can prune body-bound social annotation validation and
        # other unused public fields before materializing every record.
        select = ("SELECT dataset,publisher,sponsor,platform,account,keyword,labels "
                  f"FROM ({select}) AS facet_rows")
        sql = f"""WITH filtered AS ({select}), options AS (
            SELECT option.name,COALESCE(NULLIF(option.value,''),'(Unknown)') AS value
            FROM filtered CROSS JOIN LATERAL (VALUES
                ('publishers',publisher),('sponsors',sponsor),
                ('platforms',platform),('accounts',account),('keywords',keyword)
            ) AS option(name,value)
            WHERE option.name<>'accounts' OR dataset='social'
            UNION
            SELECT 'labels',label FROM filtered
            CROSS JOIN LATERAL jsonb_array_elements_text(labels) AS label
        ) SELECT name,value FROM (SELECT DISTINCT name,value FROM options) AS distinct_options
        ORDER BY name,value COLLATE "C" """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = {name: [] for name in ("publishers", "sponsors", "platforms", "accounts", "keywords", "labels")}
        for row in rows:
            result[row["name"]].append(row["value"])
        if dataset == "social":
            result["labels"] = [item["value"] for item in social_state_options()]
        return result

    def social_source_state_version(self, filters: Filters):
        """Read the full social source-state version, without rows or private data."""
        if filters.dataset != "social":
            raise ValueError("Historical social source versions require the social collection")
        select, params = self._public_query(filters)
        with self.connect() as conn:
            return conn.execute(social_source_snapshot_sql(select), params).fetchone()["source_state_version"]

    @staticmethod
    def _social_historical_labels(conn, select, params):
        snapshot = dict(conn.execute(
            social_source_snapshot_sql(select, include_counts=True), params
        ).fetchone())
        counts = snapshot.pop("state_counts")
        return {
            "scheme": SOCIAL_LABEL_SCHEME, "status": SOCIAL_LABEL_STATUS,
            "note": SOCIAL_LABEL_NOTE, **snapshot,
            "items": [
                {**item, **{state: counts.get(social_state_id(item["key"], state), 0)
                           for state in ("source_true", "source_false", "unknown")}}
                for item in social_label_metadata()
            ],
        }

    def dashboard(self, filters: Filters, offset=0, limit=20, sort_by="date", descending=True):
        """Aggregate in PostgreSQL; all charts share one read-only snapshot."""
        from .analytics import LABEL_NOTE, sponsor_publisher_matrix_from_counts

        offset, limit = self._page_bounds(offset, limit)
        order = self._page_order(sort_by, descending)
        select, params = self._public_query(filters)
        # Share the selected metadata, not the full public projection. Materializing
        # social_historical_states would validate every annotation for each row,
        # even though aggregates do not use that field. The full projection is
        # inlined only where needed and joined to the already bounded page IDs.
        sql = f"""WITH public_source AS NOT MATERIALIZED ({select}),
            filtered AS MATERIALIZED (
                SELECT record_id,dataset,version_id,date,title,publisher,sponsor,
                    platform,account,keyword,retrievable,effective_date,date_basis,
                    inferred_tier,labels FROM public_source
            ), totals AS (
                SELECT count(*) AS total,count(*) FILTER (WHERE retrievable) AS retrievable,
                    count(*) FILTER (WHERE effective_date IS NULL OR effective_date='') AS unknown_dates
                FROM filtered
            ), page_bounds AS (
                SELECT LEAST(%s::bigint,GREATEST(0,((total-1)/%s::bigint)*%s::bigint)) AS page_offset
                FROM totals
            ), page_members AS MATERIALIZED (
                SELECT record_id,version_id,row_number() OVER (ORDER BY {order}) AS page_ordinal
                FROM filtered ORDER BY {order}
                LIMIT %s OFFSET (SELECT page_offset FROM page_bounds)
            ), groups AS (
                SELECT option.name,COALESCE(NULLIF(option.value,''),'(Unknown)') AS value,count(*) AS count
                FROM filtered CROSS JOIN LATERAL (VALUES
                    ('publishers',publisher),('sponsors',sponsor),
                    ('platforms',platform),('accounts',account),('keywords',keyword)
                ) AS option(name,value)
                WHERE option.name<>'accounts' OR dataset='social'
                GROUP BY option.name,COALESCE(NULLIF(option.value,''),'(Unknown)')
            ), relationships AS (
                SELECT COALESCE(NULLIF(sponsor,''),'(Unknown)') AS sponsor,
                    COALESCE(NULLIF(publisher,''),'(Unknown)') AS publisher,count(*) AS count
                FROM filtered GROUP BY 1,2
            ), months AS (
                SELECT COALESCE(NULLIF(left(effective_date,7),''),'Unknown') AS month,count(*) AS count
                FROM filtered GROUP BY 1
            ), years AS (
                SELECT COALESCE(NULLIF(left(effective_date,4),''),'Unknown') AS year,count(*) AS count
                FROM filtered GROUP BY 1
            ), label_records AS (
                SELECT DISTINCT record_id,btrim(label) AS name FROM filtered
                CROSS JOIN LATERAL jsonb_array_elements_text(labels) AS label
                WHERE dataset='native' AND btrim(label)<>''
            ), label_counts AS (
                SELECT name,count(*) AS count FROM label_records GROUP BY name
            ), native_labels AS (
                SELECT count(*) AS total,count(*) FILTER (WHERE EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(labels) AS label WHERE btrim(label)<>''
                )) AS count FROM filtered WHERE dataset='native'
            ), inferred AS (
                SELECT inferred_tier AS tier,count(*) AS n FROM filtered
                WHERE date_basis LIKE 'inferred:%%' GROUP BY 1
            ), collection_counts AS (
                SELECT kinds.dataset,count(f.record_id) AS total,
                    count(f.record_id) FILTER (WHERE f.retrievable) AS retrievable,
                    count(f.record_id) FILTER (WHERE f.effective_date IS NULL OR f.effective_date='') AS unknown_dates
                FROM (VALUES ('native'),('social')) AS kinds(dataset)
                LEFT JOIN filtered f ON f.dataset=kinds.dataset GROUP BY kinds.dataset
            ), company_counts AS (
                SELECT COALESCE(NULLIF(sponsor,''),'(Unknown)') AS sponsor,
                    count(*) FILTER (WHERE dataset='native') AS native,
                    count(*) FILTER (WHERE dataset='social') AS social
                FROM filtered GROUP BY 1
            ), collection_years AS (
                SELECT COALESCE(NULLIF(left(effective_date,4),''),'Unknown') AS year,
                    dataset,count(*) AS count FROM filtered GROUP BY 1,2
            ) SELECT totals.*,page_bounds.page_offset,
                COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY m.page_ordinal)
                    FROM page_members m JOIN public_source p USING(record_id,version_id)),
                    '[]'::jsonb) AS page_rows,
                COALESCE((SELECT jsonb_agg(to_jsonb(g) ORDER BY name,count DESC,value COLLATE "C")
                    FROM groups g),'[]'::jsonb) AS groups,
                COALESCE((SELECT jsonb_agg(to_jsonb(r) ORDER BY count DESC,sponsor,publisher)
                    FROM relationships r),'[]'::jsonb) AS relationships,
                COALESCE((SELECT jsonb_agg(to_jsonb(m) ORDER BY month) FROM months m),'[]'::jsonb) AS months,
                COALESCE((SELECT jsonb_agg(to_jsonb(y) ORDER BY year) FROM years y),'[]'::jsonb) AS years,
                COALESCE((SELECT jsonb_agg(to_jsonb(l) ORDER BY count DESC,name COLLATE "C")
                    FROM label_counts l),'[]'::jsonb) AS labels,
                (SELECT to_jsonb(n) FROM native_labels n) AS native_labels,
                COALESCE((SELECT jsonb_agg(to_jsonb(i) ORDER BY tier) FROM inferred i),'[]'::jsonb) AS inferred,
                CASE WHEN %s THEN jsonb_build_object(
                    'collections',(SELECT jsonb_agg(to_jsonb(c) ORDER BY dataset) FROM collection_counts c),
                    'companies',COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY sponsor COLLATE "C") FROM company_counts c),'[]'::jsonb),
                    'timeline',COALESCE((SELECT jsonb_agg(to_jsonb(y) ORDER BY year,dataset) FROM collection_years y),'[]'::jsonb)
                ) END AS combined
            FROM totals CROSS JOIN page_bounds"""
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            # PostgreSQL counts fit bigint; cap an arbitrary Python offset before
            # binding so its old last-page clamping semantics also cover huge ints.
            aggregate = conn.execute(sql, [*params, min(offset, 2**63-1), limit, limit, limit, filters.dataset == "all"]).fetchone()
            social_labels = (
                self._social_historical_labels(conn, select, params)
                if filters.dataset != "native" else None
            )
        totals = {key: aggregate[key] for key in ("total", "retrievable", "unknown_dates")}
        offset, page_rows = aggregate["page_offset"], aggregate["page_rows"]
        groups, relationships = aggregate["groups"], aggregate["relationships"]
        months, years = aggregate["months"], aggregate["years"]
        labels, native_labels, inferred = aggregate["labels"], aggregate["native_labels"], aggregate["inferred"]
        total = totals["total"]
        stats = {**totals, **{name: [] for name in ("publishers", "sponsors", "platforms", "accounts", "keywords")},
                 "relationships": relationships, "timeline": months,
                 "inferred_dates": {row["tier"]: row["n"] for row in inferred}}
        account_total = sum(row["count"] for row in groups if row["name"] == "accounts")
        for row in groups:
            denominator = account_total if row["name"] == "accounts" else total
            stats[row["name"]].append({
                "name": row["value"], "count": row["count"],
                "percent": 100 * row["count"] / denominator if denominator else 0,
            })
        result = {
            "stats": stats, "timeline": years,
            "page": {"rows": page_rows, "total": total, "offset": offset},
            "labels": {
                "items": [{**row, "percent": 100 * row["count"] / native_labels["total"]
                           if native_labels["total"] else 0} for row in labels],
                "total": native_labels["total"], "labeled_records": native_labels["count"],
                "unlabeled_records": native_labels["total"] - native_labels["count"], "note": LABEL_NOTE,
            },
            "matrix": sponsor_publisher_matrix_from_counts(relationships),
        }
        if social_labels is not None:
            result["social_historical_labels"] = social_labels
        if filters.dataset == "all":
            result["combined"] = aggregate["combined"]
        return result

    def research_statistics(self, filters: Filters, *, group_by="years", ranking="all", periods=()):
        """Year groups or named-period counts from one read-only snapshot.

        Source dates remain in ``date``. All date selection, missing counts and
        grouping use the explicitly selected ``effective_date`` projection.
        Period filters are trusted, pre-intersected Filters from the tool layer.
        """
        if group_by not in ("none", "years") or ranking not in ("all", "highest"):
            raise ValueError("Unsupported statistics grouping or ranking")
        if len(periods) > 3:
            raise ValueError("At most three comparison periods are supported")
        datasets = ("native", "social") if filters.dataset == "all" else (filters.dataset,)

        def totals(conn, scope):
            select, params = self._public_query(scope)
            rows = conn.execute(f"WITH filtered AS ({select}) " + """SELECT dataset,
                count(*) AS total,count(*) FILTER (WHERE retrievable) AS retrievable,
                count(*) FILTER (WHERE effective_date IS NULL OR effective_date='') AS unknown_dates,
                count(*) FILTER (WHERE source_date IS NULL OR source_date='') AS source_unknown_dates,
                count(*) FILTER (WHERE date_basis LIKE 'inferred:%%') AS inferred_dates
                FROM filtered GROUP BY dataset ORDER BY dataset""", params).fetchall()
            by_dataset = {row["dataset"]: row for row in rows}
            return [by_dataset.get(dataset, {"dataset": dataset, "total": 0,
                "retrievable": 0, "unknown_dates": 0, "source_unknown_dates": 0,
                "inferred_dates": 0}) for dataset in datasets]

        select, params = self._public_query(filters)
        prefix = f"WITH filtered AS ({select}) "
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            collections = totals(conn, filters)
            groups = []
            if group_by == "years":
                groups = conn.execute(prefix + """, year_counts AS (
                    SELECT dataset,left(effective_date,4) AS name,count(*) AS count
                    FROM filtered WHERE effective_date IS NOT NULL AND effective_date<>''
                    GROUP BY dataset,left(effective_date,4)
                ), ranked AS (
                    SELECT *,dense_rank() OVER (PARTITION BY dataset ORDER BY count DESC) AS position
                    FROM year_counts
                ) SELECT dataset,name,count FROM ranked """ +
                    ("WHERE position=1 " if ranking == "highest" else "") +
                    "ORDER BY dataset,name", params).fetchall()
            comparison = [{"label": period["label"],
                "filters": period["filters"].model_dump(mode="json"),
                "collections": totals(conn, period["filters"])} for period in periods]
            records = conn.execute(prefix + """, examples AS (
                SELECT *,row_number() OVER (PARTITION BY dataset
                    ORDER BY effective_date DESC NULLS LAST,record_id) AS example_position
                FROM filtered
            ) SELECT * FROM examples WHERE example_position<=10
                ORDER BY dataset,example_position""", params).fetchall()
            tiers = conn.execute(prefix + """SELECT inferred_tier AS tier,count(*) AS count
                FROM filtered WHERE date_basis LIKE 'inferred:%%'
                GROUP BY inferred_tier ORDER BY inferred_tier""", params).fetchall()
        return {"collections": collections, "groups": groups, "periods": comparison,
            "records": records, "inferred_tiers": {row["tier"]: row["count"] for row in tiers},
            "filters": filters.model_dump(mode="json"),
            "date_basis": "source_or_supplemented" if filters.include_inferred_dates else "source_only",
            "snapshot": "repeatable_read_read_only"}

    def network(self, filters: Filters, limit=60):
        """Bounded source-listed sponsor/outlet links for native advertisements."""
        if filters.dataset != "native":
            raise ValueError("The prototype network supports native advertisements only")
        _, limit = self._page_bounds(0, limit)
        select, params = self._public_query(filters)
        prefix = f"""WITH filtered AS ({select}), relationships AS (
            SELECT COALESCE(NULLIF(sponsor,''),'(Unknown)') AS sponsor,
                COALESCE(NULLIF(publisher,''),'(Unknown)') AS publisher,count(*) AS count
            FROM filtered GROUP BY 1,2
        ) """
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            totals = conn.execute(prefix + """SELECT count(*) AS total_relationships,
                COALESCE(sum(count),0)::bigint AS total_records FROM relationships""", params).fetchone()
            rows = conn.execute(prefix + """SELECT * FROM relationships
                ORDER BY count DESC,sponsor,publisher LIMIT %s""", [*params, limit]).fetchall()
        return {"relationships": rows, **totals, "truncated": len(rows) < totals["total_relationships"]}

    def health(self):
        from .indexing import profile_snapshot

        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            rows = conn.execute(
                "SELECT r.dataset,count(*) AS n,"
                "count(*) FILTER (WHERE (v.payload->>'countable')::boolean) AS countable_n "
                "FROM records r LEFT JOIN record_versions v ON v.version_id=r.current_version "
                "WHERE r.active GROUP BY r.dataset"
            ).fetchall()
            snapshot = profile_snapshot(conn)
            # Body/index identity alone misses mutable labels and supplemented
            # dates. Hash only server-side values; never expose their payloads.
            selection = conn.execute("""WITH current AS (
                SELECT r.record_id,r.dataset,r.current_version,v.payload
                FROM records r JOIN record_versions v ON v.version_id=r.current_version
                WHERE r.active
            ) SELECT
                (SELECT md5(COALESCE(string_agg(md5(jsonb_build_array(
                    record_id,dataset,current_version,payload->'countable',payload->'retrievable',
                    payload->'published_at',payload->'publisher',payload->'sponsor',
                    payload->'platform',payload->'account',payload->'keyword',payload->'title',
                    payload->'url',payload->'archive_url')::text),',' ORDER BY record_id),''))
                    FROM current) AS records,
                (SELECT md5(COALESCE(string_agg(md5(jsonb_build_array(a.version_id,a.ordinal,a.payload)::text),
                    ',' ORDER BY a.version_id,a.ordinal),''))
                    FROM annotations a JOIN current c ON c.current_version=a.version_id) AS annotations,
                (SELECT md5(COALESCE(string_agg(md5(jsonb_build_array(
                    d.version_id,d.method,d.tier,d.inferred_date,d.precision,d.review_state,
                    d.evidence->'host_match')::text),',' ORDER BY d.version_id,d.method),''))
                    FROM date_inferences d JOIN current c ON c.current_version=d.version_id) AS dates
            """).fetchone()
            statistics_version = digest(json.dumps(selection, sort_keys=True))
        return {
            "status": "ok",
            "record_counts": {r["dataset"]: r["n"] for r in rows},
            "countable_record_counts": {r["dataset"]: r["countable_n"] for r in rows},
            "statistics_version": statistics_version,
            **snapshot,
        }

    def pending_embeddings(self, model="text-embedding-3-small", profile_id=None):
        """Only current source chunks belonging to the requested or active profile."""
        from .indexing import active_profile

        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            profile_id = profile_id or active_profile(conn)
            if not conn.execute(
                "SELECT 1 FROM retrieval_profiles WHERE profile_id=%s", (profile_id,)
            ).fetchone():
                raise ValueError("Profile has not been prepared")
            return conn.execute(
                "SELECT DISTINCT c.text_hash,c.text FROM chunks c "
                "JOIN chunk_profile_membership m USING(chunk_id) "
                "JOIN records r ON r.current_version=c.version_id AND r.record_id=c.record_id "
                "LEFT JOIN embeddings e ON e.text_hash=c.text_hash AND e.model=%s "
                "WHERE r.active AND m.profile_id=%s AND e.text_hash IS NULL ORDER BY c.text_hash",
                (model, profile_id),
            ).fetchall()

    def search(
        self,
        query: str,
        filters: Filters,
        limit=5,
        vector=None,
        model="text-embedding-3-small",
        chunks_per_record=1,
        _include_diagnostics=False,
    ):
        where, params = self.where(filters)
        select = """SELECT c.chunk_id AS evidence_id,c.record_id,c.version_id,r.dataset,
        v.payload->>'title' AS title,v.payload->>'publisher' AS publisher,v.payload->>'sponsor' AS sponsor,
        v.payload->>'url' AS url,v.payload->>'archive_url' AS archive_url,
        (v.payload->>'published_at')::date AS published_at,c.text,
        c.start_char AS start,c.end_char AS end,c.paragraph_ids,
        c.source_observation_id,c.source_version_id,c.source_body_hash,
        CASE WHEN c.source_observation_id IS NOT NULL THEN v.payload END AS observation_payload,
        CASE WHEN r.dataset='native' THEN v.payload->'issues' END AS source_text_issues"""
        join = (
            " FROM chunks c JOIN records r ON r.current_version=c.version_id "
            "AND r.record_id=c.record_id JOIN record_versions v ON v.version_id=c.version_id "
            "JOIN chunk_profile_membership m ON m.chunk_id=c.chunk_id "
            "JOIN retrieval_state s ON s.singleton AND s.active_profile=m.profile_id "
        )
        # Keep OR recall; distinct English query lexemes rank before word density.
        tokens = re.findall(r"[\w-]+", query.lower())
        stop = {
            "what",
            "which",
            "how",
            "does",
            "the",
            "a",
            "an",
            "is",
            "are",
            "of",
            "to",
            "in",
            "and",
            "for",
            "do",
            "about",
            "say",
            "these",
            "this",
            "with",
        }
        terms = list(dict.fromkeys(t for t in tokens if len(t) > 1 and t not in stop))[:40]
        literal_allowed = (filters.dataset in {"social", "all"}
                           and 4 <= len(query.strip()) <= 2000
                           and len(re.findall(r"\w+", query)) >= 2)
        if not terms and not literal_allowed:
            if _include_diagnostics:
                return {"evidence": [], "diagnostics": self._unavailable_coverage(
                    "No meaningful search terms remain after stop-word filtering.", tokens
                )}
            return []
        lexical_query = " OR ".join('"' + t + '"' for t in terms)
        # Both collections are ranked separately: social posts outnumber native
        # articles about 130 to 1, so one pooled top 50 would crowd native out.
        collections = ("native", "social") if filters.dataset == "all" else (None,)
        with self.connect(vector=vector is not None) as conn:
            # Vector adapter registration already starts a transaction; finish its
            # catalog lookup before establishing the retrieval snapshot.
            conn.commit()
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            channels_by_collection = {}
            for collection in collections:
                scoped_where = where + (" AND r.dataset=%s" if collection else "")
                scoped_params = [*params, *([collection] if collection else [])]
                lexical = conn.execute(
                    select
                    + ",(SELECT count(*) FROM unnest(tsvector_to_array(to_tsvector('english',%s))) "
                    "AS query_lexeme(term) WHERE query_lexeme.term=ANY(tsvector_to_array(c.search_vector))) "
                    "AS lexical_coverage,ts_rank_cd(c.search_vector,websearch_to_tsquery('english',%s)) AS score"
                    + join
                    + f" WHERE {scoped_where} AND c.search_vector @@ websearch_to_tsquery('english',%s) "
                    "ORDER BY lexical_coverage DESC,score DESC,c.chunk_id LIMIT 50",
                    [" ".join(terms), lexical_query, *scoped_params, lexical_query],
                ).fetchall() if terms else []
                semantic = []
                if vector is not None:
                    semantic = self._semantic_rows(
                        conn, select, join, scoped_where, scoped_params, vector, model,
                        approximate=(collection or filters.dataset) != "native",
                    )
                channels_by_collection[collection] = [("keyword", lexical), ("vector", semantic)]
            literal = []
            if literal_allowed and not any(rows for item in channels_by_collection.values() for _, rows in item):
                literal = conn.execute(
                    select + ",1.0 AS score" + join
                    + f" WHERE {where} AND r.dataset='social' "
                    "AND strpos(lower(c.text),lower(%s))>0 ORDER BY c.chunk_id LIMIT 50",
                    [*params, query.strip()],
                ).fetchall()
                channels_by_collection["social" if filters.dataset == "all" else None].append(("literal", literal))
            # Reciprocal-rank fusion within each collection.
            by_id, channels, ordered = {}, {}, {}
            for collection, sources in channels_by_collection.items():
                ranking = {}
                for channel, rows in sources:
                    for rank, row in enumerate(rows, 1):
                        eid = row["evidence_id"]
                        ranking[eid] = ranking.get(eid, 0) + 1 / (60 + rank)
                        by_id[eid] = row
                        channels.setdefault(eid, []).append(channel)
                ordered[collection] = [(eid, ranking[eid]) for eid in sorted(ranking, key=lambda k: (-ranking[k], k))]
            # Choose distinct records first, alternating between collections by
            # rank; a collection with fewer hits leaves its places to the other.
            records = []
            queues = [list(dict.fromkeys(by_id[eid]["record_id"] for eid, _ in items)) for items in ordered.values()]
            for position in range(max((len(queue) for queue in queues), default=0)):
                for queue in queues:
                    if position < len(queue) and len(records) < limit and queue[position] not in records:
                        records.append(queue[position])
            # Then retain several ranked passages within those same records.
            # Article hit alone does not guarantee answer coverage.
            from .social_source_binding import bind_search_row

            selected = {rid: [] for rid in records}
            for items in ordered.values():
                for rank, (eid, score) in enumerate(items, 1):
                    row = by_id[eid]
                    group = selected.get(row["record_id"])
                    if group is None or len(group) >= chunks_per_record:
                        continue
                    row["score"] = score
                    row["retrieval_rank"] = rank
                    row["retrieval_sources"] = channels[eid]
                    group.append(bind_search_row(row))
            evidence = [
                evidence
                for group in selected.values()
                for evidence in sorted(group, key=lambda e: (e.source_observation_id or "", e.start))
            ]
            if _include_diagnostics:
                if literal:
                    diagnostics = {
                        "status": "available", "operator": "literal_phrase", "configuration": "exact_saved_text",
                        "terms": [query.strip()], "returned_matched_terms": [query.strip()],
                        "missing_from_results": [], "missing_from_scope": [], "term_details": [],
                        "ignored_terms": [], "scope_records": None, "scope_chunks": None,
                        "reason": "No English keyword or vector hit was found. This result matches a literal phrase in saved social text; meaning and completeness are not verified.",
                    }
                    for item in evidence:
                        item.matched_terms = item.literal_matched_terms = [query.strip()]
                else:
                    diagnostics = self._coverage(conn, terms, join, where, params, evidence)
                return {"evidence": evidence, "diagnostics": diagnostics}
            return evidence

    # Nearest-neighbour candidates taken from the HNSW index before filtering.
    # At 1,000 the planner abandons the index for a full scan (about 290 ms on
    # 37k vectors); 400 stays on the index (about 8 ms) and leaves ample
    # candidates for the usual filters, with the exact fallback below.
    ANN_CANDIDATES = 400

    @staticmethod
    def _semantic_rows(conn, select, join, where, params, vector, model, *, approximate):
        """Top 50 chunks by cosine distance within the filtered scope.

        A large scope first takes index candidates from the embeddings table
        alone, then joins and filters them. If filtering leaves fewer than 50
        (a narrow selection), the exact scan over the whole scope runs instead,
        so a narrow scope never loses results. A small scope always runs exactly.
        """
        import numpy as np

        vector = np.array(vector)
        if approximate:
            conn.execute("SELECT set_config('hnsw.ef_search', %s, true), set_config('enable_seqscan', 'off', true)",
                         (str(Database.ANN_CANDIDATES),))
            rows = conn.execute(
                "WITH ann AS MATERIALIZED (SELECT text_hash, embedding <=> %s AS distance FROM embeddings "
                "WHERE model=%s ORDER BY embedding <=> %s LIMIT %s) "
                + select + ", 1-ann.distance AS score" + join
                + " JOIN ann ON ann.text_hash=c.text_hash "
                + f"WHERE {where} ORDER BY ann.distance,c.chunk_id LIMIT 50",
                [vector, model, vector, Database.ANN_CANDIDATES, *params],
            ).fetchall()
            conn.execute("SELECT set_config('enable_seqscan', 'on', true)")
            if len(rows) == 50:
                return rows
        return conn.execute(
            select
            + ", 1-(e.embedding <=> %s) AS score"
            + join
            + " JOIN embeddings e ON e.text_hash=c.text_hash AND e.model=%s "
            + f"WHERE {where} ORDER BY e.embedding <=> %s,c.chunk_id LIMIT 50",
            [vector, model, *params, vector],
        ).fetchall()

    def search_report(self, query, filters, limit=5):
        """Read results and scoped keyword coverage within one retrieval snapshot."""
        return self.search(query, filters, limit=limit, _include_diagnostics=True)

    @staticmethod
    def _unavailable_coverage(reason, ignored_terms):
        return {
            "status": "unavailable", "operator": "OR", "configuration": "english",
            "terms": [], "returned_matched_terms": [], "missing_from_results": [],
            "missing_from_scope": [], "term_details": [],
            "ignored_terms": list(dict.fromkeys(ignored_terms)), "reason": reason,
            "scope_records": None, "scope_chunks": None,
        }

    @classmethod
    def _coverage(cls, conn, terms, join, where, params, evidence):
        """Use the index's exact English tsquery semantics, including stemming.

        Missing terms concern eligible indexed passages, never an entire article's
        unindexed body or a factual absence. Non-English text is not classified as
        absent by this English-only diagnostic.
        """
        supported = list(dict.fromkeys(
            term for term in terms if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", term)
        ))
        ignored = list(dict.fromkeys(term for term in terms if term not in supported))
        if not supported:
            return cls._unavailable_coverage(
                "Keyword coverage uses an English index and is unavailable for this query. "
                "No absence claim is made; try English keywords or semantic search.", terms
            )
        analyzed = conn.execute(
            "SELECT term,tsvector_to_array(to_tsvector('english',term)) AS lexemes "
            "FROM unnest(%s::text[]) WITH ORDINALITY AS input(term,ordinal) ORDER BY ordinal",
            (supported,),
        ).fetchall()
        meaningful = [row["term"] for row in analyzed if row["lexemes"]]
        ignored.extend(row["term"] for row in analyzed if not row["lexemes"])
        if not meaningful:
            return cls._unavailable_coverage(
                "No meaningful English search terms remain after index stop-word filtering.", ignored
            )
        scope = conn.execute(
            "SELECT count(DISTINCT r.record_id) AS scope_records,count(*) AS scope_chunks"
            + join + f" WHERE {where}", params,
        ).fetchone()
        coverage = conn.execute(
            "WITH scope AS MATERIALIZED (SELECT c.chunk_id,r.record_id,c.search_vector"
            + join + f" WHERE {where}) "
            "SELECT input.term,count(DISTINCT scope.record_id) AS matching_records,"
            "array_agg(DISTINCT scope.chunk_id) FILTER (WHERE scope.chunk_id=ANY(%s::text[])) "
            "AS returned_ids FROM unnest(%s::text[]) WITH ORDINALITY AS input(term,ordinal) "
            "LEFT JOIN scope ON scope.search_vector @@ "
            "websearch_to_tsquery('english', '\"' || input.term || '\"') "
            "GROUP BY input.term,input.ordinal ORDER BY input.ordinal",
            [*params, [row.evidence_id for row in evidence], meaningful],
        ).fetchall()
        lexemes = {row["term"]: row["lexemes"] for row in analyzed}
        for row in evidence:
            row.matched_terms = [
                match["term"] for match in coverage
                if row.evidence_id in (match["returned_ids"] or [])
            ]
            row.literal_matched_terms = [
                term for term in row.matched_terms
                if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", row.text, re.I)
            ]
        return {
            "status": "partial" if ignored else "available", "operator": "OR",
            "configuration": "english", "terms": meaningful, "term_limit": 40,
            "returned_matched_terms": [row["term"] for row in coverage if row["returned_ids"]],
            "missing_from_results": [row["term"] for row in coverage if not row["returned_ids"]],
            "missing_from_scope": [row["term"] for row in coverage if row["matching_records"] == 0],
            "term_details": [{
                "term": row["term"], "lexemes": lexemes[row["term"]],
                "matching_records": row["matching_records"]
            } for row in coverage],
            "ignored_terms": ignored, **scope,
            "reason": "Keywords are combined with OR, so a result need not match every term. "
            "Search considers at most the first 40 distinct terms after basic stop-word filtering. "
            "Lexical ranking prioritizes distinct English query lexeme coverage, then word density. "
            "Coverage uses English stemming in indexed body passages within the current selection; "
            "it is not a check of meaning or factual truth. Ranking scores are not confidence probabilities.",
        }

    def validate_evidence(self, evidence: Evidence):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT c.*,v.body,v.body_hash,v.payload,r.dataset,"
                "r.current_version,r.active FROM chunks c JOIN record_versions v USING(version_id) "
                "JOIN records r ON r.record_id=c.record_id WHERE chunk_id=%s",
                (evidence.evidence_id,),
            ).fetchone()
        if evidence.source_observation_id is not None:
            from .social_source_binding import evidence_source, public_observations
            from .social_source_retrieval import source_version_id

            if (not row or row.get("active") is not True
                    or row.get("current_version") != evidence.version_id
                    or row.get("record_id") != evidence.record_id
                    or row.get("version_id") != evidence.version_id
                    or source_version_id(row.get("payload")) != evidence.version_id
                    or any(row.get(key) != getattr(evidence, key) for key in (
                        "source_observation_id", "source_version_id", "source_body_hash"))
                    or row.get("start_char") != evidence.start or row.get("end_char") != evidence.end
                    or row.get("text") != evidence.text):
                return False
            try:
                evidence_source({**row, "retrievable": row["payload"].get("retrievable"),
                    "social_source_observations": public_observations(row["payload"])}, evidence)
            except (TypeError, ValueError):
                return False
            return True
        if row and any(row.get(key) is not None for key in (
                "source_observation_id", "source_version_id", "source_body_hash")):
            return False
        return bool(
            row
            and row["record_id"] == evidence.record_id
            and row["version_id"] == evidence.version_id
            and row["start_char"] == evidence.start
            and row["end_char"] == evidence.end
            and row["body"][evidence.start : evidence.end]
            == evidence.text
            == row["text"]
        )

    def save_answer(self, question, filters, result, data_version):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO answer_runs(question,filters,result,data_version) VALUES (%s,%s,%s,%s)",
                (
                    question,
                    Jsonb(filters.model_dump(mode="json")),
                    Jsonb(result.model_dump(mode="json")),
                    data_version,
                ),
            )
