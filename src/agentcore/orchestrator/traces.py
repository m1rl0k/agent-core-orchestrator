"""In-memory trace log keyed by task_id, with optional disk mirror.

A trace records every (handoff received, llm called, outcome emitted) tuple
for a single task. The default storage is in-memory (scoped to the
process). When `disk_dir` is set, every event also gets appended as a JSON
line to `<disk_dir>/<task_id>.jsonl` so that `agentcore tail <task_id>`
can follow the chain even when it ran via the CLI (no orchestrator HTTP).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog

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
    ) -> None:
        self._by_task: dict[str, list[TraceEvent]] = {}
        self._lock = threading.RLock()
        self._max = max_per_task
        self._disk_dir: Path | None = Path(disk_dir).expanduser() if disk_dir else None
        if self._disk_dir is not None:
            try:
                self._disk_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("trace.disk_dir_unavailable", error=str(exc))
                self._disk_dir = None

    def record(self, event: TraceEvent) -> None:
        with self._lock:
            bucket = self._by_task.setdefault(event.task_id, [])
            bucket.append(event)
            if len(bucket) > self._max:
                del bucket[: len(bucket) - self._max]
        if self._disk_dir is not None:
            try:
                # Append-only JSONL — atomic at the line level under
                # POSIX append-mode writes for sub-PIPE_BUF payloads,
                # which our short events comfortably satisfy.
                path = self._disk_dir / f"{event.task_id}.jsonl"
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event.to_jsonable(), default=str) + "\n")
            except OSError as exc:
                log.warning("trace.disk_write_failed", error=str(exc))

    def for_task(self, task_id: str) -> list[TraceEvent]:
        with self._lock:
            return list(self._by_task.get(task_id, []))

    def tasks(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._by_task.keys()))
