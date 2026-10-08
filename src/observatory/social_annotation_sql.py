"""One safe, body-bound SQL projection of the historical social source states.

The scalar subquery only needs the existing ``v`` record-version alias. It is
shared by public projections and static WHERE callers, including chunk search.
No caller must add a new join or reproduce annotation validation rules.
"""

from .social_annotations import SCHEME, STATUS, social_label_metadata


def _taxonomy_sql():
    keys = [item["key"] for item in social_label_metadata()]
    # All literals come from the fixed source taxonomy, never a filter value.
    codes = ",".join(f"({ordinal},'{key}')" for ordinal, key in enumerate(keys))
    key_array = "ARRAY[" + ",".join(f"'{key}'" for key in keys) + "]::text[]"
    return keys, codes, key_array


def _annotation_valid_sql(*, body_sha=None, values_count=None, boolean_values=None, labels=None):
    """The one validation predicate used by both scalar and batch projections.

    Batch callers pass precomputed expressions, not different validation rules.
    None of these arguments contains user input.
    """
    keys, codes, key_array = _taxonomy_sql()
    body_sha = body_sha or "encode(sha256(convert_to(v.body,'UTF8')),'hex')"
    values_count = values_count or "(SELECT count(*) FROM jsonb_object_keys(safe.source_values))"
    boolean_values = boolean_values or f"""(SELECT bool_and(jsonb_typeof(safe.source_values->code.key)='boolean')
                    FROM (VALUES {codes}) AS code(ordinal,key))"""
    labels = labels or f"""COALESCE((
                    SELECT jsonb_agg(code.key ORDER BY code.ordinal)
                    FROM (VALUES {codes}) AS code(ordinal,key)
                    WHERE safe.source_values->code.key='true'::jsonb
                ),'[]'::jsonb)"""
    return f"""COALESCE(candidate.n=1
                AND candidate.payload->>'status'='{STATUS}'
                AND candidate.payload->>'basis'='supplied_source_post_id_and_exact_body'
                AND candidate.payload->>'taxonomy_mapping'='source_keys_preserved_no_native_label_mapping'
                AND jsonb_typeof(candidate.payload->'values')='object'
                AND {values_count}={len(keys)}
                AND safe.source_values ?& {key_array}
                AND {boolean_values}
                AND jsonb_typeof(candidate.payload->'source_sha256')='string'
                AND candidate.payload->>'source_sha256' ~ '^[0-9a-f]{{64}}$'
                AND jsonb_typeof(candidate.payload->'source_row')='number'
                AND candidate.payload->>'source_row' ~ '^[1-9][0-9]*$'
                AND v.body_hash ~ '^[0-9a-f]{{64}}$'
                AND v.body_hash={body_sha}
                AND candidate.payload->'body_sha256'=to_jsonb(v.body_hash)
                AND (NOT (candidate.payload ? 'version_id')
                    OR candidate.payload->'version_id'=to_jsonb(v.version_id))
                AND candidate.payload->'labels'={labels},false)"""


def social_annotation_projection_sql():
    """Return states, validity and an internal source fingerprint for ``v``.

    Candidate cardinality is checked before validation: a second annotation of
    this scheme makes every code Unknown, even if only one candidate is valid.
    JSON expansion receives a sanitized object, rather than depending on SQL
    predicate evaluation order to protect malformed JSON from type errors.
    The fingerprint contains validated public values and source bindings only;
    raw imports, explanations and arbitrary annotation fields are never read.
    """
    _, codes, _ = _taxonomy_sql()
    return f"""(
        SELECT jsonb_build_object(
            'states',states.ids,'valid',checked.valid,
            'source_token',encode(sha256(convert_to(jsonb_build_array(
                '{SCHEME}','{STATUS}',v.version_id,v.body_hash,
                encode(sha256(convert_to(v.body,'UTF8')),'hex'),
                candidate.n,checked.valid,states.ids,
                CASE WHEN checked.valid THEN candidate.payload->'source_sha256' END,
                CASE WHEN checked.valid THEN candidate.payload->'source_row' END
            )::text,'UTF8')),'hex'))
        FROM (
            SELECT count(*) AS n,
                (jsonb_agg(a.payload ORDER BY a.ordinal)->0) AS payload
            FROM annotations a WHERE a.version_id=v.version_id
                AND a.payload->>'version'='{SCHEME}'
        ) candidate
        CROSS JOIN LATERAL (
            SELECT CASE WHEN jsonb_typeof(candidate.payload->'values')='object'
                THEN candidate.payload->'values' ELSE '{{}}'::jsonb END AS source_values
        ) safe
        CROSS JOIN LATERAL (
            SELECT {_annotation_valid_sql()} AS valid
        ) checked
        CROSS JOIN LATERAL (
            SELECT jsonb_agg('{SCHEME}:' || code.key || ':' ||
                CASE WHEN NOT checked.valid THEN 'unknown'
                     WHEN safe.source_values->code.key='true'::jsonb THEN 'source_true'
                     ELSE 'source_false' END ORDER BY code.ordinal) AS ids
            FROM (VALUES {codes}) AS code(ordinal,key)
        ) states
    )"""


