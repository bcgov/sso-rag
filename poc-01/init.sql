-- Idempotent database initialisation script.
-- Safe to run multiple times; uses IF NOT EXISTS throughout.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id          SERIAL PRIMARY KEY,
    title       TEXT,
    content     TEXT,
    filename    TEXT NOT NULL,
    metadata    JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- nomic-embed-text produces 768-dimensional vectors (see parser.py EMBEDDING_DIMENSION).
CREATE TABLE IF NOT EXISTS document_embeddings (
    id          SERIAL PRIMARY KEY,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    chunk_text  TEXT NOT NULL,
    chunk_index INTEGER NOT NULL DEFAULT 0,
    source_file TEXT NOT NULL,
    header_1    TEXT,
    header_2    TEXT,
    header_3    TEXT,
    embedding   vector(768) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- HNSW index for fast approximate cosine-similarity search.
CREATE INDEX IF NOT EXISTS document_embeddings_embedding_hnsw_idx
    ON document_embeddings
    USING hnsw (embedding vector_cosine_ops);
