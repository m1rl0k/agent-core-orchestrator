"""Knowledge graph behavior for coding-agent operational memory."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import networkx as nx

import agentcore.memory.graph as graph_module
from agentcore.memory import prf
from agentcore.memory.graph import GRAPH_DDL, KnowledgeGraph


def test_graph_ddl_has_prod_indexes_and_uniqueness() -> None:
    assert "agentcore_graph_nodes" in GRAPH_DDL
    assert "agentcore_graph_edges" in GRAPH_DDL
    assert "agentcore_graph_communities" in GRAPH_DDL
    assert "project_id TEXT NOT NULL DEFAULT 'default'" in GRAPH_DDL
    assert "idx_agentcore_graph_events_project" in GRAPH_DDL
    assert "UNIQUE(project_id, source, target, relation)" in GRAPH_DDL
    assert "idx_agentcore_graph_edges_source" in GRAPH_DDL
    assert "idx_agentcore_graph_edges_target" in GRAPH_DDL
    assert "idx_agentcore_graph_edges_active_weight" in GRAPH_DDL
    assert "USING GIN(attrs)" in GRAPH_DDL
    assert "USING GIN(labels)" in GRAPH_DDL


def test_record_handoff_accumulates_edge_evidence() -> None:
    graph = KnowledgeGraph()

    graph.record_handoff("T1", "architect", "developer")
    graph.record_handoff("T1", "architect", "developer")

    assert graph.g.has_edge("agent:architect", "agent:developer")
    edge = graph.g["agent:architect"]["agent:developer"]
    assert edge["relation"] == "handoff"
    assert edge["weight"] == 2.0
    assert edge["evidence_count"] == 2


def test_record_impact_and_neighbors_for_blast_radius() -> None:
    graph = KnowledgeGraph()

    graph.record_impact("T1", "src/auth.py", ["Auth.refresh", "TokenStore.put"])

    assert graph.g.has_edge("task:T1", "file:src/auth.py")
    assert graph.g.has_edge("file:src/auth.py", "symbol:Auth.refresh")
    assert graph.g.has_edge("task:T1", "symbol:TokenStore.put")
    assert "symbol:Auth.refresh" in graph.neighbors("file:src/auth.py", hops=1)
    assert "symbol:TokenStore.put" in graph.neighbors("file:src/auth.py", hops=2)


def test_snippet_relevance_and_task_labels() -> None:
    graph = KnowledgeGraph()
    graph.record_snippet(
        "T1",
        "src/auth.py",
        start=10,
        end=20,
        content="def refresh(): ...",
        intent="fix auth refresh",
        role="developer",
    )

    tagged = graph.tag_relevance("T1", prf.QA_PASSED, reason="tests passed")
    graph.tag_task("T1", prf.POSITIVE)

    assert tagged == 1
    snippets = graph.snippets_for("T1")
    assert len(snippets) == 1
    assert snippets[0]["labels"][prf.QA_PASSED]["reason"] == "tests passed"
    assert graph.g.nodes["task:T1"]["labels"][prf.POSITIVE]["score"] == 1.0


def test_operational_memory_returns_task_labels_and_cochanged_files() -> None:
    graph = KnowledgeGraph()

    graph.record_change("T1", "src/auth.py")
    graph.record_change("T1", "src/token.py")
    graph.tag_task("T1", prf.POSITIVE)

    memory = graph.operational_memory(["src/auth.py"])

    assert memory["tasks"] == [{"id": "task:T1", "labels": [prf.POSITIVE]}]
    assert memory["label_counts"] == {prf.POSITIVE: 1}
    assert memory["neighbors"] == ["src/token.py"]


def test_merge_subgraph_namespaces_graphify_symbols() -> None:
    graph = KnowledgeGraph()
    subgraph = nx.Graph()
    subgraph.add_node("Auth.refresh", language="python")
    subgraph.add_node("TokenStore.put", language="python")
    subgraph.add_edge("Auth.refresh", "TokenStore.put", relation="calls")

    added = graph.merge_subgraph(subgraph)

    assert added == 2
    assert graph.g.has_node("symbol:Auth.refresh")
    assert graph.g.has_node("symbol:TokenStore.put")
    assert graph.g.has_edge("symbol:Auth.refresh", "symbol:TokenStore.put")
    assert graph.g["symbol:Auth.refresh"]["symbol:TokenStore.put"]["relation"] == "calls"


def test_persist_communities_is_atomic(monkeypatch) -> None:
    events: list[str] = []

    class _Settings:
        project_name = "tenant-a"

    class _Cursor:
        def __enter__(self):
            events.append("cursor_enter")
            return self

        def __exit__(self, *_exc: object) -> None:
            events.append("cursor_exit")

        def execute(self, sql: str, _params: tuple[Any, ...] | None = None) -> None:
            if sql.strip().startswith("DELETE"):
                events.append("delete")
            elif sql.strip().startswith("INSERT"):
                events.append("insert")

    class _Transaction:
        def __enter__(self):
            events.append("tx_enter")
            return self

        def __exit__(self, *_exc: object) -> None:
            events.append("tx_exit")

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def transaction(self) -> _Transaction:
            return _Transaction()

        def cursor(self) -> _Cursor:
            return _Cursor()

    @contextmanager
    def fake_pg_conn(_settings):
        yield _Connection()

    monkeypatch.setattr(graph_module, "pg_conn", fake_pg_conn)

    graph = KnowledgeGraph(settings=_Settings())  # type: ignore[arg-type]
    graph.g.add_edge("agent:architect", "agent:developer", weight=1.0)
    graph._communities = {"agent:architect": 1, "agent:developer": 1}

    graph._persist_communities()

    assert events == [
        "tx_enter",
        "cursor_enter",
        "delete",
        "insert",
        "insert",
        "cursor_exit",
        "tx_exit",
    ]
