-- Approximate nearest-neighbour candidates for semantic retrieval, and the
-- text_hash lookup that joins those candidates back to their chunks.
SET LOCAL maintenance_work_mem = '512MB';

CREATE INDEX IF NOT EXISTS chunks_text_hash ON chunks(text_hash);

CREATE INDEX IF NOT EXISTS embeddings_hnsw_cosine
    ON embeddings USING hnsw (embedding vector_cosine_ops);
