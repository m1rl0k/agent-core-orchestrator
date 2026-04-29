"""Postgres-backed operational knowledge graph with a NetworkX compute mirror.

The durable store is Postgres graph tables. NetworkX remains the in-process
algorithm layer for graphify subgraph ingestion, tests, local traversals, and
community detection.

Primary graph shape:

  agent  ─[handoff]─▶ agent
  agent  ─[worked_on]─▶ task
  task   ─[changed]──▶ file
  file   ─[contains]─▶ symbol      (from graphify)
  symbol ─[calls]────▶ symbol      (from graphify, via merge_subgraph)
  task   ─[blast_radius]─▶ symbol  (from graphify.impact, via record_impact)
  task   ─[outcome]──▶ status
  task   ─[produced]─▶ snippet

Postgres is authoritative when settings are supplied. Without settings, this
class runs in memory-only mode, preserving fast unit tests and local algorithms.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import networkx as nx
import psycopg

from agentcore.settings import Settings

GRAPH_DDL = """
CREATE TABLE IF NOT EXISTS agentcore_graph_nodes (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  attrs JSONB NOT NULL DEFAULT '{}'::jsonb,
  labels JSONB NOT NULL DEFAULT '{}'::jsonb,
  first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_nodes_kind
  ON agentcore_graph_nodes(kind);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_nodes_attrs_gin
  ON agentcore_graph_nodes USING GIN(attrs);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_nodes_labels_gin
  ON agentcore_graph_nodes USING GIN(labels);

CREATE TABLE IF NOT EXISTS agentcore_graph_edges (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL REFERENCES agentcore_graph_nodes(id) ON DELETE CASCADE,
  target TEXT NOT NULL REFERENCES agentcore_graph_nodes(id) ON DELETE CASCADE,
  relation TEXT NOT NULL,
  weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  active_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
  evidence_count INTEGER NOT NULL DEFAULT 1,
  attrs JSONB NOT NULL DEFAULT '{}'::jsonb,
  first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(source, target, relation)
);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_edges_source
  ON agentcore_graph_edges(source);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_edges_target
  ON agentcore_graph_edges(target);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_edges_relation
  ON agentcore_graph_edges(relation);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_edges_last_seen
  ON agentcore_graph_edges(last_seen DESC);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_edges_active_weight
  ON agentcore_graph_edges(active_weight DESC);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_edges_attrs_gin
  ON agentcore_graph_edges USING GIN(attrs);

CREATE TABLE IF NOT EXISTS agentcore_graph_communities (
  id BIGSERIAL PRIMARY KEY,
  node_id TEXT NOT NULL REFERENCES agentcore_graph_nodes(id) ON DELETE CASCADE,
  community_id INTEGER NOT NULL,
  detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  community_size INTEGER NOT NULL,
  mean_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  community_type TEXT NOT NULL DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_communities_node
  ON agentcore_graph_communities(node_id);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_communities_detected
  ON agentcore_graph_communities(detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_communities_type
  ON agentcore_graph_communities(community_type);

CREATE TABLE IF NOT EXISTS agentcore_graph_events (
  id BIGSERIAL PRIMARY KEY,
  task_id TEXT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  attrs JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_events_task
  ON agentcore_graph_events(task_id);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_events_actor
  ON agentcore_graph_events(actor);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_events_action
  ON agentcore_graph_events(action);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_events_created
  ON agentcore_graph_events(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agentcore_graph_events_attrs_gin
  ON agentcore_graph_events USING GIN(attrs);
"""


@dataclass(slots=True)
class CommunitySummary:
    id: int
    size: int
    members: list[str]
    label: str = ""


class KnowledgeGraph:
    """Operational/code graph facade.

    Args:
        snapshot_path: Legacy JSON snapshot path. Used only in memory-only mode.
        settings: When supplied, graph mutations are persisted to Postgres.
    """

    def __init__(
        self,
        snapshot_path: Path | str = ".agentcore/graph.json",
        settings: Settings | None = None,
        *,
        project_id: str | None = None,
    ) -> None:
        self.path = Path(snapshot_path)
        self.settings = settings
        # Tenant boundary. When `project_id` isn't set explicitly, fall
        # back to settings.project_name. Graph mutations stamp this on
        # every row so multi-tenant deployments stay isolated; load() and
        # operational_memory() filter by it too.
        self.project_id = (
            project_id if project_id is not None
            else (settings.project_name if settings else "default")
        )
        self.g: nx.Graph = nx.Graph()
        self._communities: dict[str, int] = {}

    @property
    def is_persistent(self) -> bool:
        return self.settings is not None

    def _conn(self) -> psycopg.Connection:
        if self.settings is None:
            raise RuntimeError("KnowledgeGraph is running in memory-only mode")
        return psycopg.connect(self.settings.pg_dsn, autocommit=True)

    def init_schema(self) -> None:
        """Create durable graph tables when Postgres persistence is enabled."""
        if not self.is_persistent:
            return
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(GRAPH_DDL)

    def record_event(
        self,
        task_id: str | None,
        actor: str,
        action: str,
        *,
        subject: str = "",
        **attrs: Any,
    ) -> None:
        """Append a time-series audit event for shared swarm memory."""
        if not self.is_persistent:
            events = self.g.graph.setdefault("events", [])
            if isinstance(events, list):
                events.append(
                    {
                        "task_id": task_id,
                        "actor": actor,
                        "action": action,
                        "subject": subject,
                        "attrs": attrs,
                    }
                )
            return
        sql = """
        INSERT INTO agentcore_graph_events
          (project_id, task_id, actor, action, subject, attrs)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (self.project_id, task_id, actor, action, subject, json.dumps(attrs)),
            )

    # ---- generic mutation ----------------------------------------------

    @staticmethod
    def _infer_kind(node: str) -> str:
        if ":" in node:
            return node.split(":", 1)[0]
        return "unknown"

    @staticmethod
    def _json_obj(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _persist_node(self, node: str, attrs: dict[str, Any]) -> None:
        if not self.is_persistent:
            return
        labels = self._json_obj(attrs.get("labels"))
        clean_attrs = {k: v for k, v in attrs.items() if k != "labels"}
        kind = str(clean_attrs.get("kind") or self._infer_kind(node))
        sql = """
        INSERT INTO agentcore_graph_nodes (project_id, id, kind, attrs, labels)
        VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
        ON CONFLICT (project_id, id) DO UPDATE SET
          kind = EXCLUDED.kind,
          attrs = agentcore_graph_nodes.attrs || EXCLUDED.attrs,
          labels = agentcore_graph_nodes.labels || EXCLUDED.labels,
          last_seen = now(),
          updated_at = now()
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (self.project_id, node, kind, json.dumps(clean_attrs), json.dumps(labels)),
            )

    def _persist_edge(self, u: str, v: str, weight: float, attrs: dict[str, Any]) -> None:
        if not self.is_persistent:
            return
        relation = str(attrs.get("relation", "related"))
        clean_attrs = {k: val for k, val in attrs.items() if k != "relation"}
        sql = """
        INSERT INTO agentcore_graph_edges
          (project_id, source, target, relation, weight, active_weight, evidence_count, attrs)
        VALUES (%s, %s, %s, %s, %s, %s, 1, %s::jsonb)
        ON CONFLICT (project_id, source, target, relation) DO UPDATE SET
          weight = agentcore_graph_edges.weight + EXCLUDED.weight,
          active_weight = agentcore_graph_edges.weight + EXCLUDED.weight,
          evidence_count = agentcore_graph_edges.evidence_count + 1,
          attrs = agentcore_graph_edges.attrs || EXCLUDED.attrs,
          last_seen = now(),
          updated_at = now()
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                sql,
                (self.project_id, u, v, relation, weight, weight, json.dumps(clean_attrs)),
            )

    def add_node(self, node: str, **attrs: Any) -> None:
        merged = dict(self.g.nodes[node]) if node in self.g else {}
        merged.update(attrs)
        merged.setdefault("kind", self._infer_kind(node))
        self.g.add_node(node, **merged)
        self._persist_node(node, merged)

    def add_edge(self, u: str, v: str, weight: float = 1.0, **attrs: Any) -> None:
        if u not in self.g:
            self.add_node(u, kind=self._infer_kind(u))
        if v not in self.g:
            self.add_node(v, kind=self._infer_kind(v))

        relation = str(attrs.get("relation", "related"))
        edge_attrs = {**attrs, "relation": relation}
        if self.g.has_edge(u, v):
            self.g[u][v]["weight"] = self.g[u][v].get("weight", 1.0) + weight
            self.g[u][v]["evidence_count"] = self.g[u][v].get("evidence_count", 1) + 1
            self.g[u][v].update(edge_attrs)
        else:
            self.g.add_edge(u, v, weight=weight, evidence_count=1, **edge_attrs)
        self._persist_edge(u, v, weight, edge_attrs)

    # ---- operational events --------------------------------------------
    # Stable node prefixes:
    #   agent:<name>     ·  task:<id>     ·  file:<rel_path>     ·  status:<name>

    def record_handoff(
        self,
        task_id: str,
        from_agent: str,
        to_agent: str,
        *,
        created_by: str | None = None,
    ) -> None:
        a, b = f"agent:{from_agent}", f"agent:{to_agent}"
        t = f"task:{task_id}"
        actor = created_by or to_agent
        self.add_node(a, kind="agent")
        self.add_node(b, kind="agent")
        self.add_node(t, kind="task", created_by=actor)
        self.add_edge(a, b, weight=1.0, relation="handoff", task_id=task_id, created_by=actor)
        self.add_edge(a, t, weight=1.0, relation="worked_on", task_id=task_id, created_by=actor)
        self.add_edge(b, t, weight=1.0, relation="worked_on", task_id=task_id, created_by=actor)
        self.record_event(
            task_id,
            actor,
            "handoff",
            subject=f"{from_agent}->{to_agent}",
            from_agent=from_agent,
            to_agent=to_agent,
        )

    def record_change(
        self, task_id: str, file_path: str, *, created_by: str | None = None
    ) -> None:
        t = f"task:{task_id}"
        f = f"file:{file_path}"
        actor = created_by or "unknown"
        self.add_node(t, kind="task", created_by=actor)
        self.add_node(f, kind="file", path=file_path)
        self.add_edge(t, f, weight=1.0, relation="changed", task_id=task_id, created_by=actor)
        self.record_event(task_id, actor, "changed", subject=file_path)

    def record_outcome(
        self, task_id: str, status: str, *, created_by: str | None = None
    ) -> None:
        t = f"task:{task_id}"
        s = f"status:{status}"
        actor = created_by or "unknown"
        self.add_node(t, kind="task", created_by=actor)
        self.add_node(s, kind="status", status=status)
        self.add_edge(t, s, weight=1.0, relation="outcome", task_id=task_id, created_by=actor)
        self.record_event(task_id, actor, "outcome", subject=status)

    def record_impact(
        self,
        task_id: str,
        file_path: str,
        downstream: list[str],
        *,
        created_by: str | None = None,
    ) -> None:
        """Wire a task to a file and that file's downstream symbols."""
        t = f"task:{task_id}"
        f = f"file:{file_path}"
        actor = created_by or "graphify"
        self.add_node(t, kind="task", created_by=actor)
        self.add_node(f, kind="file", path=file_path)
        self.add_edge(t, f, weight=1.0, relation="changed", task_id=task_id, created_by=actor)
        for sym in downstream:
            s = f"symbol:{sym}"
            self.add_node(s, kind="symbol", symbol=sym, created_by=actor)
            self.add_edge(f, s, weight=0.5, relation="contains", task_id=task_id, created_by=actor)
            self.add_edge(t, s, weight=0.5, relation="blast_radius", task_id=task_id, created_by=actor)
        self.record_event(
            task_id,
            actor,
            "impact",
            subject=file_path,
            downstream_count=len(downstream),
        )

    def record_snippet(
        self,
        task_id: str,
        file_path: str,
        *,
        start: int,
        end: int,
        content: str,
        intent: str = "",
        role: str = "",
        created_by: str | None = None,
    ) -> str:
        """Persist a code snippet produced/touched by a task."""
        actor = created_by or role or "unknown"
        node = f"snippet:{file_path}:{start}-{end}#{task_id}"
        self.add_node(
            node,
            kind="snippet",
            file=file_path,
            start=start,
            end=end,
            content=content[:4000],
            intent=intent,
            role=role,
            created_by=actor,
            relevance="pending",
        )
        t = f"task:{task_id}"
        f = f"file:{file_path}"
        self.add_node(t, kind="task", created_by=actor)
        self.add_node(f, kind="file", path=file_path)
        self.add_edge(t, node, weight=1.0, relation="produced", task_id=task_id, created_by=actor)
        self.add_edge(f, node, weight=1.0, relation="contains_snippet", task_id=task_id, created_by=actor)
        self.record_event(
            task_id,
            actor,
            "snippet",
            subject=node,
            file=file_path,
            start=start,
            end=end,
        )
        return node

    # ---- labels / PRF ----------------------------------------------------

    def _merge_node_labels(self, node: str, labels: dict[str, Any]) -> None:
        if node in self.g:
            existing = self.g.nodes[node].setdefault("labels", {})
            if isinstance(existing, dict):
                existing.update(labels)
        if not self.is_persistent:
            return
        sql = """
        UPDATE agentcore_graph_nodes
        SET labels = labels || %s::jsonb, updated_at = now()
        WHERE id = %s
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (json.dumps(labels), node))

    def tag_relevance(
        self,
        task_id: str,
        label: str,
        *,
        score: float = 1.0,
        reason: str = "",
        created_by: str | None = None,
    ) -> int:
        """Append a PRF label to every snippet a task produced."""
        from agentcore.memory.prf import now as _now

        t = f"task:{task_id}"
        actor = created_by or "unknown"
        record = {"score": float(score), "reason": reason, "at": _now(), "created_by": actor}
        label_patch = {label: record}
        tagged = 0

        if t in self.g:
            for n in list(self.g.neighbors(t)):
                attrs = self.g.nodes[n]
                if attrs.get("kind") != "snippet":
                    continue
                self._merge_node_labels(n, label_patch)
                tagged += 1

        if not self.is_persistent:
            return tagged

        sql = """
        SELECT n.id
        FROM agentcore_graph_edges e
        JOIN agentcore_graph_nodes n
          ON n.id = CASE WHEN e.source = %s THEN e.target ELSE e.source END
        WHERE (e.source = %s OR e.target = %s)
          AND e.relation = 'produced'
          AND n.kind = 'snippet'
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, (t, t, t))
            rows = cur.fetchall()
        for row in rows:
            self._merge_node_labels(str(row[0]), label_patch)
        count = len(rows)
        self.record_event(
            task_id,
            actor,
            "tag_relevance",
            subject=label,
            score=float(score),
            reason=reason,
            tagged=count,
        )
        return count

    def tag_task(
        self,
        task_id: str,
        label: str,
        *,
        score: float = 1.0,
        reason: str = "",
        created_by: str | None = None,
    ) -> None:
        """Tag a task node directly (independent of its snippets)."""
        from agentcore.memory.prf import now as _now

        t = f"task:{task_id}"
        actor = created_by or "unknown"
        if t not in self.g and not self.is_persistent:
            return
        self._merge_node_labels(
            t,
            {label: {"score": float(score), "reason": reason, "at": _now(), "created_by": actor}},
        )
        self.record_event(task_id, actor, "tag_task", subject=label, score=float(score), reason=reason)

    # ---- retrieval-shaped reads ----------------------------------------

    def refresh_active_weights(self) -> None:
        """Apply simple time decay so stale edges do not dominate memory."""
        if not self.is_persistent:
            return
        sql = """
        UPDATE agentcore_graph_edges
        SET active_weight = weight * exp(
          -GREATEST(EXTRACT(EPOCH FROM (now() - last_seen)) / 86400.0, 0) / 30.0
        )
        """
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql)

    def _operational_memory_db(self, file_paths: list[str], *, k: int) -> dict[str, Any]:
        file_nodes = [f"file:{fp}" for fp in file_paths]
        if not file_nodes:
            return {"tasks": [], "label_counts": {}, "neighbors": []}

        self.refresh_active_weights()
        placeholders = ", ".join(["%s"] * len(file_nodes))
        task_sql = f"""
        SELECT DISTINCT t.id, t.labels, e.last_seen
        FROM agentcore_graph_edges e
        JOIN agentcore_graph_nodes t
          ON t.id = CASE
            WHEN e.source IN ({placeholders}) THEN e.target
            ELSE e.source
          END
        WHERE (e.source IN ({placeholders}) OR e.target IN ({placeholders}))
          AND e.relation = 'changed'
          AND t.kind = 'task'
        ORDER BY e.last_seen DESC
        LIMIT %s
        """
        params: list[Any] = [*file_nodes, *file_nodes, *file_nodes, k]
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(task_sql, params)
            task_rows = cur.fetchall()

        task_ids = [str(row[0]) for row in task_rows]
        label_counts: dict[str, int] = {}
        tasks_summary = []
        for tid, labels, _last_seen in task_rows:
            label_obj = labels if isinstance(labels, dict) else {}
            tasks_summary.append({"id": str(tid), "labels": list(label_obj.keys())})
            for label in label_obj:
                label_counts[str(label)] = label_counts.get(str(label), 0) + 1

        neighbour_files: set[str] = set()
        if task_ids:
            task_placeholders = ", ".join(["%s"] * len(task_ids))
            file_sql = f"""
            SELECT DISTINCT f.id
            FROM agentcore_graph_edges e
            JOIN agentcore_graph_nodes f
              ON f.id = CASE
                WHEN e.source IN ({task_placeholders}) THEN e.target
                ELSE e.source
              END
            WHERE (e.source IN ({task_placeholders}) OR e.target IN ({task_placeholders}))
              AND e.relation = 'changed'
              AND f.kind = 'file'
            LIMIT 50
            """
            file_params: list[Any] = [*task_ids, *task_ids, *task_ids]
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(file_sql, file_params)
                file_rows = cur.fetchall()
            for row in file_rows:
                node_id = str(row[0])
                rel = node_id[len("file:"):] if node_id.startswith("file:") else node_id
                if rel not in file_paths:
                    neighbour_files.add(rel)

        return {
            "tasks": tasks_summary,
            "label_counts": label_counts,
            "neighbors": sorted(neighbour_files)[:10],
        }

    def operational_memory(
        self, file_paths: list[str], *, k: int = 5
    ) -> dict[str, Any]:
        """Recent task history relevant to a set of file paths."""
        if not file_paths:
            return {"tasks": [], "label_counts": {}, "neighbors": []}
        if self.is_persistent:
            return self._operational_memory_db(file_paths, k=k)

        task_hits: list[tuple[str, dict[str, Any]]] = []
        seen_tasks: set[str] = set()
        for fp in file_paths:
            f = f"file:{fp}"
            if f not in self.g:
                continue
            for n in self.g.neighbors(f):
                attrs = self.g.nodes[n]
                if attrs.get("kind") == "task" and n not in seen_tasks:
                    seen_tasks.add(n)
                    task_hits.append((n, attrs))

        def _last_seen(attrs: dict[str, Any]) -> str:
            labels = attrs.get("labels", {}) or {}
            return max((rec.get("at", "") for rec in labels.values()), default="")

        task_hits.sort(key=lambda x: _last_seen(x[1]), reverse=True)
        top = task_hits[:k]

        label_counts: dict[str, int] = {}
        tasks_summary = []
        for tid, attrs in top:
            tasks_summary.append({
                "id": tid,
                "labels": list((attrs.get("labels") or {}).keys()),
            })
            for label in attrs.get("labels") or {}:
                label_counts[label] = label_counts.get(label, 0) + 1

        neighbour_files: set[str] = set()
        for tid, _ in top:
            for n in self.g.neighbors(tid):
                if n.startswith("file:") and n[len("file:"):] not in file_paths:
                    neighbour_files.add(n[len("file:"):])

        return {
            "tasks": tasks_summary,
            "label_counts": label_counts,
            "neighbors": sorted(neighbour_files)[:10],
        }

    def snippets_for(self, task_id: str) -> list[dict[str, Any]]:
        t = f"task:{task_id}"
        if self.is_persistent:
            sql = """
            SELECT n.id, n.attrs, n.labels
            FROM agentcore_graph_edges e
            JOIN agentcore_graph_nodes n
              ON n.id = CASE WHEN e.source = %s THEN e.target ELSE e.source END
            WHERE (e.source = %s OR e.target = %s)
              AND e.relation = 'produced'
              AND n.kind = 'snippet'
            """
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(sql, (t, t, t))
                rows = cur.fetchall()
            out = []
            for node_id, attrs, labels in rows:
                attr_obj = attrs if isinstance(attrs, dict) else {}
                label_obj = labels if isinstance(labels, dict) else {}
                out.append({"id": str(node_id), **attr_obj, "labels": label_obj})
            return out

        if t not in self.g:
            return []
        out: list[dict[str, Any]] = []
        for n in self.g.neighbors(t):
            attrs = self.g.nodes[n]
            if attrs.get("kind") == "snippet":
                out.append({"id": n, **attrs})
        return out

    def merge_subgraph(self, other: nx.Graph, *, namespace: str = "symbol") -> int:
        """Compose another NetworkX graph into ours, namespacing its nodes."""
        if other is None or other.number_of_nodes() == 0:
            return 0
        added = 0
        for node, attrs in other.nodes(data=True):
            ns_node = node if str(node).startswith(f"{namespace}:") else f"{namespace}:{node}"
            if ns_node not in self.g:
                added += 1
            self.add_node(ns_node, **{**attrs, "kind": namespace})
        for u, v, attrs in other.edges(data=True):
            ns_u = u if str(u).startswith(f"{namespace}:") else f"{namespace}:{u}"
            ns_v = v if str(v).startswith(f"{namespace}:") else f"{namespace}:{v}"
            self.add_edge(ns_u, ns_v, **attrs)
        return added

    # ---- community detection -------------------------------------------

    def detect_communities(self, resolution: float = 1.0) -> dict[str, int]:
        if self.g.number_of_nodes() == 0:
            self._communities = {}
            return {}
        try:
            import community as community_louvain  # python-louvain
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "python-louvain is required for community detection; "
                "install with `pip install python-louvain`"
            ) from exc
        self._communities = community_louvain.best_partition(
            self.g, weight="weight", resolution=resolution
        )
        for node, cid in self._communities.items():
            self.g.nodes[node]["community"] = cid
        self._persist_communities()
        return self._communities

    def community_summaries(self) -> list[CommunitySummary]:
        if not self._communities:
            self.detect_communities()
        buckets: dict[int, list[str]] = {}
        for node, cid in self._communities.items():
            buckets.setdefault(cid, []).append(node)
        return [
            CommunitySummary(id=cid, size=len(members), members=sorted(members)[:50])
            for cid, members in sorted(buckets.items())
        ]

    def _neighbors_db(self, node: str, hops: int) -> list[str]:
        if hops < 1:
            return []
        seen: set[str] = {node}
        frontier: set[str] = {node}
        with self._conn() as conn, conn.cursor() as cur:
            for _ in range(hops):
                if not frontier:
                    break
                placeholders = ", ".join(["%s"] * len(frontier))
                sql = f"""
                SELECT source, target
                FROM agentcore_graph_edges
                WHERE source IN ({placeholders}) OR target IN ({placeholders})
                """
                params = [*frontier, *frontier]
                cur.execute(sql, params)
                nxt: set[str] = set()
                for source, target in cur.fetchall():
                    source_s, target_s = str(source), str(target)
                    if source_s in frontier:
                        nxt.add(target_s)
                    if target_s in frontier:
                        nxt.add(source_s)
                nxt -= seen
                seen.update(nxt)
                frontier = nxt
        return sorted(seen - {node})

    def neighbors(self, node: str, hops: int = 1) -> list[str]:
        if self.is_persistent:
            return self._neighbors_db(node, hops)
        if node not in self.g:
            return []
        seen: set[str] = {node}
        frontier = {node}
        for _ in range(hops):
            nxt: set[str] = set()
            for n in frontier:
                nxt.update(self.g.neighbors(n))
            nxt -= seen
            seen.update(nxt)
            frontier = nxt
            if not frontier:
                break
        return sorted(seen - {node})

    # ---- persistence ----------------------------------------------------

    def _persist_communities(self) -> None:
        if not self.is_persistent or not self._communities:
            return

        buckets: dict[int, list[str]] = {}
        for node, cid in self._communities.items():
            buckets.setdefault(cid, []).append(node)

        def _community_type(mean_confidence: float) -> str:
            if mean_confidence > 0.6:
                return "active"
            if mean_confidence > 0.3:
                return "emerging"
            return "historical"

        rows: list[tuple[str, int, int, float, str]] = []
        for cid, members in buckets.items():
            member_set = set(members)
            weights = [
                float(data.get("weight", 1.0))
                for u, v, data in self.g.edges(data=True)
                if u in member_set and v in member_set
            ]
            mean_confidence = sum(weights) / len(weights) if weights else 0.0
            ctype = _community_type(mean_confidence)
            for node in members:
                rows.append((node, cid, len(members), round(mean_confidence, 3), ctype))

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM agentcore_graph_communities")
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO agentcore_graph_communities
                      (node_id, community_id, community_size, mean_confidence, community_type)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    row,
                )

    def save(self) -> None:
        if self.is_persistent:
            # Mutations write through to Postgres immediately. Replaying the
            # NetworkX mirror here would double-count edge evidence/weights.
            self._persist_communities()
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "nodes": [
                {"id": n, "attrs": dict(d)} for n, d in self.g.nodes(data=True)
            ],
            "edges": [
                {"u": u, "v": v, "attrs": dict(d)} for u, v, d in self.g.edges(data=True)
            ],
            "communities": self._communities,
        }
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def load(self) -> bool:
        if self.is_persistent:
            self.init_schema()
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT id, attrs, labels, kind FROM agentcore_graph_nodes")
                nodes = cur.fetchall()
                cur.execute("SELECT source, target, relation, weight, attrs FROM agentcore_graph_edges")
                edges = cur.fetchall()
                cur.execute("SELECT node_id, community_id FROM agentcore_graph_communities")
                communities = cur.fetchall()

            self.g = nx.Graph()
            for node_id, attrs, labels, kind in nodes:
                attr_obj = attrs if isinstance(attrs, dict) else {}
                label_obj = labels if isinstance(labels, dict) else {}
                self.g.add_node(
                    str(node_id),
                    **{**attr_obj, "labels": label_obj, "kind": str(kind)},
                )
            for source, target, relation, weight, attrs in edges:
                attr_obj = attrs if isinstance(attrs, dict) else {}
                self.g.add_edge(
                    str(source),
                    str(target),
                    **{**attr_obj, "relation": str(relation), "weight": float(weight)},
                )
            self._communities = {str(node): int(cid) for node, cid in communities}
            return bool(nodes or edges)

        if not self.path.exists():
            return False
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.g = nx.Graph()
        for n in data.get("nodes", []):
            self.g.add_node(n["id"], **n.get("attrs", {}))
        for e in data.get("edges", []):
            self.g.add_edge(e["u"], e["v"], **e.get("attrs", {}))
        self._communities = {k: int(v) for k, v in data.get("communities", {}).items()}
        return True
