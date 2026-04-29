"""Process-wide psycopg connection pool — single-node scaling primitive.

Every Postgres-backed subsystem (idempotency, jobs, wiki/storage, traces,
UI read endpoints, vector store) used to call `psycopg.connect()` per
operation. That's a TCP handshake + authentication round-trip on every
request — under webhook fan-out and worker polling it dominates request
latency and exhausts Postgres's `max_connections`.

This module owns one shared `ConnectionPool` per process. All callers use
`pg_conn()` to check out an autocommit connection; the pool reuses warm
connections, registers pgvector once per connection, and bounds the
total connection count.

Single-node by design. Multi-process deploys still work — each worker
process holds its own pool against shared Postgres. Cross-node coherence
is not attempted because no in-memory state lives in the pool.

Falls back to direct `psycopg.connect()` when `psycopg_pool` isn't
installed so the minimum-deps install path stays functional.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import structlog

from agentcore.settings import Settings, get_settings

log = structlog.get_logger(__name__)

_lock = threading.Lock()
_pool: Any | None = None
_pool_dsn: str | None = None
_pool_available: bool = True


def _configure_conn(conn: psycopg.Connection) -> None:
    """Per-connection setup. Registers pgvector adapters when the
    extension is installed; harmless for non-vector callers since the
    adapters only fire on `vector` types."""
    try:
        from pgvector.psycopg import register_vector

        register_vector(conn)
    except Exception:
        # pgvector extension not installed in this DB, or pgvector
        # python lib missing — non-vector callers don't care.
        pass


def _build_pool(dsn: str, *, max_size: int) -> Any | None:
    global _pool_available
    try:
        from psycopg_pool import ConnectionPool
    except ImportError:
        _pool_available = False
        log.info("db.pool_unavailable", reason="psycopg_pool not installed")
        return None
    return ConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=max_size,
        kwargs={"autocommit": True},
        configure=_configure_conn,
        open=True,
    )


def get_pool(settings: Settings | None = None) -> Any | None:
    """Return the shared `ConnectionPool`, lazily constructing it on first
    use. Returns None when `psycopg_pool` isn't installed; callers should
    fall back to `psycopg.connect()` via `pg_conn()`."""
    global _pool, _pool_dsn
    if not _pool_available:
        return None
    if _pool is not None:
        return _pool
    with _lock:
        if _pool is not None:
            return _pool
        s = settings or get_settings()
        # 16 default pool size: enough for a small worker fleet
        # (concurrent claims) plus a handful of HTTP request workers
        # without overwhelming Postgres's default `max_connections=100`.
        _pool = _build_pool(s.pg_dsn, max_size=16)
        _pool_dsn = s.pg_dsn
        if _pool is not None:
            log.info("db.pool_open", max_size=16)
        return _pool


@contextmanager
def pg_conn(
    settings: Settings | None = None,
    *,
    timeout: float | None = None,
) -> Iterator[psycopg.Connection]:
    """Hand out a pooled autocommit connection. `timeout` bounds how long
    we wait for the pool to free up; None uses the pool's default.

    Falls back to a fresh `psycopg.connect()` when the pool is
    unavailable, preserving behaviour on minimum-deps installs."""
    pool = get_pool(settings)
    if pool is not None:
        kw: dict[str, Any] = {}
        if timeout is not None:
            kw["timeout"] = timeout
        with pool.connection(**kw) as conn:
            yield conn
        return
    s = settings or get_settings()
    extra: dict[str, Any] = {}
    if timeout is not None:
        extra["connect_timeout"] = max(1, int(timeout))
    conn = psycopg.connect(s.pg_dsn, autocommit=True, **extra)
    try:
        _configure_conn(conn)
        yield conn
    finally:
        conn.close()


def close_pool() -> None:
    """Best-effort pool shutdown — wire into FastAPI lifespan if you want
    a clean Postgres-side disconnect on shutdown. Tests use this between
    fixtures that need to reset the pool against a different DSN."""
    global _pool, _pool_dsn
    with _lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception:
                pass
            _pool = None
            _pool_dsn = None
