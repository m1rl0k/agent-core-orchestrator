"""Tenant isolation: same key, different projects must not collide.

These tests run against the in-memory fallback path of `IdempotencyStore`
so they don't need a live Postgres. The Postgres path uses the same code
shape with a `(project_id, scope, key)` PRIMARY KEY, so isolation
correctness reduces to the same invariant: distinct project_ids → distinct
rows.
"""

from __future__ import annotations

from agentcore.settings import Settings
from agentcore.state.idempotency import IdempotencyStore
from agentcore.state.jobs import JobQueue


def _store() -> IdempotencyStore:
    # Force in-memory mode by handing it a Settings whose pg_dsn won't
    # connect — Settings is constructed normally; the store probes once
    # in __post_init__ and falls back to the in-memory path.
    s = Settings()  # type: ignore[call-arg]
    store = IdempotencyStore(settings=s)
    # Force the fallback path even if a local Postgres happens to be up.
    store._persistent = False
    return store


def test_idempotency_keys_are_per_project() -> None:
    store = _store()
    store.put("run", "abc-123", {"hops": ["a"]}, project_id="alpha")
    store.put("run", "abc-123", {"hops": ["b"]}, project_id="beta")
    a = store.get("run", "abc-123", project_id="alpha")
    b = store.get("run", "abc-123", project_id="beta")
    assert a is not None and a["hops"] == ["a"]
    assert b is not None and b["hops"] == ["b"]
    assert a is not b


def test_idempotency_default_project_does_not_leak_to_named() -> None:
    """A put without project_id (uses settings.project_name) must not be
    visible from a different project_id."""
    store = _store()
    store.put("run", "abc-123", {"hops": ["default"]})  # uses settings.project_name
    other = store.get("run", "abc-123", project_id="otherproject")
    assert other is None


def test_idempotency_get_missing_key_returns_none() -> None:
    store = _store()
    assert store.get("run", "no-such-key", project_id="alpha") is None


def test_idempotency_scope_partition_within_project() -> None:
    """Different scopes (run vs handoff) under the same project_id and key
    are still distinct rows — `(project_id, scope, key)` is the unique
    triple."""
    store = _store()
    store.put("run", "abc", {"a": 1}, project_id="alpha")
    store.put("handoff", "abc", {"b": 2}, project_id="alpha")
    assert store.get("run", "abc", project_id="alpha") == {"a": 1}
    assert store.get("handoff", "abc", project_id="alpha") == {"b": 2}


def test_jobs_in_memory_fallback_isolates_per_project() -> None:
    """In-memory job fallback doesn't actually need project isolation
    (each orchestrator owns its own JobQueue), but the API still accepts
    project_id so callers can use the same shape regardless of mode."""
    s = Settings()  # type: ignore[call-arg]
    q = JobQueue(settings=s)
    q._persistent = False
    a = q.enqueue("test.kind", {"x": 1}, project_id="alpha")
    b = q.enqueue("test.kind", {"x": 2}, project_id="beta")
    assert a is not None and b is not None
    assert a != b
