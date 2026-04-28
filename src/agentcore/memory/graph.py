"""NetworkX-backed knowledge graph with Louvain community detection.

Primarily a **team & task history** graph, enriched with code-symbol slices
returned by graphify after each agent hop:

  agent  ─[handoff]─▶ agent
  agent  ─[worked_on]─▶ task
  task   ─[changed]──▶ file
  file   ─[contains]─▶ symbol      (from graphify)
  symbol ─[calls]────▶ symbol      (from graphify, via merge_subgraph)
  task   ─[blast_radius]─▶ symbol  (from graphify.impact, via record_impact)
  task   ─[outcome]──▶ status

Louvain on the merged graph reveals "task families" — clusters of work, the
agents that collaborate on them, and the symbol neighbourhoods they touch.

Persistence: snapshot to a single JSON file (default `.agentcore/graph.json`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import community as community_louvain  # python-louvain
import networkx as nx


@dataclass(slots=True)
class CommunitySummary:
    id: int
    size: int
    members: list[str]
    label: str = ""


class KnowledgeGraph:
    def __init__(self, snapshot_path: Path | str = ".agentcore/graph.json") -> None:
        self.path = Path(snapshot_path)
        self.g: nx.Graph = nx.Graph()
        self._communities: dict[str, int] = {}

    # ---- generic mutation ----------------------------------------------

    def add_node(self, node: str, **attrs: Any) -> None:
        self.g.add_node(node, **attrs)

    def add_edge(self, u: str, v: str, weight: float = 1.0, **attrs: Any) -> None:
        if self.g.has_edge(u, v):
            self.g[u][v]["weight"] = self.g[u][v].get("weight", 1.0) + weight
        else:
            self.g.add_edge(u, v, weight=weight, **attrs)

    # ---- operational events --------------------------------------------
    # Stable node prefixes:
    #   agent:<name>     ·  task:<id>     ·  file:<rel_path>     ·  status:<name>

    def record_handoff(self, task_id: str, from_agent: str, to_agent: str) -> None:
        a, b = f"agent:{from_agent}", f"agent:{to_agent}"
        t = f"task:{task_id}"
        self.add_node(a, kind="agent")
        self.add_node(b, kind="agent")
        self.add_node(t, kind="task")
        self.add_edge(a, b, weight=1.0, relation="handoff")
        self.add_edge(a, t, weight=1.0, relation="worked_on")
        self.add_edge(b, t, weight=1.0, relation="worked_on")

    def record_change(self, task_id: str, file_path: str) -> None:
        t = f"task:{task_id}"
        f = f"file:{file_path}"
        self.add_node(t, kind="task")
        self.add_node(f, kind="file")
        self.add_edge(t, f, weight=1.0, relation="changed")

    def record_outcome(self, task_id: str, status: str) -> None:
        t = f"task:{task_id}"
        s = f"status:{status}"
        self.add_node(t, kind="task")
        self.add_node(s, kind="status")
        self.add_edge(t, s, weight=1.0, relation="outcome")

    def record_impact(self, task_id: str, file_path: str, downstream: list[str]) -> None:
        """Wire a task to a file and that file's downstream symbols.

        Used by the enrichment hook after graphify.impact() returns. The
        downstream symbols become first-class nodes so future Louvain runs
        can discover task-family ↔ symbol-cluster correspondence.
        """
        t = f"task:{task_id}"
        f = f"file:{file_path}"
        self.add_node(t, kind="task")
        self.add_node(f, kind="file")
        self.add_edge(t, f, weight=1.0, relation="changed")
        for sym in downstream:
            s = f"symbol:{sym}"
            self.add_node(s, kind="symbol")
            self.add_edge(f, s, weight=0.5, relation="contains")
            self.add_edge(t, s, weight=0.5, relation="blast_radius")

    def merge_subgraph(self, other: "nx.Graph", *, namespace: str = "symbol") -> int:
        """Compose another NetworkX graph into ours, namespacing its nodes.

        Returns the number of nodes added. Used by the enrichment hook to
        absorb graphify's symbol-graph slices (`symbol:OAuth.refresh ─calls─▶
        symbol:TokenStore.put`, etc.) into the operational graph.
        """
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
        self._communities = community_louvain.best_partition(
            self.g, weight="weight", resolution=resolution
        )
        for node, cid in self._communities.items():
            self.g.nodes[node]["community"] = cid
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

    def neighbors(self, node: str, hops: int = 1) -> list[str]:
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

    def save(self) -> None:
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
