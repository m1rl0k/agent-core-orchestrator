"""FastAPI surface for the orchestrator.

Endpoints:
  GET  /healthz            — liveness
  GET  /agents             — registered specs (+ parse errors)
  GET  /capabilities       — host capability matrix
  POST /run                — start a task: {to_agent, payload, from_agent?}
  POST /handoff            — drive one explicit hop
  POST /signal             — accept an external Signal (e.g. webhook)
  GET  /tasks/{id}/trace   — full trace for a task
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agentcore.adapters.graphify import GraphifyAdapter
from agentcore.capabilities import detect_capabilities
from agentcore.contracts.envelopes import Handoff, new_task_id
from agentcore.host import detect_host
from agentcore.llm.router import LLMRouter
from agentcore.logging_setup import configure_logging
from agentcore.memory.graph import KnowledgeGraph
from agentcore.orchestrator.runtime import Handoff as _H  # noqa: F401 - keep import shape
from agentcore.orchestrator.runtime import HandoffRejected, Runtime
from agentcore.orchestrator.traces import TraceLog
from agentcore.settings import get_settings
from agentcore.spec.loader import AgentRegistry, watch_agents_dir


# ---------------------------------------------------------------------------
# Wire payloads
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    to_agent: str
    payload: dict[str, Any]
    from_agent: str = "user"
    chain: bool = True
    max_hops: int = 6


class RunResponse(BaseModel):
    task_id: str
    hops: list[dict[str, Any]]


class SignalIn(BaseModel):
    source: str
    kind: str
    target: str
    severity: str = "info"
    payload: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def build_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    registry = AgentRegistry()
    traces = TraceLog()
    router = LLMRouter(settings)
    graph = KnowledgeGraph()
    graph.load()  # restore prior snapshot if present
    graphify = (
        GraphifyAdapter(repo_root=settings.graphify_repo_root, enabled=True)
        if settings.enable_graphify
        else None
    )
    runtime = Runtime(
        registry=registry, router=router, traces=traces, graph=graph, graphify=graphify
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ARG001
        stop = asyncio.Event()
        watcher = asyncio.create_task(
            watch_agents_dir(settings.agents_dir, registry, stop_event=stop)
        )
        try:
            yield
        finally:
            stop.set()
            watcher.cancel()

    app = FastAPI(title="agent-core-orchestrator", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        host = detect_host()
        return {
            "ok": True,
            "agents_loaded": len(registry.all()),
            "agent_errors": len(registry.errors()),
            "host": {"os": host.os, "shell": host.shell, "arch": host.arch},
        }

    @app.get("/agents")
    async def list_agents() -> dict[str, Any]:
        return {
            "agents": [
                {
                    "name": s.name,
                    "description": s.description,
                    "provider": s.llm.provider,
                    "model": s.llm.model,
                    "accepts_from": s.contract.accepts_handoff_from,
                    "delegates_to": s.contract.delegates_to,
                    "source_path": s.source_path,
                    "checksum": s.checksum,
                }
                for s in registry.all()
            ],
            "errors": registry.errors(),
        }

    @app.get("/capabilities")
    async def capabilities() -> dict[str, Any]:
        return {name: cap.__dict__ for name, cap in detect_capabilities(settings).items()}

    @app.post("/run", response_model=RunResponse)
    async def run(req: RunRequest) -> RunResponse:
        handoff = Handoff(
            task_id=new_task_id(),
            from_agent=req.from_agent,
            to_agent=req.to_agent,
            payload=req.payload,
        )
        hops: list[dict[str, Any]] = []
        current: Handoff | None = handoff
        for _ in range(req.max_hops):
            if current is None:
                break
            try:
                outcome, nxt = await runtime.execute(current)
            except HandoffRejected as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            hops.append({
                "agent": outcome.agent,
                "status": outcome.status,
                "output": outcome.output,
                "delegate_to": outcome.delegate_to,
            })
            if not (req.chain and nxt):
                break
            current = nxt
        return RunResponse(task_id=handoff.task_id, hops=hops)

    @app.post("/handoff", response_model=RunResponse)
    async def handoff_one(handoff: Handoff) -> RunResponse:
        try:
            outcome, _ = await runtime.execute(handoff)
        except HandoffRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RunResponse(
            task_id=handoff.task_id,
            hops=[{"agent": outcome.agent, "status": outcome.status, "output": outcome.output}],
        )

    @app.post("/signal")
    async def receive_signal(sig: SignalIn) -> dict[str, Any]:
        # Signals route to Ops by convention; Ops decides whether to escalate.
        ops = registry.get("ops")
        if ops is None:
            raise HTTPException(status_code=503, detail="ops agent not loaded")
        handoff = Handoff(
            from_agent="user",
            to_agent="ops",
            payload={"signal": sig.model_dump()},
        )
        try:
            outcome, _ = await runtime.execute(handoff)
        except HandoffRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"task_id": handoff.task_id, "outcome": outcome.model_dump()}

    @app.get("/tasks/{task_id}/trace")
    async def trace(task_id: str) -> dict[str, Any]:
        events = traces.for_task(task_id)
        return {
            "task_id": task_id,
            "events": [
                {
                    "step": e.step,
                    "kind": e.kind,
                    "actor": e.actor,
                    "at": e.at.isoformat(),
                    "detail": e.detail,
                }
                for e in events
            ],
        }

    return app


app = build_app()
