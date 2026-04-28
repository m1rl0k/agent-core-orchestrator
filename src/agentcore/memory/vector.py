"""pgvector-backed vector store.

Schema (single table, multi-tenant by `collection`):
  CREATE EXTENSION IF NOT EXISTS vector;
  CREATE TABLE IF NOT EXISTS agentcore_chunks (
    id           BIGSERIAL PRIMARY KEY,
    collection   TEXT NOT NULL,
    ref          TEXT NOT NULL,            -- e.g. "code:src/foo.py:42"
    content      TEXT NOT NULL,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding    vector(768) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  CREATE INDEX IF NOT EXISTS idx_agentcore_chunks_collection
    ON agentcore_chunks(collection);
  CREATE INDEX IF NOT EXISTS idx_agentcore_chunks_embedding
    ON agentcore_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import psycopg
from pgvector.psycopg import register_vector

from agentcore.memory.embed import EMBED_DIM
from agentcore.settings import Settings, get_settings

DDL = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS agentcore_chunks (
  id          BIGSERIAL PRIMARY KEY,
  collection  TEXT NOT NULL,
  ref         TEXT NOT NULL,
  content     TEXT NOT NULL,
  metadata    JSONB NOT NULL DEFAULT '{{}}'::jsonb,
  embedding   vector({EMBED_DIM}) NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agentcore_chunks_collection
  ON agentcore_chunks(collection);
"""


@dataclass(slots=True)
class Hit:
    ref: str
    content: str
    score: float
    metadata: dict[str, Any]


class VectorStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _conn(self) -> psycopg.Connection:
        conn = psycopg.connect(self.settings.pg_dsn, autocommit=True)
        register_vector(conn)
        return conn

    def init_schema(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(DDL)

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
            "VALUES (%s, %s, %s, %s::jsonb, %s)"
        )
        with self._conn() as conn, conn.cursor() as cur:
            for ref, content, meta, emb in items:
                cur.execute(sql, (collection, ref, content, json.dumps(meta), emb))
        return len(items)

    def search(self, collection: str, query_emb: list[float], k: int = 8) -> list[Hit]:
        sql = (
            "SELECT ref, content, metadata, "
            "  1 - (embedding <=> %s) AS score "
            "FROM agentcore_chunks WHERE collection = %s "
            "ORDER BY embedding <=> %s LIMIT %s"
        )
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (query_emb, collection, query_emb, k))
            rows = cur.fetchall()
        return [Hit(ref=r[0], content=r[1], score=float(r[3]), metadata=r[2] or {}) for r in rows]

    def clear(self, collection: str) -> int:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM agentcore_chunks WHERE collection = %s", (collection,))
            return cur.rowcount
