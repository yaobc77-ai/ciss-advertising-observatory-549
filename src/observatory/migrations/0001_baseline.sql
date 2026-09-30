CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS records (
 record_id text PRIMARY KEY, dataset text NOT NULL CHECK(dataset IN ('native','social')),
 current_version text NOT NULL
);
ALTER TABLE records ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
CREATE TABLE IF NOT EXISTS record_versions (
 version_id text PRIMARY KEY, record_id text NOT NULL REFERENCES records(record_id),
 body text NOT NULL, body_hash text NOT NULL, payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS annotations (
 version_id text NOT NULL REFERENCES record_versions(version_id), ordinal integer NOT NULL,
 payload jsonb NOT NULL, PRIMARY KEY(version_id,ordinal)
);
CREATE TABLE IF NOT EXISTS chunks (
 chunk_id text PRIMARY KEY, record_id text NOT NULL REFERENCES records(record_id),
 version_id text NOT NULL REFERENCES record_versions(version_id),
 text text NOT NULL, text_hash text NOT NULL, start_char integer NOT NULL, end_char integer NOT NULL,
 paragraph_ids jsonb NOT NULL, token_count integer NOT NULL,
 search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english',text)) STORED,
 CHECK(start_char>=0 AND end_char>start_char)
);
CREATE INDEX IF NOT EXISTS chunks_search ON chunks USING gin(search_vector);
CREATE INDEX IF NOT EXISTS chunks_current ON chunks(record_id,version_id);
CREATE TABLE IF NOT EXISTS retrieval_profiles (
 profile_id text PRIMARY KEY, definition jsonb NOT NULL, definition_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chunk_profile_membership (
 profile_id text NOT NULL REFERENCES retrieval_profiles(profile_id),
 chunk_id text NOT NULL REFERENCES chunks(chunk_id), PRIMARY KEY(profile_id,chunk_id)
);
CREATE TABLE IF NOT EXISTS retrieval_state (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 active_profile text NOT NULL REFERENCES retrieval_profiles(profile_id),
 activated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS retrieval_preparations (
 profile_id text PRIMARY KEY REFERENCES retrieval_profiles(profile_id),
 source_data_version text NOT NULL, manifest_hash text NOT NULL, chunk_count integer NOT NULL,
 prepared_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS retrieval_publications (
 publication_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 profile_id text NOT NULL REFERENCES retrieval_profiles(profile_id),
 source_data_version text NOT NULL, index_version text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS embeddings (
 text_hash text NOT NULL, model text NOT NULL, embedding vector(1536) NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(text_hash,model)
);
CREATE TABLE IF NOT EXISTS imports (
 import_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 created_at timestamptz NOT NULL DEFAULT now(), report jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_ledger (
 reservation_id text PRIMARY KEY, month text NOT NULL, visitor text NOT NULL,
 kind text NOT NULL CHECK(kind IN ('generation','embedding')), model text NOT NULL,
 reserved_usd numeric(14,8) NOT NULL CHECK(reserved_usd>=0),
 actual_usd numeric(14,8), state text NOT NULL CHECK(state IN ('pending','settled','uncertain','cancelled')),
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 usage jsonb NOT NULL DEFAULT '{}', CHECK(actual_usd IS NULL OR actual_usd>=0)
);
CREATE INDEX IF NOT EXISTS usage_month ON usage_ledger(month);
CREATE INDEX IF NOT EXISTS usage_visitor ON usage_ledger(visitor,created_at);
CREATE TABLE IF NOT EXISTS answer_runs (
 run_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
 question text NOT NULL, filters jsonb NOT NULL, result jsonb NOT NULL, data_version text NOT NULL
);
CREATE TABLE IF NOT EXISTS generation_outputs (
 reservation_id text PRIMARY KEY REFERENCES usage_ledger(reservation_id),
 created_at timestamptz NOT NULL DEFAULT now(), evidence_ids jsonb NOT NULL,
 parsed jsonb, response_status text NOT NULL
);
