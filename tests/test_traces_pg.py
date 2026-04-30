"""Postgres-backed trace behavior without requiring a live database."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from pydantic_settings import SettingsConfigDict

import agentcore.orchestrator.traces as traces_module
from agentcore.orchestrator.traces import TraceEvent, TraceLog
from agentcore.settings import Settings


def _patch_pg_conn(monkeypatch, connection):  # type: ignore[no-untyped-def]
    """Patch the pooled `pg_conn` context manager that traces_module uses
    (was `psycopg.connect`). Yields a real context manager so the
    `with (pg_conn(...) as conn, ...)` shape stays valid."""

    @contextmanager
    def fake_pg_conn(*_args, **_kwargs):
        yield connection

    monkeypatch.setattr(traces_module, "pg_conn", fake_pg_conn)


class _IsolatedSettings(Settings):
    """Settings that ignore host `.env` so tests stay deterministic."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []
        self.statements: list[tuple[str, tuple[Any, ...] | None]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.statements.append((sql, params))
        if "DELETE FROM agentcore_traces" in sql:
            self.rowcount = 7

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor


def test_trace_log_records_in_memory_when_not_persistent() -> None:
    log = TraceLog()
    event = TraceEvent(task_id="task-1", step=1, kind="llm_call", actor="architect")

    log.record(event, project_id="tenant-a")

    assert log.for_task("task-1") == [event]


def test_trace_log_writes_project_scoped_rows(monkeypatch) -> None:
    cursor = _Cursor()
    settings = _IsolatedSettings(AGENTCORE_PROJECT_NAME="default")
    log = TraceLog(settings=None)
    log.settings = settings
    log._persistent = True

    _patch_pg_conn(monkeypatch, _Connection(cursor))

    log.record(
        TraceEvent(
            task_id="task-1",
            step=2,
            kind="verdict",
            actor="qa",
            detail={"approved": True},
        ),
        project_id="tenant-a",
    )

    insert = cursor.statements[-1]
    assert "INSERT INTO agentcore_traces" in insert[0]
    assert insert[1] is not None
    assert insert[1][0] == "tenant-a"
    assert insert[1][1] == "task-1"
    assert insert[1][3] == "verdict"


def test_trace_log_reads_project_scoped_rows(monkeypatch) -> None:
    at = datetime(2026, 4, 29, tzinfo=UTC)
    cursor = _Cursor(rows=[("task-1", 1, "result", "cli", at, {"approved": True})])
    log = TraceLog(settings=None)
    log.settings = _IsolatedSettings(AGENTCORE_PROJECT_NAME="default")
    log._persistent = True

    _patch_pg_conn(monkeypatch, _Connection(cursor))

    rows = log.for_task("task-1", project_id="tenant-a")

    assert len(rows) == 1
    assert rows[0].task_id == "task-1"
    assert rows[0].kind == "result"
    assert rows[0].detail == {"approved": True}
    assert cursor.statements[-1][1] == ("tenant-a", "task-1")


def test_trace_cleanup_uses_retention_days(monkeypatch) -> None:
    cursor = _Cursor()
    log = TraceLog(settings=None)
    log.settings = _IsolatedSettings()
    log._persistent = True

    _patch_pg_conn(monkeypatch, _Connection(cursor))

    assert log.cleanup(retention_days=14) == 7
    assert cursor.statements[-1][1] == ("14",)
