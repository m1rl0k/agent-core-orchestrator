"""Postgres-backed job queue.

Single `agentcore_jobs` table; workers claim with `FOR UPDATE SKIP LOCKED`.
This is the canonical Postgres-as-queue pattern (a la pgmq, river_jobs):
zero new infra, durable, multi-worker safe, observable via plain SQL.

Used today by `/wiki/refresh` so git hooks return 202 in milliseconds while
the curator runs off-thread. Future call sites: scheduled scans, autonomous
ops loops, anything that shouldn't block an HTTP request.

Falls back to in-process execution when Postgres is unreachable so the
orchestrator stays useful in dev / smoke-test mode without a DB.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import structlog

from agentcore.settings import Settings, get_settings

log = structlog.get_logger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS agentcore_jobs (
  id              BIGSERIAL PRIMARY KEY,
  kind            TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'queued',
  payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
  idempotency_key TEXT,
  priority        INT NOT NULL DEFAULT 0,
  attempts        INT NOT NULL DEFAULT 0,
  max_attempts    INT NOT NULL DEFAULT 3,
  run_after       TIMESTAMPTZ NOT NULL DEFAULT now(),
  locked_by       TEXT,
  locked_until    TIMESTAMPTZ,
  created_by      TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at      TIMESTAMPTZ,
  finished_at     TIMESTAMPTZ,
  error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim
  ON agentcore_jobs (status, run_after, priority DESC, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idem
  ON agentcore_jobs (kind, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
"""


@dataclass(slots=True)
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


class JobQueue:
    """Durable async work queue.

    `enqueue()` is fire-and-forget; the caller gets back a job id and the
    worker loop drains it on its own clock. `claim/complete/fail/requeue`
    are the worker primitives.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._persistent = False
        # In-memory fallback used only if the DB isn't reachable. Bounded.
        self._fallback: list[tuple[int, str, dict[str, Any]]] = []
        self._next_fallback_id = 1

    def init_schema(self) -> bool:
        try:
            with (
                psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(DDL)
            self._persistent = True
            log.info("jobs.persistent")
            return True
        except Exception as exc:
            self._persistent = False
            log.info("jobs.in_memory", reason=str(exc))
            return False

    @property
    def is_persistent(self) -> bool:
        return self._persistent

    # ---- producer side ----------------------------------------------

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        priority: int = 0,
        run_after: datetime | None = None,
        max_attempts: int = 3,
        created_by: str | None = None,
    ) -> int | None:
        payload = payload or {}
        run_after = run_after or datetime.now(UTC)
        if self._persistent:
            with contextlib.suppress(Exception):
                return self._enqueue_pg(
                    kind, payload, idempotency_key, priority, run_after,
                    max_attempts, created_by,
                )
        # Fallback: stash for an immediate in-process worker drain.
        return self._enqueue_mem(kind, payload)

    def _enqueue_pg(
        self,
        kind: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
        priority: int,
        run_after: datetime,
        max_attempts: int,
        created_by: str | None,
    ) -> int:
        with (
            psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
            conn.cursor() as cur,
        ):
            # ON CONFLICT honours the partial unique index for (kind, idem_key)
            # so duplicate webhooks return the existing job id rather than enqueuing twice.
            if idempotency_key is not None:
                cur.execute(
                    """
                    INSERT INTO agentcore_jobs
                      (kind, payload, idempotency_key, priority, run_after, max_attempts, created_by)
                    VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s)
                    ON CONFLICT (kind, idempotency_key) WHERE idempotency_key IS NOT NULL
                    DO UPDATE SET kind = EXCLUDED.kind  -- no-op to RETURNING
                    RETURNING id
                    """,
                    (kind, json.dumps(payload), idempotency_key, priority,
                     run_after, max_attempts, created_by),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO agentcore_jobs
                      (kind, payload, priority, run_after, max_attempts, created_by)
                    VALUES (%s, %s::jsonb, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (kind, json.dumps(payload), priority, run_after,
                     max_attempts, created_by),
                )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def _enqueue_mem(self, kind: str, payload: dict[str, Any]) -> int:
        jid = self._next_fallback_id
        self._next_fallback_id += 1
        self._fallback.append((jid, kind, payload))
        if len(self._fallback) > 1024:
            self._fallback = self._fallback[-1024:]
        return jid

    # ---- consumer side ----------------------------------------------

    def claim(self, worker_id: str, lock_seconds: int = 600) -> Job | None:
        """Atomically claim a single ready job. Returns None if nothing is ready."""
        if not self._persistent:
            return self._claim_mem()
        try:
            return self._claim_pg(worker_id, lock_seconds)
        except Exception as exc:
            log.warning("jobs.claim_failed", error=str(exc))
            return None

    def _claim_pg(self, worker_id: str, lock_seconds: int) -> Job | None:
        sql = """
        WITH next AS (
          SELECT id FROM agentcore_jobs
           WHERE (status = 'queued' AND run_after <= now())
              OR (status = 'running' AND locked_until IS NOT NULL AND locked_until < now())
           ORDER BY priority DESC, created_at ASC
           FOR UPDATE SKIP LOCKED
           LIMIT 1
        )
        UPDATE agentcore_jobs AS j
           SET status = 'running',
               attempts = j.attempts + 1,
               locked_by = %s,
               locked_until = now() + (%s || ' seconds')::interval,
               started_at = now()
          FROM next
         WHERE j.id = next.id
         RETURNING j.id, j.kind, j.payload, j.attempts, j.max_attempts;
        """
        with (
            psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(sql, (worker_id, str(lock_seconds)))
            row = cur.fetchone()
        if not row:
            return None
        jid, kind, payload, attempts, max_attempts = row
        return Job(
            id=int(jid),
            kind=str(kind),
            payload=payload if isinstance(payload, dict) else {},
            attempts=int(attempts),
            max_attempts=int(max_attempts),
        )

    def _claim_mem(self) -> Job | None:
        if not self._fallback:
            return None
        jid, kind, payload = self._fallback.pop(0)
        return Job(id=jid, kind=kind, payload=payload, attempts=1, max_attempts=1)

    def complete(self, job_id: int) -> None:
        if not self._persistent:
            return
        with contextlib.suppress(Exception):
            self._update_status(job_id, "done", error=None)

    def fail(self, job_id: int, error: str, *, retry_in_seconds: int = 30) -> None:
        """Mark failed; reschedule if attempts remain."""
        if not self._persistent:
            return
        try:
            with (
                psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    "SELECT attempts, max_attempts FROM agentcore_jobs WHERE id = %s",
                    (job_id,),
                )
                row = cur.fetchone()
                if not row:
                    return
                attempts, max_attempts = int(row[0]), int(row[1])
                if attempts >= max_attempts:
                    cur.execute(
                        """
                        UPDATE agentcore_jobs
                           SET status = 'failed', error = %s, finished_at = now(),
                               locked_by = NULL, locked_until = NULL
                         WHERE id = %s
                        """,
                        (error[:2000], job_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE agentcore_jobs
                           SET status = 'queued', error = %s,
                               run_after = now() + (%s || ' seconds')::interval,
                               locked_by = NULL, locked_until = NULL
                         WHERE id = %s
                        """,
                        (error[:2000], str(retry_in_seconds), job_id),
                    )
        except Exception as exc:
            log.warning("jobs.fail_failed", error=str(exc))

    def _update_status(
        self, job_id: int, status: str, *, error: str | None
    ) -> None:
        with (
            psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                """
                UPDATE agentcore_jobs
                   SET status = %s, error = %s, finished_at = now(),
                       locked_by = NULL, locked_until = NULL
                 WHERE id = %s
                """,
                (status, error, job_id),
            )

    def cleanup(self, retention_days: int = 7) -> int:
        if not self._persistent:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        try:
            with (
                psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    """
                    DELETE FROM agentcore_jobs
                     WHERE status IN ('done','failed') AND finished_at < %s
                    """,
                    (cutoff,),
                )
                return cur.rowcount or 0
        except Exception as exc:
            log.warning("jobs.cleanup_failed", error=str(exc))
            return 0


