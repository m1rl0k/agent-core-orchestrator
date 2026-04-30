"""UI chain detail cache helpers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import agentcore.ui.routes as routes


def test_cached_reuses_value_within_ttl(monkeypatch) -> None:
    routes._CACHE.clear()
    now = [100.0]
    calls = {"count": 0}

    monkeypatch.setattr(routes.time, "monotonic", lambda: now[0])

    def load() -> dict[str, int]:
        calls["count"] += 1
        return {"value": calls["count"]}

    assert routes._cached(("proj", "chain_detail:abc"), load) == {"value": 1}
    assert routes._cached(("proj", "chain_detail:abc"), load) == {"value": 1}
    assert calls["count"] == 1

    now[0] += routes._CACHE_TTL + 0.1
    assert routes._cached(("proj", "chain_detail:abc"), load) == {"value": 2}
    assert calls["count"] == 2


def test_load_chain_detail_unifies_sources(monkeypatch) -> None:
    class _Idem:
        def get(self, scope: str, key: str, *, project_id: str):
            assert scope == "chain"
            assert key == "chain-1"
            assert project_id == "tenant-a"
            return {"chain_id": key, "status": "done", "hops": []}

    monkeypatch.setattr(routes, "_chain_jobs", lambda *_a, **_k: [{"id": 1}])
    monkeypatch.setattr(routes, "_chain_review_history", lambda *_a, **_k: [{"kind": "result"}])
    monkeypatch.setattr(
        routes,
        "_chain_detail_from_graph",
        lambda *_a, **_k: {
            "chain_id": "chain-1",
            "status": "done",
            "hops": [{"agent": "developer"}],
            "files_touched": ["src/app.py"],
            "snippets_produced": [{"role": "developer", "file": "src/app.py"}],
        },
    )

    detail = routes._load_chain_detail(
        object(), object(), _Idem(), "chain-1", project_id="tenant-a"
    )

    assert detail == {
        "chain": {
            "chain_id": "chain-1",
            "status": "done",
            "hops": [],
            "files_touched": ["src/app.py"],
            "snippets_produced": [{"role": "developer", "file": "src/app.py"}],
        },
        "chain_jobs": [{"id": 1}],
        "review_history": [{"kind": "result"}],
    }


def test_chain_jobs_matches_runtime_and_followup_jobs(monkeypatch) -> None:
    class _Cursor:
        def __init__(self) -> None:
            self.params: tuple[Any, ...] | None = None

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, _sql: str, params: tuple[Any, ...]) -> None:
            self.params = params

        def fetchall(self) -> list[tuple[Any, ...]]:
            return [
                (
                    7,
                    "wiki.refresh.incremental",
                    "done",
                    1,
                    3,
                    None,
                    None,
                    None,
                    None,
                    "chain:chain-1",
                    None,
                )
            ]

    class _Connection:
        def __init__(self, cursor: _Cursor) -> None:
            self._cursor = cursor

        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def cursor(self) -> _Cursor:
            return self._cursor

    class _Queue:
        is_persistent = True
        settings = object()

    cursor = _Cursor()

    @contextmanager
    def fake_pg_conn(_settings):
        yield _Connection(cursor)

    monkeypatch.setattr(routes, "pg_conn", fake_pg_conn)

    rows = routes._chain_jobs(_Queue(), "chain-1", project_id="tenant-a")

    assert cursor.params == (
        "tenant-a",
        "chain-1",
        "chain-1",
        "chain:chain-1",
        "chain-1:%",
        "chain:chain-1",
    )
    assert rows[0]["kind"] == "wiki.refresh.incremental"
    assert rows[0]["created_by"] == "chain:chain-1"
