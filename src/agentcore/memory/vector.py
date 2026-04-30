"""pgvector-backed vector store.

Schema (single table, multi-tenant by `collection`):
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE TABLE IF NOT EXISTS agentcore_chunks (
    id           BIGSERIAL PRIMARY KEY,
    collection   TEXT NOT NULL,
    ref          TEXT NOT NULL,            -- e.g. "code:src/foo.py:42"
    content      TEXT NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding    vector(<dim>) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(collection, ref)
  );
  CREATE INDEX IF NOT EXISTS idx_agentcore_chunks_collection
    ON agentcore_chunks(collection);
  CREATE INDEX IF NOT EXISTS idx_agentcore_chunks_embedding
    ON agentcore_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

`<dim>` is determined at `init_schema(dim=...)` time so swapping in a
different fastembed model (e.g. mxbai-large-v1, 1024-dim) does not require
hand-editing DDL.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from agentcore.memory.embed import DEFAULT_MODEL, EMBED_DIM, embedding_dim_for_model
from agentcore.settings import Settings, get_settings
from agentcore.state.db import pg_conn


def _build_ddl(dim: int) -> str:
    return f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS agentcore_chunks (
  id          BIGSERIAL PRIMARY KEY,
  collection  TEXT NOT NULL,
  ref         TEXT NOT NULL,
  content     TEXT NOT NULL,
  metadata    JSONB NOT NULL DEFAULT '{{}}'::jsonb,
  embedding   vector({dim}) NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_agentcore_chunks_collection_ref
  ON agentcore_chunks(collection, ref);
CREATE INDEX IF NOT EXISTS idx_agentcore_chunks_collection
  ON agentcore_chunks(collection);
CREATE INDEX IF NOT EXISTS idx_agentcore_chunks_embedding
  ON agentcore_chunks USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);
"""


# Backwards-compatible default DDL (Nomic-1.5 / 768-dim).
DDL = _build_ddl(EMBED_DIM)


@dataclass(slots=True)
class Hit:
    ref: str
    content: str
    score: float
    metadata: dict[str, Any]


class VectorStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _conn(self):  # type: ignore[no-untyped-def]
        # Pooled connection from the process-wide pool. Pgvector adapters
        # are registered in the pool's `_configure_conn` so vector binds
        # work on every checkout.
        return pg_conn(self.settings)

    def init_schema(self, dim: int | None = None) -> None:
        """Create the chunks table at the requested embedding dimension.

        When `dim` is None, defaults to the registered dim of
        `settings.embed_model` (or the package default).

        If `agentcore_chunks` already exists at a *different* embedding
        dimension, raise loudly. `CREATE TABLE IF NOT EXISTS` is a no-op
        when the table is present, so without this check a configuration
        switch (e.g. swapping a 768-dim model for a 1024-dim one) would
        silently leave the table at the old dim and every later upsert
        would fail with a vector-dim error that the retrieval factory
        swallows — silent retriever outage.
        """
        if dim is None:
            try:
                dim = embedding_dim_for_model(self.settings.embed_model or DEFAULT_MODEL)
            except ValueError:
                dim = EMBED_DIM
        with self._conn() as conn, conn.cursor() as cur:
            # Read the existing column type if the table is there. `to_regclass`
            # returns NULL for missing tables, so the WHERE matches zero rows
            # on first install and the check is skipped.
            cur.execute(
                """
                SELECT format_type(a.atttypid, a.atttypmod)
                  FROM pg_attribute a
                 WHERE a.attrelid = to_regclass('agentcore_chunks')
                   AND a.attname = 'embedding'
                   AND NOT a.attisdropped
                """
            )
            row = cur.fetchone()
            if row:
                existing = str(row[0])
                m = re.match(r"vector\((\d+)\)", existing)
                if m and int(m.group(1)) != dim:
                    raise RuntimeError(
                        f"agentcore_chunks.embedding has {existing} but requested "
                        f"vector({dim}); migrate the table before changing embed_model"
                    )
            cur.execute(_build_ddl(dim))
            cur.execute("ANALYZE agentcore_chunks")

    def upsert(
        self,
        collection: str,
        items: list[tuple[str, str, dict[str, Any], list[float]]],
    ) -> int:
        """`items` = list of (ref, content, metadata, embedding)."""
        if not items:
            return 0
        sql = (
            "INSERT INTO agentcore_chunks (collection, ref, content, metadata, embedding) "
            "VALUES (%s, %s, %s, %s::jsonb, %s::vector) "
            "ON CONFLICT (collection, ref) DO UPDATE SET "
            "content = EXCLUDED.content, "
            "metadata = EXCLUDED.metadata, "
            "embedding = EXCLUDED.embedding, "
            "updated_at = now()"
        )
        with self._conn() as conn, conn.cursor() as cur:
            for ref, content, meta, emb in items:
                cur.execute(sql, (collection, ref, content, json.dumps(meta), emb))
        return len(items)

    def search(self, collection: str, query_emb: list[float], k: int = 8) -> list[Hit]:
        sql = (
            "SELECT ref, content, metadata, "
            "  1 - (embedding <=> %s::vector) AS score "
            "FROM agentcore_chunks WHERE collection = %s "
            "ORDER BY embedding <=> %s::vector LIMIT %s"
        )
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (query_emb, collection, query_emb, k))
            rows = cur.fetchall()
        return [Hit(ref=r[0], content=r[1], score=float(r[3]), metadata=r[2] or {}) for r in rows]

    def clear(self, collection: str) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM agentcore_chunks WHERE collection = %s", (collection,))
            return cur.rowcount

    def delete_by_ref(self, collection: str, ref: str) -> int:
        """Remove a single chunk by its (collection, ref) identity.

        Used by the wiki indexer when a page is deleted on disk so its
        embedding doesn't keep showing up in retrieval.
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agentcore_chunks WHERE collection = %s AND ref = %s",
                (collection, ref),
            )
            return cur.rowcount

    def close(self) -> None:
        # No-op: connection lifecycle is owned by the shared pool in
        # `agentcore.state.db`. Kept for API compatibility.
        return None
