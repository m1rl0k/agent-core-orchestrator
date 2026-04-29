"""Persistent idempotency store.

Webhooks and git hooks fire-and-forget; the same `Idempotency-Key` header
arriving twice must NOT re-execute the chain. This module provides:

  - durable storage (Postgres) for `(scope, key) -> response` records
  - automatic TTL (default 24h) so the table doesn't grow forever
  - graceful in-memory fallback when Postgres isn't reachable, so the
    orchestrator stays up in dev/local without a DB

Used by `app.py` for `/run`, `/handoff`, `/signal`, and `/wiki/refresh`.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import structlog

from agentcore.settings import Settings, get_settings

log = structlog.get_logger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS agentcore_idempotency (
  key         TEXT NOT NULL,
  scope       TEXT NOT NULL,
  payload     JSONB NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at  TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (scope, key)
);
CREATE INDEX IF NOT EXISTS idx_idem_expires
  ON agentcore_idempotency(expires_at);
"""


@dataclass(slots=True)
class _MemEntry:
    payload: dict[str, Any]
    expires_at: float


@dataclass(slots=True)
class IdempotencyStore:
    """`(scope, key) -> response` cache with TTL.

    Tries Postgres first; falls back to a bounded in-memory dict if the DB
    is unavailable. Both code paths are safe to call concurrently from
    asyncio handlers (Postgres path: row-level via PRIMARY KEY + ON
    CONFLICT; in-memory path: GIL).
    """

    settings: Settings = field(default_factory=get_settings)
    ttl_seconds: float = 86400.0
    _mem: dict[tuple[str, str], _MemEntry] = field(default_factory=dict)
    _mem_max: int = 4096
    _persistent: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        # Probe Postgres availability once. Failing quietly is correct here:
        # an unreachable DB shouldn't take down the orchestrator.
        try:
            with psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn, conn.cursor() as cur:
                cur.execute(DDL)
            self._persistent = True
            log.info("idempotency.persistent")
        except Exception as exc:
            self._persistent = False
            log.info("idempotency.in_memory", reason=str(exc))

    # ---- public API -------------------------------------------------

    def get(self, scope: str, key: str) -> dict[str, Any] | None:
        if self._persistent:
            with contextlib.suppress(Exception):
                return self._get_pg(scope, key)
            # Fall through to memory if a live read fails (DB blip).
        return self._get_mem(scope, key)

    def put(
        self,
        scope: str,
        key: str,
        payload: dict[str, Any],
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        ttl = float(ttl_seconds if ttl_seconds is not None else self.ttl_seconds)
        if self._persistent:
            with contextlib.suppress(Exception):
                self._put_pg(scope, key, payload, ttl)
                return
        self._put_mem(scope, key, payload, ttl)

    def cleanup(self) -> int:
        """Delete expired rows. Cheap — index on expires_at."""
        if not self._persistent:
            self._cleanup_mem()
            return 0
        try:
            with (
                psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    "DELETE FROM agentcore_idempotency WHERE expires_at < now()"
                )
                return cur.rowcount or 0
        except Exception as exc:
            log.warning("idempotency.cleanup_failed", error=str(exc))
            return 0

    # ---- Postgres path ----------------------------------------------

    def _get_pg(self, scope: str, key: str) -> dict[str, Any] | None:
        with (
            psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT payload FROM agentcore_idempotency "
                "WHERE scope = %s AND key = %s AND expires_at > now()",
                (scope, key),
            )
            row = cur.fetchone()
            if row is None:
                return None
            payload = row[0]
            return payload if isinstance(payload, dict) else None

    def _put_pg(
        self, scope: str, key: str, payload: dict[str, Any], ttl: float
    ) -> None:
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
        with (
            psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                """
                INSERT INTO agentcore_idempotency (scope, key, payload, expires_at)
                VALUES (%s, %s, %s::jsonb, %s)
                ON CONFLICT (scope, key) DO UPDATE SET
                  payload = EXCLUDED.payload,
                  expires_at = EXCLUDED.expires_at
                """,
                (scope, key, json.dumps(payload), expires_at),
            )

    # ---- in-memory fallback -----------------------------------------

    def _get_mem(self, scope: str, key: str) -> dict[str, Any] | None:
        now = time.monotonic()
        entry = self._mem.get((scope, key))
        if entry is None:
            return None
        if entry.expires_at < now:
            self._mem.pop((scope, key), None)
            return None
        return entry.payload

    def _put_mem(
        self, scope: str, key: str, payload: dict[str, Any], ttl: float
    ) -> None:
        if len(self._mem) >= self._mem_max:
            self._cleanup_mem()
            if len(self._mem) >= self._mem_max:
                # Drop the oldest by expiry.
                oldest = min(self._mem.items(), key=lambda kv: kv[1].expires_at)[0]
                self._mem.pop(oldest, None)
        self._mem[(scope, key)] = _MemEntry(
            payload=payload, expires_at=time.monotonic() + ttl
        )

    def _cleanup_mem(self) -> None:
        now = time.monotonic()
        for k, v in list(self._mem.items()):
            if v.expires_at < now:
                self._mem.pop(k, None)
