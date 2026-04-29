"""durable traces and wiki pages

Revision ID: 004_traces_wiki_pages
Revises: 003_graph_pk_per_project
Create Date: 2026-04-29 00:00:00 UTC

Adds cluster-visible operational traces and a Postgres mirror of wiki pages.
Disk mirrors remain supported by runtime code for local developer workflows.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "004_traces_wiki_pages"
down_revision: str | Sequence[str] | None = "003_graph_pk_per_project"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agentcore_traces (
          id         BIGSERIAL PRIMARY KEY,
          project_id TEXT NOT NULL DEFAULT 'default',
          task_id    TEXT NOT NULL,
          step       INT NOT NULL,
          kind       TEXT NOT NULL,
          actor      TEXT NOT NULL,
          at         TIMESTAMPTZ NOT NULL DEFAULT now(),
          detail     JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE INDEX IF NOT EXISTS idx_agentcore_traces_task
          ON agentcore_traces (project_id, task_id, at);
        CREATE INDEX IF NOT EXISTS idx_agentcore_traces_project_created
          ON agentcore_traces (project_id, at DESC);
        CREATE INDEX IF NOT EXISTS idx_agentcore_traces_kind
          ON agentcore_traces (project_id, kind, at DESC);
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agentcore_wiki_pages (
          project_id    TEXT NOT NULL,
          branch        TEXT NOT NULL,
          rel           TEXT NOT NULL,
          title         TEXT NOT NULL,
          frontmatter   JSONB NOT NULL DEFAULT '{}'::jsonb,
          body          TEXT NOT NULL DEFAULT '',
          content_hash  TEXT NOT NULL DEFAULT '',
          updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (project_id, branch, rel)
        );
        CREATE INDEX IF NOT EXISTS idx_agentcore_wiki_pages_project_branch
          ON agentcore_wiki_pages (project_id, branch);
        CREATE INDEX IF NOT EXISTS idx_agentcore_wiki_pages_updated
          ON agentcore_wiki_pages (project_id, branch, updated_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agentcore_wiki_pages CASCADE")
    op.execute("DROP TABLE IF EXISTS agentcore_traces CASCADE")
