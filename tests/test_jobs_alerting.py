"""Dead-letter alerting for durable jobs."""

from __future__ import annotations

from typing import Any

from pydantic_settings import SettingsConfigDict

import agentcore.state.jobs as jobs_module
from agentcore.settings import Settings
from agentcore.state.jobs import JobQueue


class _IsolatedSettings(Settings):
    """Settings that ignore any host `.env` so tests stay deterministic."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def error(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))


class _Cursor:
    def __init__(self, row: tuple[Any, ...], *, update_rowcount: int = 1) -> None:
        self._row = row
        self._update_rowcount = update_rowcount
        self.rowcount = 0
        self.statements: list[str] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, _params: tuple[Any, ...]) -> None:
        self.statements.append(sql)
        if "UPDATE agentcore_jobs" in sql:
            self.rowcount = self._update_rowcount

    def fetchone(self) -> tuple[Any, ...]:
        return self._row


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor


def test_dead_letter_transition_emits_structured_alert(monkeypatch) -> None:
    cursor = _Cursor(("tenant-a", "wiki_refresh", 3, 3, "curator"))
    recorder = _Recorder()
    queue = JobQueue(settings=_IsolatedSettings())
    queue._persistent = True

    monkeypatch.setattr(jobs_module, "log", recorder)
    monkeypatch.setattr(
        jobs_module.psycopg,
        "connect",
        lambda *_args, **_kwargs: _Connection(cursor),
    )

    queue.fail(42, "boom")

    assert recorder.events == [
        (
            "jobs.dead_letter",
            {
                "job_id": 42,
                "project_id": "tenant-a",
                "kind": "wiki_refresh",
                "attempts": 3,
                "max_attempts": 3,
                "created_by": "curator",
                "error": "boom",
            },
        )
    ]


def test_retryable_failure_does_not_emit_dead_letter_alert(monkeypatch) -> None:
    cursor = _Cursor(("tenant-a", "wiki_refresh", 1, 3, "curator"))
    recorder = _Recorder()
    queue = JobQueue(settings=_IsolatedSettings())
    queue._persistent = True

    monkeypatch.setattr(jobs_module, "log", recorder)
    monkeypatch.setattr(
        jobs_module.psycopg,
        "connect",
        lambda *_args, **_kwargs: _Connection(cursor),
    )

    queue.fail(42, "try again")

    assert recorder.events == []
    assert any("SET status = 'queued'" in stmt for stmt in cursor.statements)
