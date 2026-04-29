"""initial schema — chunks, graph, idempotency, jobs

Revision ID: 001_initial
Revises:
Create Date: 2026-04-28 21:30:00 UTC

Single source of truth: each module owns its DDL constant (CREATE TABLE
IF NOT EXISTS + indexes). This migration imports those constants and
applies them — running on a fresh DB creates everything; running on a
DB that's already had `init_schema()` called is a no-op.

Subsequent migrations (002+) will use plain `op.execute(...)` ALTER
statements; they don't import from runtime modules so the historical
record is reproducible even if the runtime DDL drifts.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Vector store + the 768-dim default. Operators can override
    # AGENTCORE_EMBED_MODEL to a different-dim model; in that case the
    # `init_schema(dim=...)` runtime call does the right thing — this
    # migration just covers the standard path.
    from agentcore.memory.embed import EMBED_DIM
    from agentcore.memory.vector import _build_ddl

    op.execute(_build_ddl(EMBED_DIM))

    # Operational graph (nodes / edges / communities / events).
    from agentcore.memory.graph import GRAPH_DDL

    op.execute(GRAPH_DDL)

    # Durable idempotency cache for /run, /handoff, /signal, /wiki/refresh.
    from agentcore.state.idempotency import DDL as IDEM_DDL

    op.execute(IDEM_DDL)

    # Jobs queue — `FOR UPDATE SKIP LOCKED` claim pattern.
    from agentcore.state.jobs import DDL as JOBS_DDL

    op.execute(JOBS_DDL)


def downgrade() -> None:
    # Order matters: drop dependents first (FK-free, but still safer).
    op.execute("DROP TABLE IF EXISTS agentcore_jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS agentcore_idempotency CASCADE")
    op.execute("DROP TABLE IF EXISTS agentcore_graph_events CASCADE")
    op.execute("DROP TABLE IF EXISTS agentcore_graph_communities CASCADE")
    op.execute("DROP TABLE IF EXISTS agentcore_graph_edges CASCADE")
    op.execute("DROP TABLE IF EXISTS agentcore_graph_nodes CASCADE")
    op.execute("DROP TABLE IF EXISTS agentcore_chunks CASCADE")
