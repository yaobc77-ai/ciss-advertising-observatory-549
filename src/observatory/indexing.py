"""Versioned chunk membership with an atomic, fully checked publication boundary.

Profiles select immutable original-source chunks; they never replace or delete them.
Preparing is local and unpaid. Embeddings are supplied separately before activation.
"""

import hashlib
import json
from importlib.metadata import version

from psycopg.types.json import Jsonb

from .chunking import CHUNKING_PROFILE_VERSION, chunk_retrieval_body
from .segmentation import COVERAGE_SEGMENTATION_PROFILE, SEGMENTATION_PROFILE

LEGACY_PROFILE = "legacy600-v1"
SENTENCE_PROFILE = "sentence600-v1"
SOCIAL_OBSERVATION_PROFILE = "sentence600-social-observations-v1"
EMBEDDING_MODEL = "text-embedding-3-small"
PROFILE_IDS = (LEGACY_PROFILE, SENTENCE_PROFILE, SOCIAL_OBSERVATION_PROFILE)
OBSERVATION_FIELDS = ("source_observation_id", "source_version_id", "source_body_hash")


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def definition(profile_id):
    if profile_id not in PROFILE_IDS:
        raise ValueError(f"Unknown retrieval profile: {profile_id}")
    result = {
        "strategy": "legacy" if profile_id == LEGACY_PROFILE else "sentence",
        "max_tokens": 600,
        "overlap_tokens": 100,
        "chunking_algorithm": CHUNKING_PROFILE_VERSION,
        "encoding": "cl100k_base",
        "tiktoken_version": version("tiktoken"),
        "embedding_model": EMBEDDING_MODEL,
        "embedding_input": "original-chunk-text-v1",
    }
    if profile_id in (SENTENCE_PROFILE, SOCIAL_OBSERVATION_PROFILE):
        result.update(segmentation=SEGMENTATION_PROFILE, pysbd_version=version("pysbd"))
    if profile_id == SOCIAL_OBSERVATION_PROFILE:
        from .social_source_retrieval import SCHEMA_VERSION

        result.update(
            social_observation_strategy="sentence_coverage",
            social_observation_policy=SCHEMA_VERSION,
            social_observation_segmentation=COVERAGE_SEGMENTATION_PROFILE,
            social_retrieval="source-observation-keywords-v1",
        )
    return result


def _register(conn, profile_id):
    spec = definition(profile_id)
    fingerprint = _digest(json.dumps(spec, sort_keys=True, separators=(",", ":")))
    conn.execute(
        "INSERT INTO retrieval_profiles(profile_id,definition,definition_hash) "
        "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
        (profile_id, Jsonb(spec), fingerprint),
    )
    row = conn.execute(
        "SELECT definition,definition_hash FROM retrieval_profiles WHERE profile_id=%s",
        (profile_id,),
    ).fetchone()
    if row["definition"] != spec or row["definition_hash"] != fingerprint:
        raise ValueError("Retrieval profile definition changed; use a new profile identifier")
    return spec


def source_version(conn):
    return conn.execute(
        "SELECT md5(COALESCE(string_agg(current_version,',' ORDER BY record_id),'')) AS value "
        "FROM records WHERE active"
    ).fetchone()["value"]


def active_profile(conn):
    row = conn.execute("SELECT active_profile FROM retrieval_state WHERE singleton").fetchone()
    if not row:
        raise RuntimeError("Retrieval profiles are not initialized; run init-db")
    return row["active_profile"]


def _sources(conn):
    rows = conn.execute(
        "SELECT r.record_id,r.dataset,v.version_id,v.body,v.payload FROM records r "
        "LEFT JOIN record_versions v ON v.version_id=r.current_version AND v.record_id=r.record_id "
        "WHERE r.active "
        "ORDER BY r.record_id"
    ).fetchall()
    for row in rows:
        payload = row["payload"]
        if (not isinstance(payload, dict) or payload.get("record_id") != row["record_id"]
                or payload.get("dataset") != row["dataset"]):
            raise ValueError("Current source version belongs to a different record or dataset")
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        if _digest(serialized) != row["version_id"] or payload.get("body") != row["body"]:
            raise ValueError("Source version payload was modified in place")
    return rows


