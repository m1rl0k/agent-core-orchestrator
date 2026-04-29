"""Auto-start helper for the Postgres dependency.

The CLI should start cleanly even when Postgres isn't running. For commands
that need it (`migrate`, `index`, `wiki rebuild|search|refresh`) we probe
first; if the DSN refuses, we try to bring up the docker-compose `postgres`
service (when docker is available) and wait for `pg_isready`.

Failure to start is surfaced cleanly — the CLI exits with a hint instead
of dumping a SQLAlchemy / psycopg traceback.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import psycopg
import structlog

from agentcore.settings import Settings

log = structlog.get_logger(__name__)


def is_postgres_ready(settings: Settings, *, timeout: float = 2.0) -> bool:
    """Return True iff a connect() succeeds within `timeout` seconds."""
    try:
        with psycopg.connect(
            settings.pg_dsn, autocommit=True, connect_timeout=timeout
        ):
            return True
    except Exception:
        return False


def docker_available() -> bool:
    return shutil.which("docker") is not None


def _compose_file_path() -> Path | None:
    here = Path.cwd()
    for parent in [here, *here.parents]:
        cand = parent / "docker-compose.yml"
        if cand.exists():
            return cand
    return None


def ensure_postgres(
    settings: Settings,
    *,
    boot_timeout: float = 30.0,
    poll_interval: float = 1.0,
) -> bool:
    """Probe Postgres; if down, try to start it via docker-compose.

    Returns True if Postgres is reachable at the end of the call. False if
    we couldn't start it (no docker / no compose file / boot timeout).
    Callers should surface a clean message and exit on False — never
    throw a connection traceback at the user.
    """
    if is_postgres_ready(settings):
        return True

    compose = _compose_file_path()
    if not docker_available() or compose is None:
        log.warning(
            "postgres.unreachable",
            docker_available=docker_available(),
            compose_file=str(compose) if compose else None,
            dsn_host=settings.pg_host,
            dsn_port=settings.pg_port,
        )
        return False

    log.info("postgres.auto_starting", compose=str(compose))
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(compose), "up", "-d", "postgres"],
            check=False,
            capture_output=True,
            text=True,
            timeout=boot_timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("postgres.compose_up_failed", error=str(exc))
        return False

    deadline = time.monotonic() + boot_timeout
    while time.monotonic() < deadline:
        if is_postgres_ready(settings):
            log.info("postgres.up")
            return True
        time.sleep(poll_interval)
    log.warning("postgres.boot_timeout", timeout_seconds=boot_timeout)
    return False
