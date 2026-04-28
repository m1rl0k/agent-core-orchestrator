"""Enrichment hook: post-hop, the runtime should write task→file→symbol
edges into the operational graph and absorb graphify subgraphs."""

from __future__ import annotations

import networkx as nx
import pytest

from agentcore.adapters.graphify import GraphifyAdapter, SymbolImpact
from agentcore.contracts.envelopes import Handoff
from agentcore.llm.router import ChatMessage, LLMResponse, LLMRouter
from agentcore.memory.graph import KnowledgeGraph
from agentcore.orchestrator.runtime import Runtime
from agentcore.orchestrator.traces import TraceLog
from agentcore.spec.loader import AgentRegistry
from agentcore.spec.parser import parse_agent_text


ARCHITECT = """\
---
name: architect
description: Plans things.
soul: { role: architect }
contract:
  inputs:
    - { name: brief, type: string, required: true }
  outputs:
    - { name: summary,         type: string, required: true }
    - { name: files_to_change, type: list,   required: true }
  accepts_handoff_from: [user]
  delegates_to: [developer]
---
You are the Architect.
"""


class FakeRouter(LLMRouter):
    def __init__(self) -> None:
        pass

    async def complete(self, messages, model_config):  # type: ignore[override]
        body = (
            '{"summary":"add /metrics","files_to_change":'
            '[{"path":"src/app/metrics.py","action":"create","rationale":"new endpoint"},'
            ' {"path":"src/app/__init__.py","action":"modify","rationale":"register router"}]}'
        )
        return LLMResponse(text=body, provider="fake", model="fake", raw=None)


class FakeGraphify(GraphifyAdapter):
    """In-memory stand-in: pretends to know two files, each with one symbol."""

    def __init__(self) -> None:
        # Skip the real __init__; fabricate a ready capability.
        from agentcore.capabilities import Capability

        self.capability = Capability(
            name="graphify",
            enabled=True,
            installed=True,
            authenticated=True,
            cli="fake",
            install_hint="",
            auth_hint="",
        )
        self.repo_root = None  # type: ignore[assignment]
        self._mod = object()
        self._engine = object()

    def impact(self, symbol_or_file: str) -> SymbolImpact | None:  # type: ignore[override]
        return SymbolImpact(
            symbol=symbol_or_file,
            file=symbol_or_file,
            downstream=[f"{symbol_or_file}::caller_a", f"{symbol_or_file}::caller_b"],
            confidence=0.9,
        )

    def subgraph_for(self, refs):  # type: ignore[override]
        g = nx.Graph()
        ref_list = list(refs)
        if not ref_list:
            return None
        seed = ref_list[0]
        for r in ref_list[1:]:
            g.add_edge(seed, r, relation="calls")
        return g


@pytest.mark.asyncio
async def test_enrichment_writes_handoff_changes_and_impact() -> None:
    registry = AgentRegistry()
    registry.upsert(parse_agent_text(ARCHITECT, source="architect.agent.md"))

    graph = KnowledgeGraph(snapshot_path="/tmp/agentcore-test-graph.json")  # noqa: S108
    runtime = Runtime(
        registry=registry,
        router=FakeRouter(),
        traces=TraceLog(),
        graph=graph,
        graphify=FakeGraphify(),
    )

    handoff = Handoff(from_agent="user", to_agent="architect", payload={"brief": "x"})
    outcome, _next = await runtime.execute(handoff)

    assert outcome.status in {"ok", "delegated"}
    # handoff edge: user -> architect
    assert graph.g.has_edge("agent:user", "agent:architect")
    # changed-file edges from the architect's plan
    assert graph.g.has_edge(f"task:{handoff.task_id}", "file:src/app/metrics.py")
    assert graph.g.has_edge(f"task:{handoff.task_id}", "file:src/app/__init__.py")
    # blast-radius from graphify.impact()
    assert graph.g.has_edge(
        f"task:{handoff.task_id}", "symbol:src/app/metrics.py::caller_a"
    )
    # subgraph merged in (calls edge between symbol nodes)
    has_calls_edge = any(
        d.get("relation") == "calls" for _, _, d in graph.g.edges(data=True)
    )
    assert has_calls_edge


@pytest.mark.asyncio
async def test_enrichment_is_noop_without_graph() -> None:
    registry = AgentRegistry()
    registry.upsert(parse_agent_text(ARCHITECT, source="architect.agent.md"))
    runtime = Runtime(registry=registry, router=FakeRouter(), traces=TraceLog())
    handoff = Handoff(from_agent="user", to_agent="architect", payload={"brief": "x"})
    outcome, _ = await runtime.execute(handoff)
    assert outcome.status in {"ok", "delegated"}