def social_source_snapshot_sql(public_sql, *, include_counts=False):
    """Fingerprint the complete selected social scope in one SQL statement.

    ``public_sql`` is Database's own allowlisted projection; filter values stay
    in its parameters. Candidate cardinality, body hashing and validation are
    calculated once per selected version. Rejoining the original filtered rows
    preserves their record identities and multiplicity in counts/fingerprints.
    """
    _, codes, _ = _taxonomy_sql()
    valid_sql = _annotation_valid_sql(
        body_sha="v.body_sha", values_count="metrics.values_count",
        boolean_values="metrics.boolean_values", labels="metrics.labels",
    )
    counts = """
        count(*) AS total,
        count(*) FILTER (WHERE annotation->'valid'='true'::jsonb) AS valid_annotation_records,
        count(*) FILTER (WHERE annotation->'valid'='false'::jsonb) AS unknown_annotation_records,
        COALESCE((SELECT jsonb_object_agg(state_id,n) FROM (
            SELECT state_id,count(*) AS n FROM social_states
            CROSS JOIN LATERAL jsonb_array_elements_text(annotation->'states') AS state_id
            GROUP BY state_id
        ) state_counts),'{}'::jsonb) AS state_counts,
    """ if include_counts else ""
    return f"""WITH public_source AS NOT MATERIALIZED ({public_sql}), filtered AS MATERIALIZED (
        SELECT record_id,version_id,dataset FROM public_source
    ), social_versions AS MATERIALIZED (
        SELECT v.version_id,v.body_hash,
            encode(sha256(convert_to(v.body,'UTF8')),'hex') AS body_sha
        FROM record_versions v JOIN (
            SELECT DISTINCT f.version_id FROM filtered f WHERE f.dataset='social'
        ) selected ON selected.version_id=v.version_id
    ), candidates AS MATERIALIZED (
        SELECT v.version_id,count(a.version_id) AS n,
            (jsonb_agg(a.payload ORDER BY a.ordinal)
                FILTER (WHERE a.version_id IS NOT NULL)->0) AS payload
        FROM social_versions v LEFT JOIN annotations a ON a.version_id=v.version_id
            AND a.payload->>'version'='{SCHEME}'
        GROUP BY v.version_id
    ), source_values AS MATERIALIZED (
        SELECT candidate.*,
            CASE WHEN jsonb_typeof(candidate.payload->'values')='object'
                THEN candidate.payload->'values' ELSE '{{}}'::jsonb END AS source_values
        FROM candidates candidate
    ), value_metrics AS MATERIALIZED (
        SELECT safe.version_id,
            (SELECT count(*) FROM jsonb_object_keys(safe.source_values)) AS values_count,
            bool_and(jsonb_typeof(safe.source_values->code.key)='boolean') AS boolean_values,
            COALESCE(jsonb_agg(code.key ORDER BY code.ordinal)
                FILTER (WHERE safe.source_values->code.key='true'::jsonb),'[]'::jsonb) AS labels
        FROM source_values safe CROSS JOIN (VALUES {codes}) AS code(ordinal,key)
        GROUP BY safe.version_id,safe.source_values
    ), checked_sources AS MATERIALIZED (
        SELECT candidate.version_id,{valid_sql} AS valid
        FROM candidates candidate
        JOIN social_versions v ON v.version_id=candidate.version_id
        JOIN source_values safe ON safe.version_id=candidate.version_id
        JOIN value_metrics metrics ON metrics.version_id=candidate.version_id
    ), version_states AS MATERIALIZED (
        SELECT checked.version_id,jsonb_agg('{SCHEME}:' || code.key || ':' ||
            CASE WHEN NOT checked.valid THEN 'unknown'
                 WHEN safe.source_values->code.key='true'::jsonb THEN 'source_true'
                 ELSE 'source_false' END ORDER BY code.ordinal) AS ids
        FROM checked_sources checked JOIN source_values safe USING (version_id)
        CROSS JOIN (VALUES {codes}) AS code(ordinal,key)
        GROUP BY checked.version_id
    ), version_annotations AS MATERIALIZED (
        SELECT v.version_id,jsonb_build_object(
            'states',states.ids,'valid',checked.valid,
            'source_token',encode(sha256(convert_to(jsonb_build_array(
                '{SCHEME}','{STATUS}',v.version_id,v.body_hash,v.body_sha,
                candidate.n,checked.valid,states.ids,
                CASE WHEN checked.valid THEN candidate.payload->'source_sha256' END,
                CASE WHEN checked.valid THEN candidate.payload->'source_row' END
            )::text,'UTF8')),'hex')) AS annotation
        FROM social_versions v
        JOIN candidates candidate USING (version_id)
        JOIN checked_sources checked USING (version_id)
        JOIN version_states states USING (version_id)
    ), social_states AS MATERIALIZED (
        SELECT f.record_id,f.version_id,v.annotation
        FROM filtered f JOIN version_annotations v ON v.version_id=f.version_id
        WHERE f.dataset='social'
    ) SELECT {counts} encode(sha256(convert_to(jsonb_build_array(
        '{SCHEME}','{STATUS}',COALESCE(jsonb_agg(jsonb_build_array(
            record_id,version_id,annotation->>'source_token'
        ) ORDER BY record_id COLLATE "C"),'[]'::jsonb)
    )::text,'UTF8')),'hex') AS source_state_version FROM social_states"""
