-- Runs once, on first initialization of an empty data directory.
--
-- The extension is created here rather than in an Alembic migration so that a
-- fresh database is usable before migrations run, and so migrations do not need
-- superuser privileges.

CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector: embeddings for RAG
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- trigram indexes for fuzzy search
CREATE EXTENSION IF NOT EXISTS "uuid-ossp"; -- UUID generation
