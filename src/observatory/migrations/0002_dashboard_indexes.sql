-- Keep indexes modest until representative client data establishes scale needs.
CREATE INDEX IF NOT EXISTS records_active_dataset
 ON records(dataset,record_id) WHERE active;
CREATE INDEX IF NOT EXISTS record_versions_record
 ON record_versions(record_id);