def expected_chunks(row, spec):
    payload = row["payload"]
    if not payload.get("retrievable", False):
        return []
    if payload.get("dataset") == "social" and spec.get("social_observation_strategy"):
        return _observation_chunks(row, spec)
    result = []
    for chunk in chunk_retrieval_body(
        row["body"], spec["max_tokens"], spec["overlap_tokens"],
        retrieval_end=payload.get("retrieval_end"),
        retrieval_ranges=payload.get("retrieval_ranges"), strategy=spec["strategy"],
    ):
        if row["body"][chunk["start"]:chunk["end"]] != chunk["text"]:
            raise ValueError("Chunk does not match its original source interval")
        result.append({
            "chunk_id": _digest(f"{row['version_id']}:{chunk['start']}:{chunk['end']}"),
            "record_id": row["record_id"], "version_id": row["version_id"],
            "text": chunk["text"], "text_hash": _digest(chunk["text"]),
            "start_char": chunk["start"], "end_char": chunk["end"],
            "paragraph_ids": chunk["paragraph_ids"], "token_count": chunk["token_count"],
        })
    return result


def _observation_chunks(row, spec):
    from .social_source_retrieval import validated_observations

    if spec["social_observation_strategy"] != "sentence_coverage":
        raise ValueError("Unknown source-observation chunk strategy")
    result = []
    for observation in validated_observations(row["payload"]):
        body = observation["body"]
        if _digest(body) != observation["source_body_hash"]:
            raise ValueError("Source observation body hash changed")
        for chunk in chunk_retrieval_body(
            body, spec["max_tokens"], spec["overlap_tokens"], strategy="sentence_coverage",
        ):
            if body[chunk["start"]:chunk["end"]] != chunk["text"]:
                raise ValueError("Observation chunk does not match its source interval")
            result.append({
                "chunk_id": _digest(
                    f"{row['version_id']}:{observation['source_version_id']}:"
                    f"{chunk['start']}:{chunk['end']}"
                ),
                "record_id": row["record_id"], "version_id": row["version_id"],
                "text": chunk["text"], "text_hash": _digest(chunk["text"]),
                "start_char": chunk["start"], "end_char": chunk["end"],
                "paragraph_ids": chunk["paragraph_ids"], "token_count": chunk["token_count"],
                **{field: observation[field] for field in OBSERVATION_FIELDS},
            })
    return result


def _source_fields(row):
    """Keep legacy chunk dictionaries and their manifest bytes unchanged."""
    result = dict(row)
    present = [result.get(field) is not None for field in OBSERVATION_FIELDS]
    if any(present) and not all(present):
        raise ValueError("Incomplete source-observation provenance")
    if not any(present):
        for field in OBSERVATION_FIELDS:
            result.pop(field, None)
    return result


def store_record_chunks(conn, profile_id, row, spec=None):
    """Used by both import and candidate preparation while holding lock 54901."""
    spec = spec or _register(conn, profile_id)
    for chunk in expected_chunks(row, spec):
        conn.execute(
            "INSERT INTO chunks(chunk_id,record_id,version_id,text,text_hash,start_char,"
            "end_char,paragraph_ids,token_count,source_observation_id,source_version_id,"
            "source_body_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (chunk["chunk_id"], chunk["record_id"], chunk["version_id"], chunk["text"],
             chunk["text_hash"], chunk["start_char"], chunk["end_char"],
             Jsonb(chunk["paragraph_ids"]), chunk["token_count"],
             *(chunk.get(field) for field in OBSERVATION_FIELDS)),
        )
        stored = conn.execute(
            "SELECT chunk_id,record_id,version_id,text,text_hash,start_char,end_char,"
            "paragraph_ids,token_count,source_observation_id,source_version_id,source_body_hash "
            "FROM chunks WHERE chunk_id=%s", (chunk["chunk_id"],)
        ).fetchone()
        if stored is None or _source_fields(stored) != chunk:
            raise ValueError("Existing immutable chunk differs from generated source chunk")
        conn.execute(
            "INSERT INTO chunk_profile_membership(profile_id,chunk_id) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING", (profile_id, chunk["chunk_id"]),
        )


def _members(conn, profile_id):
    rows = conn.execute(
        "SELECT c.chunk_id,c.record_id,c.version_id,c.text,c.text_hash,c.start_char,"
        "c.end_char,c.paragraph_ids,c.token_count,c.source_observation_id,c.source_version_id,"
        "c.source_body_hash FROM chunks c "
        "JOIN chunk_profile_membership m USING(chunk_id) "
        "JOIN records r ON r.current_version=c.version_id AND r.record_id=c.record_id "
        "WHERE r.active AND m.profile_id=%s ORDER BY c.chunk_id", (profile_id,),
    ).fetchall()
    return [_source_fields(row) for row in rows]


