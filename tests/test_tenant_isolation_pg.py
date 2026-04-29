"""Tenant isolation against a live Postgres.

Marked `pytest.mark.integration` so it auto-skips when no DB is reachable.
The devcontainer brings up `agentcore-postgres` on `localhost:5432`; CI
should do the same. Locally:

    docker compose up -d postgres
    uv run pytest -m integration tests/test_tenant_isolation_pg.py
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from agentcore.settings import Settings
from agentcore.state.idempotency import IdempotencyStore
from agentcore.state.jobs import JobQueue


def _pg_reachable(settings: Settings) -> bool:
    try:
        with psycopg.connect(settings.pg_dsn, autocommit=True, connect_timeout=2):
            return True
    except Exception:
        return False


@pytest.fixture
def settings_with_pg() -> Settings:
    s = Settings()
    if not _pg_reachable(s):
        pytest.skip("Postgres not reachable on this host (start it via `docker compose up -d postgres`)")
    return s


@pytest.mark.integration
def test_pg_idempotency_isolates_per_project(settings_with_pg: Settings) -> None:
    store = IdempotencyStore(settings=settings_with_pg)
    assert store._persistent, "store should have switched to Postgres mode"

    key = f"test-isolation-{uuid.uuid4().hex[:8]}"
    store.put("run", key, {"hops": ["alpha"]}, project_id="alpha-tenant")
    store.put("run", key, {"hops": ["beta"]}, project_id="beta-tenant")

    a = store.get("run", key, project_id="alpha-tenant")
    b = store.get("run", key, project_id="beta-tenant")
    assert a is not None and a["hops"] == ["alpha"]
    assert b is not None and b["hops"] == ["beta"]
    # Cross-tenant must be invisible.
    assert store.get("run", key, project_id="non-existent") is None


@pytest.mark.integration
def test_pg_jobs_isolate_per_project(settings_with_pg: Settings) -> None:
    queue = JobQueue(settings=settings_with_pg)
    queue.init_schema()
    assert queue.is_persistent

    idem = f"isolation-{uuid.uuid4().hex[:8]}"
    a_id = queue.enqueue(
        "test.tenant.kind", {"x": 1}, project_id="alpha-tenant", idempotency_key=idem
    )
    b_id = queue.enqueue(
        "test.tenant.kind", {"x": 2}, project_id="beta-tenant", idempotency_key=idem
    )
    assert a_id and b_id and a_id != b_id, "same idempotency_key under two projects must not collide"

    # Worker scoped to alpha-tenant claims only alpha's job.
    claimed_alpha = queue.claim("worker-a", project_id="alpha-tenant")
    assert claimed_alpha is not None
    assert claimed_alpha.id == a_id
    assert claimed_alpha.payload["x"] == 1

    # Worker scoped to beta-tenant claims only beta's job.
    claimed_beta = queue.claim("worker-b", project_id="beta-tenant")
    assert claimed_beta is not None
    assert claimed_beta.id == b_id
    assert claimed_beta.payload["x"] == 2

    queue.complete(a_id)
    queue.complete(b_id)


@pytest.mark.integration
def test_pg_idempotency_same_key_different_scopes(settings_with_pg: Settings) -> None:
    store = IdempotencyStore(settings=settings_with_pg)
    key = f"scope-test-{uuid.uuid4().hex[:8]}"
    store.put("run", key, {"a": 1}, project_id="alpha-tenant")
    store.put("handoff", key, {"b": 2}, project_id="alpha-tenant")
    assert store.get("run", key, project_id="alpha-tenant") == {"a": 1}
    assert store.get("handoff", key, project_id="alpha-tenant") == {"b": 2}
