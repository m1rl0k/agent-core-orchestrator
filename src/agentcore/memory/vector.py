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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from pgvector.psycopg import register_vector

from agentcore.memory.embed import DEFAULT_MODEL, EMBED_DIM, embedding_dim_for_model
from agentcore.settings import Settings, get_settings


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


def _configure_conn(conn: psycopg.Connection) -> None:
    register_vector(conn)


class VectorStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._pool: Any | None = None
        self._pool_available = True

    def _ensure_pool(self) -> Any | None:
        if not self._pool_available:
            return None
        if self._pool is None:
            try:
                from psycopg_pool import ConnectionPool
            except ImportError:
                self._pool_available = False
                return None
            self._pool = ConnectionPool(
                conninfo=self.settings.pg_dsn,
                min_size=1,
                max_size=8,
                kwargs={"autocommit": True},
                configure=_configure_conn,
                open=True,
            )
        return self._pool

    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        pool = self._ensure_pool()
        if pool is None:
            conn = psycopg.connect(self.settings.pg_dsn, autocommit=True)
            try:
                _configure_conn(conn)
                yield conn
            finally:
                conn.close()
            return
        with pool.connection() as conn:
            yield conn

    def init_schema(self, dim: int | None = None) -> None:
        """Create the chunks table at the requested embedding dimension.

        When `dim` is None, defaults to the registered dim of
        `settings.embed_model` (or the package default).
        """
        if dim is None:
            try:
                dim = embedding_dim_for_model(self.settings.embed_model or DEFAULT_MODEL)
            except ValueError:
                dim = EMBED_DIM
        ddl = _build_ddl(dim)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(ddl)
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
        if self._pool is not None:
            self._pool.close()
            self._pool = None