def _native_vectors(conn, profile_id):
    """Compare existing native vectors, including their exact stored values."""
    return conn.execute(
        "SELECT DISTINCT c.text_hash,e.model,e.embedding::text AS embedding FROM chunks c "
        "JOIN chunk_profile_membership m USING(chunk_id) "
        "JOIN records r ON r.current_version=c.version_id AND r.record_id=c.record_id "
        "JOIN embeddings e ON e.text_hash=c.text_hash "
        "WHERE r.active AND r.dataset='native' AND m.profile_id=%s "
        "ORDER BY c.text_hash,e.model", (profile_id,),
    ).fetchall()


def _manifest(members):
    # Include the stored payload as well as IDs to detect accidental in-place edits.
    return _digest(json.dumps(members, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def record_preparation(conn, profile_id):
    members = _members(conn, profile_id)
    conn.execute(
        "INSERT INTO retrieval_preparations(profile_id,source_data_version,manifest_hash,chunk_count) "
        "VALUES (%s,%s,%s,%s) ON CONFLICT(profile_id) DO UPDATE SET "
        "source_data_version=excluded.source_data_version,manifest_hash=excluded.manifest_hash,"
        "chunk_count=excluded.chunk_count,prepared_at=now()",
        (profile_id, source_version(conn), _manifest(members), len(members)),
    )


def bootstrap(conn):
    """One-time migration: reproduce legacy membership without changing old chunks."""
    spec = _register(conn, LEGACY_PROFILE)
    created = conn.execute(
        "INSERT INTO retrieval_state(singleton,active_profile) VALUES (true,%s) "
        "ON CONFLICT DO NOTHING RETURNING active_profile", (LEGACY_PROFILE,),
    ).fetchone()
    if created:
        for row in _sources(conn):
            store_record_chunks(conn, LEGACY_PROFILE, row, spec)
        record_preparation(conn, LEGACY_PROFILE)
    else:
        _register(conn, active_profile(conn))


def profile_snapshot(conn):
    profile_id = active_profile(conn)
    source = source_version(conn)
    members = _members(conn, profile_id)
    spec = conn.execute(
        "SELECT definition_hash FROM retrieval_profiles WHERE profile_id=%s", (profile_id,)
    ).fetchone()
    index = _digest(f"{profile_id}:{spec['definition_hash']}:{_manifest(members)}")
    return {
        "source_data_version": source, "active_profile": profile_id,
        "index_version": index, "data_version": _digest(f"{source}:{index}"),
        "chunks": len(members),
    }


class IndexManager:
    def __init__(self, db):
        self.db = db

    def prepare(self, profile_id):
        with self.db.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(54901)")
            spec = _register(conn, profile_id)
            for row in _sources(conn):
                store_record_chunks(conn, profile_id, row, spec)
            record_preparation(conn, profile_id)
            return self._status(conn, profile_id)

    def status(self, profile_id=None):
        with self.db.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return self._status(conn, profile_id or active_profile(conn))

    @staticmethod
    def _status(conn, profile_id):
        spec = conn.execute(
            "SELECT definition,definition_hash FROM retrieval_profiles WHERE profile_id=%s",
            (profile_id,),
        ).fetchone()
        if not spec:
            raise ValueError("Profile has not been prepared")
        preparation = conn.execute(
            "SELECT source_data_version,manifest_hash,chunk_count FROM retrieval_preparations "
            "WHERE profile_id=%s", (profile_id,),
        ).fetchone()
        current_source = source_version(conn)
        members = _members(conn, profile_id)
        missing = conn.execute(
            "SELECT count(DISTINCT c.text_hash) AS n FROM chunks c "
            "JOIN chunk_profile_membership m USING(chunk_id) "
            "JOIN records r ON r.current_version=c.version_id AND r.record_id=c.record_id "
            "LEFT JOIN embeddings e ON e.text_hash=c.text_hash AND e.model=%s "
            "WHERE r.active AND m.profile_id=%s AND e.text_hash IS NULL",
            (EMBEDDING_MODEL, profile_id),
        ).fetchone()["n"]
        return {
            "profile_id": profile_id, "active": profile_id == active_profile(conn),
            "source_data_version": current_source, "definition": spec["definition"],
            "preparation": preparation, "chunks": len(members),
            "missing_embeddings": missing,
            "prepared_snapshot_matches": bool(
                preparation and preparation["source_data_version"] == current_source
                and preparation["manifest_hash"] == _manifest(members)
            ),
        }

    def activate(self, profile_id, *, expected_source_data_version):
        """Publish or roll back only a fully prepared, current, embedded profile."""
        with self.db.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(54901)")
            spec = _register(conn, profile_id)
            status = self._status(conn, profile_id)
            if expected_source_data_version != source_version(conn):
                raise ValueError("Source snapshot changed after preparation")
            if not status["prepared_snapshot_matches"]:
                raise ValueError("Profile preparation is missing or stale; prepare again")
            expected = sorted(
                [chunk for row in _sources(conn) for chunk in expected_chunks(row, spec)],
                key=lambda item: item["chunk_id"],
            )
            if expected != _members(conn, profile_id):
                raise ValueError("Profile membership is incomplete or violates source ranges")
            if status["missing_embeddings"]:
                raise ValueError("Profile embeddings are incomplete; active index was not changed")
            conn.execute(
                "UPDATE retrieval_state SET active_profile=%s,activated_at=now() WHERE singleton",
                (profile_id,),
            )
            snapshot = profile_snapshot(conn)
            conn.execute(
                "INSERT INTO retrieval_publications(profile_id,source_data_version,index_version) "
                "VALUES (%s,%s,%s)",
                (profile_id, snapshot["source_data_version"], snapshot["index_version"]),
            )
            return snapshot

    def activate_social_observation_profile(
        self, *, expected_source_data_version, expected_previous_profile,
    ):
        """Publish unpaid social keywords only after preserving the native index.

        The ordinary activation gate still requires complete embeddings. This
        explicit transition accepts the existing native vector availability,
        including missing vectors, and never creates vectors or repairs sources.
        """
        if expected_previous_profile not in (LEGACY_PROFILE, SENTENCE_PROFILE):
            raise ValueError("An existing legacy or sentence profile must be named")
        profile_id = SOCIAL_OBSERVATION_PROFILE
        with self.db.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(54901)")
            if active_profile(conn) != expected_previous_profile:
                raise ValueError("Active profile changed after preparation")
            previous_spec = _register(conn, expected_previous_profile)
            spec = _register(conn, profile_id)
            status = self._status(conn, profile_id)
            if expected_source_data_version != source_version(conn):
                raise ValueError("Source snapshot changed after preparation")
            if not status["prepared_snapshot_matches"]:
                raise ValueError("Profile preparation is missing or stale; prepare again")
            sources = _sources(conn)
            expected = sorted(
                [chunk for row in sources for chunk in expected_chunks(row, spec)],
                key=lambda item: item["chunk_id"],
            )
            members = _members(conn, profile_id)
            if expected != members:
                raise ValueError("Profile membership is incomplete or violates source ranges")
            native_ids = {row["record_id"] for row in sources if row["payload"].get("dataset") == "native"}
            native = [chunk for chunk in members if chunk["record_id"] in native_ids]
            previous_native = [
                chunk for chunk in _members(conn, expected_previous_profile)
                if chunk["record_id"] in native_ids
            ]
            expected_previous_native = sorted(
                [chunk for row in sources if row["record_id"] in native_ids
                 for chunk in expected_chunks(row, previous_spec)],
                key=lambda item: item["chunk_id"],
            )
            if native != previous_native or previous_native != expected_previous_native:
                raise ValueError("Native chunks differ from the previous active profile")
            if _native_vectors(conn, profile_id) != _native_vectors(conn, expected_previous_profile):
                raise ValueError("Native vector availability differs from the previous active profile")
            if source_version(conn) != expected_source_data_version or active_profile(conn) != expected_previous_profile:
                raise ValueError("Source or active profile changed during publication")
            conn.execute(
                "UPDATE retrieval_state SET active_profile=%s,activated_at=now() WHERE singleton",
                (profile_id,),
            )
            snapshot = profile_snapshot(conn)
            conn.execute(
                "INSERT INTO retrieval_publications(profile_id,source_data_version,index_version) "
                "VALUES (%s,%s,%s)",
                (profile_id, snapshot["source_data_version"], snapshot["index_version"]),
            )
            return snapshot
