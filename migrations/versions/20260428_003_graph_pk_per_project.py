"""graph_pk_per_project — make graph node id unique per project, not globally

Revision ID: 003_graph_pk_per_project
Revises: 002_tenant_scoping
Create Date: 2026-04-28 22:35:00 UTC

Migration 002 added `project_id` columns to the graph tables but left the
primary keys global. That meant `agent:architect` under project A and the
same id under project B collided in `ON CONFLICT (id) DO UPDATE`, with the
later write silently mutating the earlier project's row.

This migration:
  - drops the global PK on `agentcore_graph_nodes(id)` and replaces it
    with `(project_id, id)`
  - replaces the FK-free unique on `agentcore_graph_edges(source, target,
    relation)` with `(project_id, source, target, relation)`

Once applied, ON CONFLICT in the runtime can target the composite key and
multi-tenant writes stay properly partitioned.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "003_graph_pk_per_project"
down_revision: str | Sequence[str] | None = "002_tenant_scoping"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop every FK that references the old nodes PK so we can replace it.
    op.execute(
        "ALTER TABLE agentcore_graph_edges "
        "DROP CONSTRAINT IF EXISTS agentcore_graph_edges_source_fkey"
    )
    op.execute(
        "ALTER TABLE agentcore_graph_edges "
        "DROP CONSTRAINT IF EXISTS agentcore_graph_edges_target_fkey"
    )
    op.execute(
        "ALTER TABLE agentcore_graph_communities "
        "DROP CONSTRAINT IF EXISTS agentcore_graph_communities_node_id_fkey"
    )

    # Nodes: drop global PK on id, replace with (project_id, id).
    op.execute(
        "ALTER TABLE agentcore_graph_nodes "
        "DROP CONSTRAINT IF EXISTS agentcore_graph_nodes_pkey"
    )
    op.execute(
        "ALTER TABLE agentcore_graph_nodes "
        "ADD CONSTRAINT agentcore_graph_nodes_pkey PRIMARY KEY (project_id, id)"
    )

    # Recreate FKs against the new composite key. Edges and communities
    # all gained a project_id column in 002, so the join is on
    # (project_id, source/target/node_id) -> (project_id, id).
    op.execute(
        "ALTER TABLE agentcore_graph_edges "
        "ADD CONSTRAINT agentcore_graph_edges_source_fkey "
        "FOREIGN KEY (project_id, source) "
        "REFERENCES agentcore_graph_nodes (project_id, id) "
        "ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE agentcore_graph_edges "
        "ADD CONSTRAINT agentcore_graph_edges_target_fkey "
        "FOREIGN KEY (project_id, target) "
        "REFERENCES agentcore_graph_nodes (project_id, id) "
        "ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE agentcore_graph_communities "
        "ADD CONSTRAINT agentcore_graph_communities_node_id_fkey "
        "FOREIGN KEY (project_id, node_id) "
        "REFERENCES agentcore_graph_nodes (project_id, id) "
        "ON DELETE CASCADE"
    )

    # Edges: drop global unique constraint, replace with tenant-scoped composite.
    op.execute(
        "ALTER TABLE agentcore_graph_edges "
        "DROP CONSTRAINT IF EXISTS agentcore_graph_edges_source_target_relation_key"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS agentcore_graph_edges_uniq_per_project "
        "ON agentcore_graph_edges (project_id, source, target, relation)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agentcore_graph_nodes "
        "DROP CONSTRAINT IF EXISTS agentcore_graph_nodes_pkey"
    )
    op.execute(
        "ALTER TABLE agentcore_graph_nodes ADD PRIMARY KEY (id)"
    )
    op.execute(
        "DROP INDEX IF EXISTS agentcore_graph_edges_uniq_per_project"
    )
    op.execute(
        "ALTER TABLE agentcore_graph_edges "
        "ADD CONSTRAINT agentcore_graph_edges_source_target_relation_key "
        "UNIQUE (source, target, relation)"
    )
