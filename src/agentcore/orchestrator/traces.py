"""In-memory trace log keyed by task_id.

A trace records every (handoff received, llm called, outcome emitted) tuple
for a single task. Persisting to disk is a future concern; the structlog
sink already gives us a tail of every event.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

EventKind = Literal["handoff_in", "llm_call", "outcome", "error", "signal"]


@dataclass(slots=True)
class TraceEvent:
    task_id: str
    step: int
    kind: EventKind
    actor: str
    at: datetime = field(default_factory=lambda: datetime.now(UTC))
    detail: dict[str, Any] = field(default_factory=dict)


class TraceLog:
    """Append-only ring per task. Thread-safe; suitable for asyncio + threads."""

    def __init__(self, max_per_task: int = 500) -> None:
        self._by_task: dict[str, list[TraceEvent]] = {}
        self._lock = threading.RLock()
        self._max = max_per_task

    def record(self, event: TraceEvent) -> None:
        with self._lock:
            bucket = self._by_task.setdefault(event.task_id, [])
            bucket.append(event)
            if len(bucket) > self._max:
                del bucket[: len(bucket) - self._max]

    def for_task(self, task_id: str) -> list[TraceEvent]:
        with self._lock:
            return list(self._by_task.get(task_id, []))

    def tasks(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._by_task.keys()))
