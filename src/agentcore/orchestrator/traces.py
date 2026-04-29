"""Trace log keyed by task_id, with optional disk and Postgres mirrors.

A trace records every (handoff received, llm called, outcome emitted) tuple
for a single task. The default storage is in-memory (scoped to the process).
When `disk_dir` is set, every event also gets appended as JSONL for local
CLI workflows. When `settings` is set, events are best-effort dual-written
to Postgres so multi-node orchestrators and the UI see the same history.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog

from agentcore.settings import Settings
from agentcore.state.db import pg_conn

log = structlog.get_logger(__name__)

EventKind = Literal[
    "handoff_in", "llm_call", "outcome", "error", "signal",
    # Runtime extras (executor + batching + discovery + retry).
    "executor", "batch_split", "batch_chunk", "discovery", "llm_retry",
    # CLI-level chain lifecycle (review loop, apply, PR, final result).
    # Persisted alongside runtime events so `agentcore tail` shows the
    # full story end-to-end.
    "review_round", "verdict", "route_back", "applied", "pr_opened", "result",
]

DDL = """
CREATE TABLE IF NOT EXISTS agentcore_traces (
  id         BIGSERIAL PRIMARY KEY,
  project_id TEXT NOT NULL DEFAULT 'default',
  task_id    TEXT NOT NULL,
  step       INT NOT NULL,
  kind       TEXT NOT NULL,
  actor      TEXT NOT NULL,
  at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  detail     JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_agentcore_traces_task
  ON agentcore_traces (project_id, task_id, at);
CREATE INDEX IF NOT EXISTS idx_agentcore_traces_project_created
  ON agentcore_traces (project_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_agentcore_traces_kind
  ON agentcore_traces (project_id, kind, at DESC);
"""


@dataclass(slots=True)
class TraceEvent:
    task_id: str
    step: int
    kind: EventKind
    actor: str
    at: datetime = field(default_factory=lambda: datetime.now(UTC))
    detail: dict[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "step": self.step,
            "kind": self.kind,
            "actor": self.actor,
            "at": self.at.isoformat(),
            "detail": self.detail,
        }


class TraceLog:
    """Append-only ring per task. Thread-safe; suitable for asyncio + threads."""

    def __init__(
        self,
        max_per_task: int = 500,
        *,
        disk_dir: Path | str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings
        self._by_task: dict[str, list[TraceEvent]] = {}
        self._lock = threading.RLock()
        self._max = max_per_task
        self._persistent = False
        self._disk_dir: Path | None = Path(disk_dir).expanduser() if disk_dir else None
        if self._disk_dir is not None:
            try:
                self._disk_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("trace.disk_dir_unavailable", error=str(exc))
                self._disk_dir = None
        if self.settings is not None:
            self.init_schema()

    def init_schema(self) -> bool:
        if self.settings is None:
            self._persistent = False
            return False
        try:
            with (
                psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(DDL)
            self._persistent = True
            log.info("trace.persistent")
            return True
        except Exception as exc:
            self._persistent = False
            log.info("trace.in_memory", reason=str(exc))
            return False

    @property
    def is_persistent(self) -> bool:
        return self._persistent

    def record(self, event: TraceEvent, *, project_id: str | None = None) -> None:
        with self._lock:
            bucket = self._by_task.setdefault(event.task_id, [])
            bucket.append(event)
            if len(bucket) > self._max:
                del bucket[: len(bucket) - self._max]
        if self._disk_dir is not None:
            self._write_disk(event)
        if self._persistent:
            self._write_pg(event, project_id=project_id)

    def _write_disk(self, event: TraceEvent) -> None:
        try:
            # Append-only JSONL — atomic at the line level under
            # POSIX append-mode writes for sub-PIPE_BUF payloads,
            # which our short events comfortably satisfy.
            path = self._disk_dir / f"{event.task_id}.jsonl"  # type: ignore[operator]
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event.to_jsonable(), default=str) + "\n")
        except OSError as exc:
            log.warning("trace.disk_write_failed", error=str(exc))

    def _write_pg(self, event: TraceEvent, *, project_id: str | None = None) -> None:
        if self.settings is None:
            return
        pid = project_id or self.settings.project_name
        with contextlib.suppress(Exception):
            with (
                psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    """
                    INSERT INTO agentcore_traces
                      (project_id, task_id, step, kind, actor, at, detail)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        pid,
                        event.task_id,
                        event.step,
                        str(event.kind),
                        event.actor,
                        event.at,
                        json.dumps(event.detail),
                    ),
                )

    def for_task(
        self, task_id: str, *, project_id: str | None = None
    ) -> list[TraceEvent]:
        if self._persistent and self.settings is not None:
            rows = self._for_task_pg(task_id, project_id=project_id)
            if rows:
                return rows
        with self._lock:
            return list(self._by_task.get(task_id, []))

    def _for_task_pg(
        self, task_id: str, *, project_id: str | None = None
    ) -> list[TraceEvent]:
        if self.settings is None:
            return []
        pid = project_id or self.settings.project_name
        try:
            with (
                psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    """
                    SELECT task_id, step, kind, actor, at, detail
                      FROM agentcore_traces
                     WHERE project_id = %s
                       AND task_id = %s
                  ORDER BY at ASC, id ASC
                    """,
                    (pid, task_id),
                )
                out: list[TraceEvent] = []
                for tid, step, kind, actor, at, detail in cur.fetchall() or []:
                    out.append(
                        TraceEvent(
                            task_id=str(tid),
                            step=int(step),
                            kind=str(kind),  # type: ignore[arg-type]
                            actor=str(actor),
                            at=at,
                            detail=detail if isinstance(detail, dict) else {},
                        )
                    )
                return out
        except Exception as exc:
            log.warning("trace.pg_read_failed", error=str(exc))
            return []

    def cleanup(self, *, retention_days: int) -> int:
        """Delete old durable trace rows. JSONL retention remains operator-owned."""
        if not self._persistent or self.settings is None:
            return 0
        try:
            with (
                psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    """
                    DELETE FROM agentcore_traces
                     WHERE at < now() - (%s || ' days')::interval
                    """,
                    (str(retention_days),),
                )
                return int(cur.rowcount or 0)
        except Exception as exc:
            log.warning("trace.cleanup_failed", error=str(exc))
            return 0

    def tasks(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._by_task.keys()))