# ---- worker loop ----------------------------------------------------

Handler = Callable[[dict[str, Any]], Awaitable[None]]


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


async def run_worker(
    queue: JobQueue,
    handlers: dict[str, Handler],
    *,
    worker_id: str | None = None,
    poll_interval: float = 1.0,
    lock_seconds: int = 600,
    stop_event: Any = None,
) -> None:
    """Drain `queue`, dispatching by `kind` to the matching handler.

    `stop_event` (asyncio.Event) is the cancellation signal — set it during
    lifespan shutdown so the loop exits cleanly.
    """
    import asyncio

    wid = worker_id or default_worker_id()
    log.info("worker.start", worker_id=wid, kinds=list(handlers.keys()))
    while True:
        if stop_event is not None and stop_event.is_set():
            log.info("worker.stop", worker_id=wid)
            return
        job = queue.claim(wid, lock_seconds=lock_seconds)
        if job is None:
            try:
                if stop_event is not None:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=poll_interval
                    )
                    return
            except TimeoutError:
                pass
            else:
                await asyncio.sleep(poll_interval)
            continue
        handler = handlers.get(job.kind)
        if handler is None:
            log.warning("worker.unknown_kind", kind=job.kind, job_id=job.id)
            queue.fail(job.id, f"no handler for kind {job.kind!r}")
            continue
        try:
            await handler(job.payload)
            queue.complete(job.id)
        except Exception as exc:
            log.warning(
                "worker.handler_failed", kind=job.kind, job_id=job.id, error=str(exc)
            )
            queue.fail(job.id, repr(exc))
