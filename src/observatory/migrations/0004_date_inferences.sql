-- Likely publication dates for records whose source date is missing.
-- Source dates in record_versions are never changed. Each row is bound to one
-- immutable record version, carries its evidence tier and stays unreviewed
-- until a person accepts or rejects it.
CREATE TABLE IF NOT EXISTS date_inferences (
 inference_id text PRIMARY KEY,
 record_id text NOT NULL REFERENCES records(record_id),
 version_id text NOT NULL REFERENCES record_versions(version_id),
 method text NOT NULL CHECK(method IN ('url_path','archive_first_capture','web_search')),
 tier text NOT NULL CHECK(tier IN ('A','B','C')),
 inferred_date date,
 earliest date,
 latest date,
 precision text NOT NULL CHECK(precision IN ('day','month','year','range')),
 evidence jsonb NOT NULL,
 review_state text NOT NULL DEFAULT 'unreviewed' CHECK(review_state IN ('unreviewed','accepted','rejected')),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(version_id, method),
 CHECK(earliest IS NULL OR latest IS NULL OR earliest <= latest),
 CHECK(precision <> 'day' OR inferred_date IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS date_inferences_version ON date_inferences(version_id) WHERE review_state <> 'rejected';
