-- Observation provenance is optional for existing immutable canonical chunks.
ALTER TABLE chunks
    ADD COLUMN source_observation_id text,
    ADD COLUMN source_version_id text,
    ADD COLUMN source_body_hash text,
    ADD CONSTRAINT chunks_source_observation_complete CHECK (
        (source_observation_id IS NULL AND source_version_id IS NULL AND source_body_hash IS NULL)
        OR
        (source_observation_id IS NOT NULL AND source_version_id IS NOT NULL AND source_body_hash IS NOT NULL)
    );

CREATE INDEX chunks_source_observation
    ON chunks(record_id,version_id,source_observation_id,source_version_id)
    WHERE source_observation_id IS NOT NULL;
