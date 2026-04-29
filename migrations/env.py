"""Alembic env — wires migration runs to agentcore.settings.

We don't declare SQLAlchemy models; migrations use raw `op.execute(...)` SQL
to stay close to the inline DDL the runtime already uses. Alembic just gives
us versioning, ordering, and rollback metadata.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make `agentcore` importable from a fresh CI env.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agentcore.settings import get_settings  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Build the SQLAlchemy URL from agentcore settings — single source of truth.
_settings = get_settings()
_dsn = (
    f"postgresql+psycopg://{_settings.pg_user}:{_settings.pg_password}"
    f"@{_settings.pg_host}:{_settings.pg_port}/{_settings.pg_database}"
)
config.set_main_option("sqlalchemy.url", _dsn)

# We don't have SQLAlchemy models — autogenerate is intentionally off.
target_metadata = None


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing — useful for review."""
    context.configure(
        url=_dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against the live database."""
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = _dsn
    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
