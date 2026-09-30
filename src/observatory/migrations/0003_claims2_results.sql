-- CLAIMS2 analysis is separate from immutable articles and legacy annotations.
CREATE UNIQUE INDEX IF NOT EXISTS record_versions_record_version ON record_versions(record_id,version_id);
CREATE TABLE IF NOT EXISTS claims2_taxonomies (
 bundle_fingerprint text PRIMARY KEY,
 payload jsonb NOT NULL,
 payload_sha256 text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS claims2_runs (
 run_key text PRIMARY KEY,
 upstream_run_id text NOT NULL,
 dataset text NOT NULL CHECK(dataset IN ('native','social')),
 bundle_fingerprint text NOT NULL REFERENCES claims2_taxonomies(bundle_fingerprint),
 metadata jsonb NOT NULL,
 metadata_sha256 text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS claims2_imports (
 import_key text PRIMARY KEY,
 run_key text NOT NULL REFERENCES claims2_runs(run_key),
 audit_manifest_sha256 text NOT NULL,
 audit_report_sha256 text NOT NULL,
 review_manifest_sha256 text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS claims2_results (
 candidate_key text PRIMARY KEY,
 run_key text NOT NULL REFERENCES claims2_runs(run_key),
 import_key text NOT NULL REFERENCES claims2_imports(import_key),
 record_id text NOT NULL REFERENCES records(record_id),
 version_id text NOT NULL REFERENCES record_versions(version_id),
 body_hash text NOT NULL,
 nc_id text NOT NULL,
 sc_id text,
 start_char integer NOT NULL,
 end_char integer NOT NULL,
 quote text NOT NULL,
 payload jsonb NOT NULL,
 payload_sha256 text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 CHECK(start_char >= 0 AND end_char > start_char),
 FOREIGN KEY(record_id,version_id) REFERENCES record_versions(record_id,version_id)
);
CREATE INDEX IF NOT EXISTS claims2_results_record ON claims2_results(record_id,version_id);
CREATE INDEX IF NOT EXISTS claims2_results_category ON claims2_results(nc_id,sc_id);
-- Candidate evidence is immutable; later human reviews append a new revision.
CREATE TABLE IF NOT EXISTS claims2_reviews (
 review_key text PRIMARY KEY,
 revision bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
 candidate_key text NOT NULL REFERENCES claims2_results(candidate_key),
 import_key text NOT NULL REFERENCES claims2_imports(import_key),
 review_state text NOT NULL CHECK(review_state IN ('automatic_unverified','human_supported')),
 payload jsonb NOT NULL,
 payload_sha256 text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claims2_reviews_candidate ON claims2_reviews(candidate_key,revision DESC);
CREATE TABLE IF NOT EXISTS claims2_retractions (
 event_key text PRIMARY KEY,
 candidate_key text NOT NULL REFERENCES claims2_results(candidate_key),
 reviewer text NOT NULL CHECK(length(trim(reviewer)) > 0),
 reason text NOT NULL CHECK(length(trim(reason)) > 0),
 reviewed_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claims2_retractions_candidate ON claims2_retractions(candidate_key);
