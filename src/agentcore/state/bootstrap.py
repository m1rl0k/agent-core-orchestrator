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


# ---------------------------------------------------------------------------
# Schema drift check
# ---------------------------------------------------------------------------


def verify_schema(
    settings: Settings, *, strict: bool = False
) -> tuple[bool, str | None, str | None]:
    """Compare alembic head against the live DB revision.

    Returns `(ok, head, current)`. `ok=True` means head matches what's in
    `alembic_version`. `ok=False` means either the table is missing
    (init_schema fallback ran but `alembic upgrade head` was never
    invoked) or the DB is on an older revision than the bundled
    migrations.

    On mismatch, logs a structured warning. If `strict=True` and there's
    a real mismatch (not "no DB / no alembic"), raises `RuntimeError`
    so production deploys can fail-closed when migrations are out of
    sync. Default is warn-only so dev iteration isn't blocked.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    alembic_ini = repo_root / "alembic.ini"
    if not alembic_ini.exists():
        log.info("schema.no_alembic_ini", path=str(alembic_ini))
        return True, None, None

    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
    except ImportError:
        log.info("schema.alembic_unavailable")
        return True, None, None

    try:
        cfg = Config(str(alembic_ini))
        script = ScriptDirectory.from_config(cfg)
        head = script.get_current_head()
    except Exception as exc:
        log.warning("schema.head_unreadable", error=str(exc))
        return True, None, None

    current: str | None = None
    try:
        with psycopg.connect(settings.pg_dsn, autocommit=True) as conn:
            ctx = MigrationContext.configure(conn.cursor())
            current = ctx.get_current_revision()
    except Exception as exc:
        log.warning("schema.current_unreadable", error=str(exc))
        # Can't read DB — don't block startup; ensure_postgres handles
        # connectivity separately.
        return True, head, None

    if current == head:
        log.info("schema.ok", revision=head)
        return True, head, current

    if current is None:
        msg = (
            "alembic_version table missing; running on init_schema() "
            "fallbacks. Run `agentcore migrate upgrade` to apply migrations "
            f"(head={head})."
        )
    else:
        msg = (
            f"schema drift: DB at {current!r} but bundled head is {head!r}. "
            "Run `agentcore migrate upgrade` to apply pending migrations."
        )
    log.warning("schema.drift", head=head, current=current, message=msg)
    if strict:
        raise RuntimeError(msg)
    return False, head, current
