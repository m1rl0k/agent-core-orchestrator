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
import hmac
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from agentcore.adapters.graphify import GraphifyAdapter
from agentcore.capabilities import detect_capabilities
from agentcore.contracts.envelopes import Handoff, new_task_id
from agentcore.host import detect_host
from agentcore.llm.router import LLMRouter
from agentcore.logging_setup import configure_logging
from agentcore.memory.graph import KnowledgeGraph
from agentcore.orchestrator.runtime import Handoff as _H  # noqa: F401 - keep import shape
from agentcore.orchestrator.runtime import HandoffRejected, Runtime, SLAExceeded
from agentcore.orchestrator.traces import TraceLog
from agentcore.settings import get_settings
from agentcore.spec.loader import AgentRegistry, watch_agents_dir

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Wire payloads
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    to_agent: str
    payload: dict[str, Any]
    from_agent: str = "user"
    chain: bool = True
    max_hops: int = 6


class HandoffRequest(BaseModel):
    from_agent: str = "user"
    to_agent: str
    payload: dict[str, Any]


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
    if settings.host not in {"127.0.0.1", "localhost"} and not settings.api_token:
        raise RuntimeError(
            "AGENTCORE_API_TOKEN is required when AGENTCORE_HOST is not localhost"
        )

    registry = AgentRegistry()
    traces = TraceLog()
    router = LLMRouter(settings)
    from agentcore.state.idempotency import IdempotencyStore
    from agentcore.state.jobs import JobQueue

    idem_cache = IdempotencyStore(settings=settings)
    job_queue = JobQueue(settings=settings)
    job_queue.init_schema()  # best-effort; falls back to in-memory mode if no DB
    job_handlers: dict[str, Any] = {}
    import contextlib

    graph = KnowledgeGraph(settings=settings)
    try:
        graph.load()  # initialize/load durable Postgres graph memory
    except Exception:
        # Postgres unreachable / not provisioned — degrade to in-memory mode so
        # the orchestrator still serves. The retriever follows the same pattern.
        graph = KnowledgeGraph()  # no settings → memory-only
        with contextlib.suppress(Exception):
            graph.load()
    graphify = (
        GraphifyAdapter(repo_root=settings.graphify_repo_root, enabled=True)
        if settings.enable_graphify
        else None
    )
    from agentcore.retrieval.factory import try_build_retriever

    retriever = try_build_retriever(settings, graph)
    runtime = Runtime(
        registry=registry, router=router, traces=traces,
        graph=graph, graphify=graphify, retriever=retriever,
    )

    async def require_api_token(authorization: str | None = Header(default=None)) -> None:
        if not settings.api_token:
            return
        expected = f"Bearer {settings.api_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing or invalid bearer token",
            )

    def save_graph_best_effort() -> None:
        try:
            graph.save()
        except Exception:
            return

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from agentcore.state.jobs import run_worker as _run_worker

        stop = asyncio.Event()
        watcher = asyncio.create_task(
            watch_agents_dir(settings.agents_dir, registry, stop_event=stop)
        )
        # Drain the durable Postgres job queue if any handlers are registered
        # (currently: wiki refresh). Falls back to in-memory mode when the DB
        # isn't reachable; either way the queue is bounded.
        worker = None
        if job_handlers:
            worker = asyncio.create_task(
                _run_worker(job_queue, job_handlers, stop_event=stop)
            )
        try:
            yield
        finally:
            save_graph_best_effort()
            stop.set()
            watcher.cancel()
            if worker is not None:
                worker.cancel()

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

    async def _drive_chain(
        first: Handoff, max_hops: int, chain: bool
    ) -> list[dict[str, Any]]:
        """Run the handoff chain. Wrapped by /run with `max_chain_seconds`."""
        hops: list[dict[str, Any]] = []
        current: Handoff | None = first
        for _ in range(max_hops):
            if current is None:
                break
            outcome, nxt = await runtime.execute(current)
            hops.append({
                "agent": outcome.agent,
                "status": outcome.status,
                "output": outcome.output,
                "delegate_to": outcome.delegate_to,
            })
            if not (chain and nxt):
                break
            current = nxt
        return hops

    @app.post("/run", response_model=RunResponse, dependencies=[Depends(require_api_token)])
    async def run(
        req: RunRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> RunResponse:
        if idempotency_key:
            cached = idem_cache.get("run", idempotency_key)
            if cached is not None:
                return RunResponse(**cached)
        handoff = Handoff(
            task_id=new_task_id(),
            from_agent=req.from_agent,
            to_agent=req.to_agent,
            payload=req.payload,
        )
        chain_cap = settings.max_chain_seconds
        try:
            if chain_cap and chain_cap > 0:
                hops = await asyncio.wait_for(
                    _drive_chain(handoff, req.max_hops, req.chain),
                    timeout=float(chain_cap),
                )
            else:
                hops = await _drive_chain(handoff, req.max_hops, req.chain)
        except HandoffRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SLAExceeded as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail=f"chain exceeded max_chain_seconds={chain_cap}",
            ) from exc
        save_graph_best_effort()
        resp = RunResponse(task_id=handoff.task_id, hops=hops)
        if idempotency_key:
            idem_cache.put("run", idempotency_key, resp.model_dump())
        return resp

    @app.post("/handoff", response_model=RunResponse, dependencies=[Depends(require_api_token)])
    async def handoff_one(
        req: HandoffRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> RunResponse:
        if idempotency_key:
            cached = idem_cache.get("handoff", idempotency_key)
            if cached is not None:
                return RunResponse(**cached)
        handoff = Handoff(
            task_id=new_task_id(),
            from_agent=req.from_agent,
            to_agent=req.to_agent,
            payload=req.payload,
        )
        try:
            outcome, _ = await runtime.execute(handoff)
        except HandoffRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SLAExceeded as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        save_graph_best_effort()
        resp = RunResponse(
            task_id=handoff.task_id,
            hops=[{"agent": outcome.agent, "status": outcome.status, "output": outcome.output}],
        )
        if idempotency_key:
            idem_cache.put("handoff", idempotency_key, resp.model_dump())
        return resp

    @app.post("/signal", dependencies=[Depends(require_api_token)])
    async def receive_signal(
        sig: SignalIn,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if idempotency_key:
            cached = idem_cache.get("signal", idempotency_key)
            if cached is not None:
                return cached
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
        except SLAExceeded as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        save_graph_best_effort()
        resp = {"task_id": handoff.task_id, "outcome": outcome.model_dump()}
        if idempotency_key:
            idem_cache.put("signal", idempotency_key, resp)
        return resp

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

    # ----------------------------------------------------------------
    # Wiki endpoints — registered only when AGENTCORE_ENABLE_WIKI=true
    # so the disabled mode keeps the surface minimal.
    # ----------------------------------------------------------------
    if settings.enable_wiki:
        _register_wiki_routes(
            app, settings, router, require_api_token, job_queue, job_handlers
        )

    return app


def _register_wiki_routes(  # type: ignore[no-untyped-def]
    app: FastAPI, settings, router, require_api_token, job_queue, job_handlers
) -> None:
    """Wire the living-wiki endpoints. Off-by-default; only invoked from
    `build_app` when `AGENTCORE_ENABLE_WIKI=true`."""
    import contextlib

    from agentcore.adapters.git_local import GitAdapter
    from agentcore.wiki.curator import WikiCurator
    from agentcore.wiki.index import WikiIndex
    from agentcore.wiki.storage import WikiStorage

    branch = "default"
    with contextlib.suppress(Exception):
        b = GitAdapter(repo_root=settings.graphify_repo_root).current_branch()
        if b:
            branch = b
    storage = WikiStorage(settings.wiki_root, settings.project_name, branch)

    embedder = None
    vector = None
    with contextlib.suppress(Exception):
        from agentcore.memory.embed import Embedder
        from agentcore.memory.vector import VectorStore

        v = VectorStore(settings)
        with contextlib.suppress(Exception):
            v.init_schema()
            vector = v
            embedder = Embedder(settings)
    index = WikiIndex(storage, embedder, vector)
    curator = WikiCurator(
        router, storage, index, curator_model=settings.wiki_curator_model
    )

    # Register job handlers so the worker loop can drain wiki refresh jobs.
    async def _h_seed(payload: dict[str, Any]) -> None:
        await curator.seed_from_repo(
            settings.graphify_repo_root, commit_sha=payload.get("commit_sha")
        )

    async def _h_incremental(payload: dict[str, Any]) -> None:
        await curator.incremental(
            payload.get("changed_paths") or [],
            settings.graphify_repo_root,
            commit_sha=payload.get("commit_sha"),
        )

    job_handlers["wiki.refresh.seed"] = _h_seed
    job_handlers["wiki.refresh.incremental"] = _h_incremental

    # Stale-index startup warning. Pure / fast / no LLM calls; gives the
    # operator an immediate "your wiki is N pages behind the source" signal.
    with contextlib.suppress(Exception):
        report = curator.lint(settings.graphify_repo_root)
        if report.orphans or report.stale or report.missing_coverage:
            log.warning(
                "wiki.index_stale",
                orphans=len(report.orphans),
                stale=len(report.stale),
                missing_coverage=len(report.missing_coverage),
                hint="run `agentcore wiki rebuild` or POST /wiki/refresh to refresh",
            )

    class WikiRefreshIn(BaseModel):
        commit_sha: str | None = None
        changed_paths: list[str] = []
        mode: str = "incremental"  # "incremental" | "seed" | "lint"

    @app.get("/wiki")
    async def wiki_index() -> dict[str, Any]:
        pages = [
            {"rel": p.rel, "title": p.title, "sources": p.sources}
            for p in storage.walk()
        ]
        return {
            "project": storage.project,
            "branch": storage.branch,
            "root": str(storage.root),
            "count": len(pages),
            "pages": pages,
        }

    @app.get("/wiki/search")
    async def wiki_search(q: str, k: int = 8) -> dict[str, Any]:
        if not index.is_ready:
            raise HTTPException(status_code=503, detail="wiki retrieval unavailable")
        hits = await index.search(q, k=k)
        return {
            "query": q,
            "hits": [
                {"rel": h.rel, "title": h.title, "score": h.score, "excerpt": h.excerpt}
                for h in hits
            ],
        }

    @app.get("/wiki/{path:path}")
    async def wiki_page(path: str) -> dict[str, Any]:
        page = storage.read(path)
        if page is None:
            raise HTTPException(status_code=404, detail=f"no wiki page at {path!r}")
        return {
            "rel": page.rel,
            "title": page.title,
            "frontmatter": page.frontmatter,
            "body": page.body,
        }

    @app.post("/wiki/refresh", dependencies=[Depends(require_api_token)], status_code=202)
    async def wiki_refresh(
        req: WikiRefreshIn,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        """Enqueue a wiki refresh. Lint is cheap so stays inline.

        Seed/incremental are pushed to the durable Postgres job queue —
        git hooks / webhooks return 202 in milliseconds while the curator
        drains off-thread. Idempotency-Key collapses duplicate enqueues
        thanks to the partial unique index on (kind, idempotency_key).
        """
        repo = settings.graphify_repo_root
        if req.mode == "lint":
            report = curator.lint(repo)
            return {
                "mode": "lint",
                "orphans": report.orphans,
                "stale": report.stale,
                "missing_coverage": report.missing_coverage,
            }
        if req.mode == "seed":
            jid = job_queue.enqueue(
                "wiki.refresh.seed",
                {"commit_sha": req.commit_sha},
                idempotency_key=idempotency_key,
                created_by="wiki/refresh",
            )
            return {"mode": "seed", "status": "queued", "job_id": jid}
        # default: incremental
        changed_paths = list(req.changed_paths)
        if len(changed_paths) > settings.wiki_max_changed_paths:
            raise HTTPException(
                status_code=413,
                detail=f"changed_paths exceeds limit {settings.wiki_max_changed_paths}",
            )
        jid = job_queue.enqueue(
            "wiki.refresh.incremental",
            {"changed_paths": changed_paths, "commit_sha": req.commit_sha},
            idempotency_key=idempotency_key,
            created_by="wiki/refresh",
        )
        return {"mode": "incremental", "status": "queued", "job_id": jid}


app = build_app()
