"""UI graph projection helpers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import agentcore.ui.routes as routes


def test_known_projects_includes_graph_edges_and_traces(monkeypatch) -> None:
    class _Settings:
        project_name = "default"

    class _Cursor:
        sql = ""

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str) -> None:
            self.sql = sql

        def fetchall(self) -> list[tuple[str]]:
            return [("trace-only",), ("edge-only",)]

    class _Connection:
        def __init__(self, cursor: _Cursor) -> None:
            self._cursor = cursor

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def cursor(self) -> _Cursor:
            return self._cursor

    cursor = _Cursor()

    @contextmanager
    def fake_pg_conn(_settings, **_kwargs: Any):
        yield _Connection(cursor)

    monkeypatch.setattr(routes, "pg_conn", fake_pg_conn)

    assert routes._known_projects(_Settings()) == ["default", "trace-only", "edge-only"]
    assert "agentcore_graph_edges" in cursor.sql
    assert "agentcore_traces" in cursor.sql


def test_graph_snapshot_scopes_edge_weight_join_to_project(monkeypatch) -> None:
    class _Cursor:
        def __init__(self) -> None:
            self.statements: list[str] = []
            self._fetch = 0

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str, _params: tuple[Any, ...]) -> None:
            self.statements.append(sql)

        def fetchall(self) -> list[tuple[Any, ...]]:
            self._fetch += 1
            if self._fetch == 1:
                return [("task:abc", "task", {}, 1.0)]
            return []

    class _Connection:
        def __init__(self, cursor: _Cursor) -> None:
            self._cursor = cursor

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def cursor(self) -> _Cursor:
            return self._cursor

    cursor = _Cursor()

    @contextmanager
    def fake_pg_conn(_settings):
        yield _Connection(cursor)

    monkeypatch.setattr(routes, "pg_conn", fake_pg_conn)

    nodes, edges, kinds = routes._graph_snapshot(
        object(), limit_nodes=1000, project_id="prior-project"
    )

    assert nodes == [
        {"id": "task:abc", "kind": "task", "label": "abc", "score": 1.0, "attrs": {}}
    ]
    assert edges == []
    assert kinds == {"task": 1}
    assert "ON (e.source = n.id OR e.target = n.id)" in cursor.statements[0]
