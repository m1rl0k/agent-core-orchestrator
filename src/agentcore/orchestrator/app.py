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
    # When true, the chain runs as durable jobs — each hop is its own
    # row in agentcore_jobs, so an orchestrator restart in the middle
    # of a hop is reclaimed by another worker (or the same one on
    # boot) when the lock expires. Trade-off: every hop pays one extra
    # round-trip to Postgres; result is fetched via GET /chains/{id}.
    durable: bool = False


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
    traces = TraceLog(settings=settings)
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

    # ----------------------------------------------------------------
    # Durable chain handler — registered regardless of wiki state.
    # `durable: true` on /run enqueues runtime.chain.advance jobs; the
    # handler runs one hop and either finalises the chain (storing the
    # result in the idempotency cache under scope='chain') or
    # re-enqueues the successor. Worker death mid-hop is reclaimed via
    # the existing lock_until path, so chains survive orchestrator
    # restarts. At-least-once LLM calls, at-most-once hop OUTPUT (we
    # only re-enqueue after the hop's output is committed).
    # ----------------------------------------------------------------
    async def _runtime_chain_advance(payload: dict[str, Any]) -> None:
        chain_id = payload["chain_id"]
        pid = payload.get("project_id") or settings.project_name
        max_hops = int(payload.get("max_hops", 6))
        do_chain = bool(payload.get("chain", True))
        step = int(payload.get("step", 0))
        hops_so_far = list(payload.get("hops", []))

        # Cooperative cancel: DELETE /chains/{id} stamps the idem slot
        # with status='cancelled'. Honour it before doing any work so a
        # cancel between two queued hops actually stops the chain.
        existing = idem_cache.get("chain", chain_id, project_id=pid)
        if isinstance(existing, dict) and existing.get("status") == "cancelled":
            return

        handoff = Handoff(**payload["handoff"])
        try:
            outcome, nxt = await runtime.execute(handoff)
        except (HandoffRejected, SLAExceeded) as exc:
            # Fatal for this chain — record final state and stop.
            idem_cache.put(
                "chain",
                chain_id,
                {
                    "chain_id": chain_id,
                    "status": "failed",
                    "error": str(exc),
                    "hops": hops_so_far,
                },
                project_id=pid,
                ttl_seconds=86400.0,
            )
            return

        hops_so_far.append({
            "agent": outcome.agent,
            "status": outcome.status,
            "output": outcome.output,
            "delegate_to": outcome.delegate_to,
        })

        more = do_chain and nxt is not None and (step + 1) < max_hops
        if more and nxt is not None:
            job_queue.enqueue(
                "runtime.chain.advance",
                {
                    "chain_id": chain_id,
                    "project_id": pid,
                    "max_hops": max_hops,
                    "chain": do_chain,
                    "step": step + 1,
                    "hops": hops_so_far,
                    "handoff": nxt.model_dump(mode="json"),
                },
                project_id=pid,
                idempotency_key=f"{chain_id}:{step + 1}",
                created_by="runtime.chain",
            )
            return

        # Terminal: stash the final result under the chain id.
        idem_cache.put(
            "chain",
            chain_id,
            {"chain_id": chain_id, "status": "done", "hops": hops_so_far},
            project_id=pid,
            ttl_seconds=86400.0,
        )

    job_handlers["runtime.chain.advance"] = _runtime_chain_advance

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
        from agentcore.state.bootstrap import verify_schema
        from agentcore.state.jobs import run_worker as _run_worker

        # Schema drift check. Logs a structured warning if alembic head
        # doesn't match `alembic_version` in the DB; raises if
        # AGENTCORE_STRICT_SCHEMA=true so production deploys behind
        # `alembic upgrade head` fail loudly when migrations are stale.
        verify_schema(settings, strict=settings.strict_schema)

        stop = asyncio.Event()
        watcher = asyncio.create_task(
            watch_agents_dir(settings.agents_dir, registry, stop_event=stop)
        )
        # Drain the durable Postgres job queue. The runtime.chain.advance
        # handler is always registered now (durable chains), so this loop
        # is always live; wiki refresh, signal scans, etc. share the same
        # worker. Falls back to in-memory mode when the DB isn't reachable.
        worker = None
        if job_handlers:
            worker = asyncio.create_task(
                _run_worker(
                    job_queue, job_handlers,
                    stop_event=stop,
                    kind_limits=settings.kind_limits or None,
                )
            )

        # Periodic cleanup so durable tables don't grow unbounded.
        # idempotency expires by row TTL; jobs done/failed past
        # `jobs_retention_days` are removed wholesale.
        async def _cleanup_loop() -> None:
            interval = float(settings.cleanup_interval_seconds)
            try:
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=interval)
                        return  # stop event fired
                    except TimeoutError:
                        pass
                    try:
                        idem_cache.cleanup()
                    except Exception as exc:
                        log.warning("cleanup.idem_failed", error=str(exc))
                    try:
                        job_queue.cleanup(retention_days=settings.jobs_retention_days)
                    except Exception as exc:
                        log.warning("cleanup.jobs_failed", error=str(exc))
            except asyncio.CancelledError:
                return

        cleaner = asyncio.create_task(_cleanup_loop())
        try:
            yield
        finally:
            save_graph_best_effort()
            stop.set()
            watcher.cancel()
            if worker is not None:
                worker.cancel()
            cleaner.cancel()

    app = FastAPI(title="agent-core-orchestrator", lifespan=lifespan)

    # Mount the lightweight Jinja UI at /ui. Read-only views over the
    # same state the HTTP API exposes — dashboard, agents, chains,
    # jobs, wiki. Wiki tab only appears when AGENTCORE_ENABLE_WIKI=true.
    try:
        from agentcore.ui import mount_ui

        mount_ui(
            app,
            settings=settings,
            registry=registry,
            job_queue=job_queue,
            idem_cache=idem_cache,
            host_info=detect_host(),
            wiki_storage=None,  # populated by _register_wiki_routes when enabled
        )
    except Exception as exc:
        log.warning("ui.mount_failed", error=str(exc))

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

    @app.post("/run", dependencies=[Depends(require_api_token)])
    async def run(
        req: RunRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        project_id: str | None = Header(default=None, alias="X-Project-Id"),
    ) -> dict[str, Any]:
        pid = project_id or settings.project_name
        if idempotency_key:
            cached = idem_cache.get("run", idempotency_key, project_id=pid)
            if cached is not None:
                return cached
        handoff = Handoff(
            task_id=new_task_id(),
            from_agent=req.from_agent,
            to_agent=req.to_agent,
            payload=req.payload,
        )

        # Durable mode: enqueue first hop and return 202. Result fetched
        # later via GET /chains/{chain_id}. Survives restarts because each
        # hop is its own jobs row with lock_until reclaim semantics.
        if req.durable:
            chain_id = handoff.task_id
            jid = job_queue.enqueue(
                "runtime.chain.advance",
                {
                    "chain_id": chain_id,
                    "project_id": pid,
                    "max_hops": req.max_hops,
                    "chain": req.chain,
                    "step": 0,
                    "hops": [],
                    "handoff": handoff.model_dump(mode="json"),
                },
                project_id=pid,
                idempotency_key=f"{chain_id}:0",
                created_by="run/durable",
            )
            resp_d = {
                "task_id": chain_id,
                "chain_id": chain_id,
                "status": "queued",
                "job_id": jid,
                "project_id": pid,
            }
            if idempotency_key:
                idem_cache.put("run", idempotency_key, resp_d, project_id=pid)
            return resp_d
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
        resp_dict = RunResponse(task_id=handoff.task_id, hops=hops).model_dump()
        # Always stash chain state keyed by task_id so the UI has exactly
        # one place to look for any run's hops — durable or not.
        idem_cache.put(
            "chain", handoff.task_id,
            {"chain_id": handoff.task_id, "status": "done", "hops": hops},
            project_id=pid, ttl_seconds=86400.0,
        )
        if idempotency_key:
            idem_cache.put("run", idempotency_key, resp_dict, project_id=pid)
        return resp_dict

    @app.post("/handoff", response_model=RunResponse, dependencies=[Depends(require_api_token)])
    async def handoff_one(
        req: HandoffRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        project_id: str | None = Header(default=None, alias="X-Project-Id"),
    ) -> RunResponse:
        pid = project_id or settings.project_name
        if idempotency_key:
            cached = idem_cache.get("handoff", idempotency_key, project_id=pid)
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
        hops = [
            {"agent": outcome.agent, "status": outcome.status, "output": outcome.output}
        ]
        resp = RunResponse(task_id=handoff.task_id, hops=hops)
        # Mirror to scope='chain' — same rule as /run: task_id is the
        # canonical lookup key.
        idem_cache.put(
            "chain", handoff.task_id,
            {"chain_id": handoff.task_id, "status": "done", "hops": hops},
            project_id=pid, ttl_seconds=86400.0,
        )
        if idempotency_key:
            idem_cache.put("handoff", idempotency_key, resp.model_dump(), project_id=pid)
        return resp

    @app.post("/signal", dependencies=[Depends(require_api_token)])
    async def receive_signal(
        sig: SignalIn,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        project_id: str | None = Header(default=None, alias="X-Project-Id"),
    ) -> dict[str, Any]:
        pid = project_id or settings.project_name
        if idempotency_key:
            cached = idem_cache.get("signal", idempotency_key, project_id=pid)
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
            idem_cache.put("signal", idempotency_key, resp, project_id=pid)
        return resp

    @app.get("/chains/{chain_id}")
    async def chain_status(
        chain_id: str,
        project_id: str | None = Header(default=None, alias="X-Project-Id"),
    ) -> dict[str, Any]:
        """Status of a durable chain.

        Returned shapes:
          - terminal: {chain_id, status: "done"|"failed"|"cancelled", hops: [...]}
          - in-flight: {chain_id, status: "running"}
          - unknown: 404
        """
        pid = project_id or settings.project_name
        cached = idem_cache.get("chain", chain_id, project_id=pid)
        if cached is not None:
            return cached
        # Not in the result cache yet — chain is either still running or
        # the cache TTL expired. We return 'running' so polling clients
        # don't 404 spuriously between hops.
        return {"chain_id": chain_id, "status": "running"}

    @app.delete(
        "/chains/{chain_id}",
        dependencies=[Depends(require_api_token)],
    )
    async def cancel_chain(
        chain_id: str,
        project_id: str | None = Header(default=None, alias="X-Project-Id"),
    ) -> dict[str, Any]:
        """Cancel an in-flight durable chain.

        Soft cancel: any queued `runtime.chain.advance` job for this
        chain is marked `cancelled`; the handler reads the chain idem
        slot at the top of every hop and bails if it sees
        `status='cancelled'`. An in-flight hop already inside the
        runtime is NOT interrupted (the LLM call runs to its SLA), but
        the next hop will not be enqueued.
        """
        pid = project_id or settings.project_name
        # 1. Stamp the chain idem slot first so the in-flight handler
        #    sees the cancel before it would re-enqueue the next hop.
        idem_cache.put(
            "chain",
            chain_id,
            {
                "chain_id": chain_id,
                "status": "cancelled",
                "hops": [],
            },
            project_id=pid,
            ttl_seconds=86400.0,
        )
        # 2. Mark queued/locked successors so workers skip them too.
        cancelled = job_queue.cancel_chain(chain_id, project_id=pid)
        return {
            "chain_id": chain_id,
            "status": "cancelled",
            "jobs_cancelled": cancelled,
        }

    @app.get(
        "/jobs/dead-letter",
        dependencies=[Depends(require_api_token)],
    )
    async def dead_letter_list(
        project_id: str | None = Header(default=None, alias="X-Project-Id"),
        limit: int = 50,
    ) -> dict[str, Any]:
        """Inspect permanently-failed jobs (status='dead_letter').

        Dead-letter rows are NOT auto-deleted by the cleanup loop, so
        they accumulate until purged via `DELETE /jobs/dead-letter`.
        """
        pid = project_id or settings.project_name
        rows = job_queue.list_dead_letter(project_id=pid, limit=limit)
        return {"project_id": pid, "count": len(rows), "items": rows}

    @app.delete(
        "/jobs/dead-letter",
        dependencies=[Depends(require_api_token)],
    )
    async def dead_letter_purge(
        project_id: str | None = Header(default=None, alias="X-Project-Id"),
        older_than_days: int | None = None,
    ) -> dict[str, Any]:
        """Purge dead-letter rows. `older_than_days=null` (default)
        wipes all of them for the project; pass an int to keep recents."""
        pid = project_id or settings.project_name
        n = job_queue.purge_dead_letter(
            project_id=pid, older_than_days=older_than_days
        )
        return {"project_id": pid, "purged": n}

    @app.post(
        "/jobs/{job_id}/retry",
        dependencies=[Depends(require_api_token)],
    )
    async def job_retry(
        job_id: int,
        project_id: str | None = Header(default=None, alias="X-Project-Id"),
    ) -> dict[str, Any]:
        """Resurrect a dead-lettered job: reset its attempts, requeue it,
        and let the next worker claim it. 404 if no matching dead-letter
        row for this project."""
        pid = project_id or settings.project_name
        ok = job_queue.retry_dead_letter(job_id, project_id=pid)
        if not ok:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no dead-letter job {job_id} for project {pid!r} "
                    "(maybe wrong id, wrong project, or not dead-lettered)"
                ),
            )
        return {"job_id": job_id, "project_id": pid, "status": "queued"}

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
        project_id: str | None = Header(default=None, alias="X-Project-Id"),
    ) -> dict[str, Any]:
        pid = project_id or settings.project_name
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
                {"commit_sha": req.commit_sha, "project_id": pid},
                project_id=pid,
                idempotency_key=idempotency_key,
                created_by="wiki/refresh",
            )
            return {"mode": "seed", "status": "queued", "job_id": jid, "project_id": pid}
        # default: incremental
        changed_paths = list(req.changed_paths)
        if len(changed_paths) > settings.wiki_max_changed_paths:
            raise HTTPException(
                status_code=413,
                detail=f"changed_paths exceeds limit {settings.wiki_max_changed_paths}",
            )
        jid = job_queue.enqueue(
            "wiki.refresh.incremental",
            {
                "changed_paths": changed_paths,
                "commit_sha": req.commit_sha,
                "project_id": pid,
            },
            project_id=pid,
            idempotency_key=idempotency_key,
            created_by="wiki/refresh",
        )
        return {"mode": "incremental", "status": "queued", "job_id": jid, "project_id": pid}


app = build_app()
