"""NetworkX-backed knowledge graph with Louvain community detection.

This is primarily a **team & task history** graph, not a code-symbol graph.
Code traversal is offloaded to MCP servers (gitnexus, code-graph, etc.) —
this graph captures the operational layer:

  agent ─[handoff]─▶ agent
  agent ─[worked_on]─▶ task
  task  ─[changed]──▶ file
  task  ─[outcome]──▶ status

Louvain communities reveal "task families" — clusters of work, the agents
that collaborate on them, and the parts of the codebase they touch. Agents
reference these via `KnowledgeBinding.graph_communities`.

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
