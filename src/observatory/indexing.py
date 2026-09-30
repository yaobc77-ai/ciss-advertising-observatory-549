"""Versioned chunk membership with an atomic, fully checked publication boundary.

Profiles select immutable original-source chunks; they never replace or delete them.
Preparing is local and unpaid. Embeddings are supplied separately before activation.
"""

import hashlib
import json
from importlib.metadata import version

from psycopg.types.json import Jsonb

from .chunking import CHUNKING_PROFILE_VERSION, chunk_retrieval_body
from .segmentation import SEGMENTATION_PROFILE

LEGACY_PROFILE = "legacy600-v1"
SENTENCE_PROFILE = "sentence600-v1"
EMBEDDING_MODEL = "text-embedding-3-small"
PROFILE_IDS = (LEGACY_PROFILE, SENTENCE_PROFILE)


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
    if profile_id == SENTENCE_PROFILE:
        result.update(segmentation=SEGMENTATION_PROFILE, pysbd_version=version("pysbd"))
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
        "SELECT r.record_id,v.version_id,v.body,v.payload FROM records r "
        "JOIN record_versions v ON v.version_id=r.current_version WHERE r.active "
        "ORDER BY r.record_id"
    ).fetchall()
    for row in rows:
        serialized = json.dumps(
            row["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        if _digest(serialized) != row["version_id"] or row["payload"].get("body") != row["body"]:
            raise ValueError("Source version payload was modified in place")
    return rows


def expected_chunks(row, spec):
    payload = row["payload"]
    if not payload.get("retrievable", False):
        return []
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


def store_record_chunks(conn, profile_id, row, spec=None):
    """Used by both import and candidate preparation while holding lock 54901."""
    spec = spec or _register(conn, profile_id)
    for chunk in expected_chunks(row, spec):
        conn.execute(
            "INSERT INTO chunks(chunk_id,record_id,version_id,text,text_hash,start_char,"
            "end_char,paragraph_ids,token_count) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (chunk["chunk_id"], chunk["record_id"], chunk["version_id"], chunk["text"],
             chunk["text_hash"], chunk["start_char"], chunk["end_char"],
             Jsonb(chunk["paragraph_ids"]), chunk["token_count"]),
        )
        stored = conn.execute(
            "SELECT chunk_id,record_id,version_id,text,text_hash,start_char,end_char,"
            "paragraph_ids,token_count FROM chunks WHERE chunk_id=%s", (chunk["chunk_id"],)
        ).fetchone()
        if stored != chunk:
            raise ValueError("Existing immutable chunk differs from generated source chunk")
        conn.execute(
            "INSERT INTO chunk_profile_membership(profile_id,chunk_id) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING", (profile_id, chunk["chunk_id"]),
        )


def _members(conn, profile_id):
    return conn.execute(
        "SELECT c.chunk_id,c.record_id,c.version_id,c.text,c.text_hash,c.start_char,"
        "c.end_char,c.paragraph_ids,c.token_count FROM chunks c "
        "JOIN chunk_profile_membership m USING(chunk_id) "
        "JOIN records r ON r.current_version=c.version_id AND r.record_id=c.record_id "
        "WHERE r.active AND m.profile_id=%s ORDER BY c.chunk_id", (profile_id,),
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
