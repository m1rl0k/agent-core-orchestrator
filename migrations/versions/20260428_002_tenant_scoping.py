"""tenant scoping — add project_id to durable state tables

Revision ID: 002_tenant_scoping
Revises: 001_initial
Create Date: 2026-04-28 22:15:00 UTC

Until now `project_name` was a label sprinkled into wiki/vector collection
names but not a hard boundary on the durable state. This migration adds a
`project_id` column to every multi-tenant-relevant table, backfills existing
rows with `'default'`, and adds composite indexes so per-tenant queries hit
indexes cleanly.

After upgrade, all idempotency / jobs / graph queries MUST scope by
project_id — see the runtime store classes for the new query shape.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "002_tenant_scoping"
down_revision: str | Sequence[str] | None = "001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Tables that need project scoping. Vector chunks already encode the project
# in their `collection` name (`wiki:<project>:<branch>`, `code:<project>`),
# so they don't need a separate column.
_TENANT_TABLES: list[str] = [
    "agentcore_idempotency",
    "agentcore_jobs",
    "agentcore_graph_nodes",
    "agentcore_graph_edges",
    "agentcore_graph_communities",
    "agentcore_graph_events",
]


def upgrade() -> None:
    for tbl in _TENANT_TABLES:
        op.execute(
            f"ALTER TABLE {tbl} "
            f"ADD COLUMN IF NOT EXISTS project_id TEXT NOT NULL DEFAULT 'default'"
        )
        # Composite index puts project_id first so per-tenant scans are cheap.
        op.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{tbl}_project ON {tbl} (project_id)"
        )

    # Idempotency primary key needs project_id baked in — same key under
    # different projects must be distinct.
    op.execute(
        "ALTER TABLE agentcore_idempotency DROP CONSTRAINT IF EXISTS agentcore_idempotency_pkey"
    )
    op.execute(
        "ALTER TABLE agentcore_idempotency "
        "ADD PRIMARY KEY (project_id, scope, key)"
    )

    # Jobs claim index: include project_id so the worker query is a single
    # index lookup. Drop the old single-column claim index first.
    op.execute("DROP INDEX IF EXISTS idx_jobs_claim")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_claim "
        "ON agentcore_jobs (project_id, status, run_after, priority DESC, created_at)"
    )
    # Idempotency partial unique on (kind, idempotency_key) becomes
    # (project_id, kind, idempotency_key).
    op.execute("DROP INDEX IF EXISTS idx_jobs_idem")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idem "
        "ON agentcore_jobs (project_id, kind, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )


def downgrade() -> None:
    # Restore old primary key and indexes.
    op.execute(
        "ALTER TABLE agentcore_idempotency DROP CONSTRAINT IF EXISTS agentcore_idempotency_pkey"
    )
    op.execute(
        "ALTER TABLE agentcore_idempotency ADD PRIMARY KEY (scope, key)"
    )
    op.execute("DROP INDEX IF EXISTS idx_jobs_claim")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_claim "
        "ON agentcore_jobs (status, run_after, priority DESC, created_at)"
    )
    op.execute("DROP INDEX IF EXISTS idx_jobs_idem")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idem "
        "ON agentcore_jobs (kind, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    for tbl in _TENANT_TABLES:
        op.execute(f"DROP INDEX IF EXISTS idx_{tbl}_project")
        op.execute(f"ALTER TABLE {tbl} DROP COLUMN IF EXISTS project_id")
