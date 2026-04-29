"""UI routes. Registered by `orchestrator/app.py` via `mount_ui(app, ...)`.

Every view is a read-only Jinja render over data the HTTP API already
exposes — the UI is a thin projection, never a second source of truth.
Writes (retry, cancel, refresh) redirect to the JSON endpoints with
the same auth/idempotency semantics.
"""

from __future__ import annotations

import contextlib
import json
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))


# 3-second TTL cache for graph snapshots + stat counts. The graph view
# reloads often as operators click around; re-running the full node/edge
# query every time is wasteful when the data hasn't changed. Keyed by
# (project_id, fn_name) so multi-tenant queries don't cross.
_CACHE_TTL = 3.0
_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}


def _cached(key: tuple[str, str], loader):  # type: ignore[no-untyped-def]
    """Tiny manual TTL cache. We don't use functools.lru_cache here
    because Settings/JobQueue aren't hashable and we want per-project
    freshness, not eternal memoisation."""
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and (now - hit[0]) < _CACHE_TTL:
        return hit[1]
    value = loader()
    _CACHE[key] = (now, value)
    # Bound memory — LRU-ish eviction when we exceed 64 entries.
    if len(_CACHE) > 64:
        oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
        _CACHE.pop(oldest, None)
    return value


def mount_ui(  # type: ignore[no-untyped-def]
    app: FastAPI,
    *,
    settings,
    registry,
    job_queue,
    idem_cache,
    host_info,
    wiki_storage=None,
) -> None:
    """Mount `/ui/*` routes + `/ui/static/*` assets onto `app`.

    All dependencies are injected — the UI owns no state of its own.
    Wiki routes are registered only when `wiki_storage` is provided
    (i.e. `AGENTCORE_ENABLE_WIKI=true`), matching the HTTP surface.
    """
    app.mount(
        "/ui/static",
        StaticFiles(directory=str(_HERE / "static")),
        name="ui-static",
    )

    def _pid(request: Request) -> str:
        """Project id for the current request. Precedence: ?project= query
        param, then X-Project-Id header, then server default. Lets you
        switch tenants from the UI without restarting the server."""
        q = request.query_params.get("project")
        if q:
            return q
        h = request.headers.get("x-project-id")
        if h:
            return h
        return settings.project_name

    def _ctx(request: Request, active: str, **extra: Any) -> dict[str, Any]:
        pid = _pid(request)
        projects = _known_projects(settings)
        return {
            "request": request,
            "active": active,
            "project": pid,
            "projects": projects,
            "wiki_enabled": settings.enable_wiki,
            **extra,
        }

    @app.get("/ui", include_in_schema=False)
    async def ui_root() -> RedirectResponse:
        return RedirectResponse(url="/ui/dashboard", status_code=307)

    @app.get("/ui/dashboard", response_class=HTMLResponse, name="ui-dashboard")
    async def ui_dashboard(request: Request) -> HTMLResponse:
        from agentcore.capabilities import detect_capabilities

        capabilities = {}
        with contextlib.suppress(Exception):
            capabilities = detect_capabilities(settings)

        pid = _pid(request)
        stats = _compute_stats(settings, registry, job_queue, wiki_storage, pid)
        recent = _recent_chains(settings, job_queue, limit=5, project_id=pid)

        return _TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            _ctx(
                request,
                "dashboard",
                health={"ok": len(registry.errors()) == 0},
                host={"os": host_info.os, "arch": host_info.arch, "shell": host_info.shell},
                stats=stats,
                capabilities={
                    # `__slots__` covers the dataclass fields; `status`
                    # is a derived @property and must be added by hand,
                    # otherwise the template's `cap.status` renders
                    # empty.
                    k: {
                        **{f: getattr(v, f) for f in v.__slots__},
                        "status": v.status,
                    }
                    for k, v in capabilities.items()
                },
                recent_chains=recent,
            ),
        )

    @app.get("/ui/agents", response_class=HTMLResponse, name="ui-agents")
    async def ui_agents(request: Request) -> HTMLResponse:
        pid = _pid(request)
        # Per-agent activity from the operational graph: how many tasks
        # they've worked on, recent task ids + ages. Lets the operator
        # expand a row and see what an agent has actually been doing.
        activity = _agent_activity(settings, pid)
        agents = [
            {
                "name": s.name,
                "description": s.description,
                "provider": s.llm.provider,
                "model": s.llm.model,
                "accepts_from": s.contract.accepts_handoff_from,
                "delegates_to": s.contract.delegates_to,
                "sla_seconds": s.contract.sla_seconds,
                "source_path": s.source_path or "",
                "tasks_count": activity.get(s.name, {}).get("tasks_count", 0),
                "recent_tasks": activity.get(s.name, {}).get("recent_tasks", []),
                "delegations_made": activity.get(s.name, {}).get("delegations_made", 0),
                "delegations_received": activity.get(s.name, {}).get("delegations_received", 0),
            }
            for s in registry.all()
        ]
        return _TEMPLATES.TemplateResponse(
            request,
            "agents.html",
            _ctx(
                request,
                "agents",
                agents=agents,
                errors=registry.errors(),
                agents_dir=str(settings.agents_dir),
            ),
        )

    @app.get("/ui/chains", response_class=HTMLResponse, name="ui-chains")
    async def ui_chains(request: Request) -> HTMLResponse:
        pid = _pid(request)
        return _TEMPLATES.TemplateResponse(
            request,
            "chains.html",
            _ctx(
                request, "chains",
                chains=_recent_chains(settings, job_queue, limit=50, project_id=pid),
            ),
        )

    @app.get("/ui/chains/{chain_id}", response_class=HTMLResponse, name="ui-chain-detail")
    async def ui_chain_detail(request: Request, chain_id: str) -> HTMLResponse:
        pid = _pid(request)
        # First source: idempotency cache (HTTP-driven chains carry full
        # hop arrays here). Fall back to reconstruction from the graph
        # so CLI-driven chains (which never touch the idempotency cache)
        # still render a useful detail view.
        chain = idem_cache.get("chain", chain_id, project_id=pid)
        if chain is None:
            chain = _chain_detail_from_graph(settings, chain_id, pid)
        in_flight = _chain_in_flight_jobs(job_queue, chain_id, project_id=pid)
        review_history = _chain_review_history(chain_id)
        return _TEMPLATES.TemplateResponse(
            request,
            "chain_detail.html",
            _ctx(
                request,
                "chains",
                chain_id=chain_id,
                chain=chain,  # None if neither source has it
                in_flight=in_flight,
                review_history=review_history,
            ),
        )

    @app.get("/ui/graph", response_class=HTMLResponse, name="ui-graph")
    async def ui_graph(request: Request) -> HTMLResponse:
        pid = _pid(request)
        nodes, edges, kinds = _cached(
            (pid, "graph_snapshot"),
            lambda: _graph_snapshot(settings, limit_nodes=200, project_id=pid),
        )
        return _TEMPLATES.TemplateResponse(
            request,
            "graph.html",
            _ctx(
                request,
                "graph",
                nodes=nodes,
                edges=edges,
                kinds=kinds,
                node_count=len(nodes),
                edge_count=len(edges),
            ),
        )

    @app.get("/ui/jobs", response_class=HTMLResponse, name="ui-jobs")
    async def ui_jobs(request: Request) -> HTMLResponse:
        pid = _pid(request)
        jobs = _recent_jobs(job_queue, pid, limit=50)
        dead_letter = []
        with contextlib.suppress(Exception):
            dead_letter = job_queue.list_dead_letter(project_id=pid, limit=20)
        counts = {
            "queued": sum(1 for j in jobs if j["status"] == "queued"),
            "running": sum(1 for j in jobs if j["status"] == "running"),
            "dead_letter": len(dead_letter),
        }
        return _TEMPLATES.TemplateResponse(
            request,
            "jobs.html",
            _ctx(request, "jobs", jobs=jobs, dead_letter=dead_letter, counts=counts),
        )

    if settings.enable_wiki and wiki_storage is not None:
        @app.get("/ui/wiki", response_class=HTMLResponse, name="ui-wiki")
        async def ui_wiki(request: Request) -> HTMLResponse:
            pages = [
                {"rel": p.rel, "title": p.title, "sources": p.sources}
                for p in wiki_storage.walk()
            ]
            total_sources = sum(len(p["sources"]) for p in pages)
            return _TEMPLATES.TemplateResponse(
                request,
                "wiki.html",
                _ctx(
                    request,
                    "wiki",
                    pages=pages,
                    project=settings.project_name,
                    root=str(wiki_storage.root),
                    branch=wiki_storage.branch,
                    total_sources=total_sources,
                ),
            )

        @app.get(
            "/ui/wiki/page/{rel:path}",
            response_class=HTMLResponse,
            name="ui-wiki-page",
        )
        async def ui_wiki_page(request: Request, rel: str) -> HTMLResponse:
            from fastapi import HTTPException as _HTTPException

            page = wiki_storage.read(rel)
            if page is None:
                raise _HTTPException(status_code=404, detail=f"page {rel!r} not found")
            # Render the markdown body to HTML using markdown-it-py
            # (already a transitive dep via rich). Falls back to a
            # `<pre>` block if rendering fails for any reason — never
            # leaks raw HTML if the page contains untrusted content.
            try:
                from markdown_it import MarkdownIt

                md = MarkdownIt("commonmark", {"breaks": True, "html": False})
                rendered_html = md.render(page.body or "")
            except Exception:
                from html import escape

                rendered_html = (
                    f"<pre style='white-space: pre-wrap'>"
                    f"{escape(page.body or '')}</pre>"
                )
            return _TEMPLATES.TemplateResponse(
                request,
                "wiki_page.html",
                _ctx(
                    request,
                    "wiki",
                    page=page,
                    rendered_html=rendered_html,
                ),
            )


# ---------------------------------------------------------------------------
# Read helpers — all best-effort, UI degrades gracefully if Postgres is down.
# ---------------------------------------------------------------------------


def _known_projects(settings) -> list[str]:  # type: ignore[no-untyped-def]
    """All project_ids that have state anywhere in the DB. Populates the
    header switcher so an operator can jump between tenants without
    knowing the names ahead of time. Best-effort; empty on DB miss."""
    import psycopg

    sql = """
    SELECT DISTINCT project_id FROM agentcore_jobs
    UNION
    SELECT DISTINCT project_id FROM agentcore_idempotency
    UNION
    SELECT DISTINCT project_id FROM agentcore_graph_nodes
    ORDER BY 1
    """
    try:
        with (
            psycopg.connect(settings.pg_dsn, autocommit=True, connect_timeout=2) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(sql)
            found = [str(r[0]) for r in cur.fetchall() or []]
    except Exception:
        found = []
    # Always include the server default so the switcher renders even
    # on a fresh DB.
    if settings.project_name not in found:
        found.insert(0, settings.project_name)
    return found


def _compute_stats(  # type: ignore[no-untyped-def]
    settings, registry, job_queue, wiki_storage, project_id: str
) -> dict[str, Any]:
    """Single Postgres round-trip for the dashboard counters.

    Returns dashes for any subsystem that's offline. Never raises.
    """
    stats: dict[str, Any] = {
        "agents_loaded": len(registry.all()),
        "agent_errors": len(registry.errors()),
        "jobs_queued": 0,
        "jobs_running": 0,
        "jobs_dead": 0,
        "chains_24h": 0,
        "chains_active": 0,
        "graph_nodes": 0,
        "graph_edges": 0,
        "wiki_pages": 0,
        "wiki_stale": 0,
    }
    with contextlib.suppress(Exception):
        counts = _job_counts(job_queue, project_id)
        stats["jobs_queued"] = counts.get("queued", 0)
        stats["jobs_running"] = counts.get("running", 0)
        stats["jobs_dead"] = counts.get("dead_letter", 0)
        stats["chains_active"] = counts.get("chain_running", 0)
    # Chains come from the idempotency cache (scope='chain'), not jobs.
    # Every /run and /handoff writes one row keyed by task_id.
    with contextlib.suppress(Exception):
        stats["chains_24h"] = _chain_count_24h(settings, project_id)
    with contextlib.suppress(Exception):
        n, e = _cached(
            (project_id, "graph_sizes"),
            lambda: _graph_sizes(settings, project_id),
        )
        stats["graph_nodes"] = n
        stats["graph_edges"] = e
    if wiki_storage is not None:
        with contextlib.suppress(Exception):
            pages = list(wiki_storage.walk())
            stats["wiki_pages"] = len(pages)
    return stats


def _chain_count_24h(settings, project_id: str) -> int:  # type: ignore[no-untyped-def]
    """Count distinct chains in the last 24h. Two sources unioned:

      1. `agentcore_idempotency` rows with scope IN ('chain','run')
         — written by HTTP `/run` and durable chain handlers.
      2. `agentcore_graph_nodes` with kind='task' — written by EVERY
         chain hop (CLI or HTTP), so this catches `agentcore plan`
         runs that never went through the orchestrator.

    DISTINCT id avoids double-counting when both sources record the
    same chain.
    """
    import psycopg

    sql = """
    SELECT count(*) FROM (
      SELECT key AS id FROM agentcore_idempotency
       WHERE project_id = %s
         AND scope IN ('chain', 'run')
         AND created_at > now() - interval '24 hours'
      UNION
      SELECT replace(id, 'task:', '') AS id FROM agentcore_graph_nodes
       WHERE project_id = %s
         AND kind = 'task'
         AND last_seen > now() - interval '24 hours'
    ) AS u
    """
    with (
        psycopg.connect(settings.pg_dsn, autocommit=True, connect_timeout=2) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, (project_id, project_id))
        row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def _chain_review_history(chain_id: str) -> list[dict[str, Any]]:
    """Extract review-round verdicts and route-backs from the local
    JSONL trace at ~/.agentcore/traces/<chain-id>.jsonl. Lets the UI
    show WHY a chain stalled or got rejected — not just that it did.

    Returns a list of `{round, agent, approved, comments, blockers,
    route_back_to}` entries per verdict, plus synthetic entries for
    `route_back` / `result` events.
    """
    from pathlib import Path as _P

    path = _P.home() / ".agentcore" / "traces" / f"{chain_id}.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = evt.get("kind")
                if kind not in ("verdict", "route_back", "result", "review_round"):
                    continue
                out.append({
                    "kind": kind,
                    "round": evt.get("step"),
                    "actor": evt.get("actor"),
                    "at": evt.get("at"),
                    **(evt.get("detail") or {}),
                })
    except OSError:
        return []
    return out


def _chain_detail_from_graph(  # type: ignore[no-untyped-def]
    settings, chain_id: str, project_id: str
) -> dict[str, Any] | None:
    """Reconstruct a chain detail from the operational graph for chains
    that never touched the idempotency cache (CLI runs).

    Returns the same shape as an idempotency-stored chain:
      {chain_id, status, hops: [{agent, status, output?}]}
    """
    import psycopg

    task_id = f"task:{chain_id}"
    try:
        with (
            psycopg.connect(settings.pg_dsn, autocommit=True, connect_timeout=2) as conn,
            conn.cursor() as cur,
        ):
            # The task node — gives us labels for status inference.
            cur.execute(
                "SELECT labels, last_seen FROM agentcore_graph_nodes "
                "WHERE project_id = %s AND id = %s",
                (project_id, task_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            labels, last_seen = row[0] or {}, row[1]
            if "qa_passed" in labels or "positive" in labels:
                status = "done"
            elif "qa_failed" in labels:
                status = "failed"
            elif "dev_revised" in labels:
                status = "in_review"
            else:
                status = "ran"

            # Hops: agents that worked on this task, with the snippets
            # they produced (a proxy for "output").
            cur.execute(
                """
                SELECT
                    replace(e.source, 'agent:', '') AS agent,
                    n.id, n.attrs, n.labels
                  FROM agentcore_graph_edges e
                  LEFT JOIN agentcore_graph_nodes n
                    ON n.project_id = e.project_id AND n.id = e.target
                 WHERE e.project_id = %s
                   AND e.target = %s
                   AND e.relation = 'worked_on'
                   AND e.source LIKE 'agent:%%'
              ORDER BY agent
                """,
                (project_id, task_id),
            )
            agents_seen: list[str] = []
            for agent, _nid, _attrs, _lbls in cur.fetchall() or []:
                a = str(agent)
                if a not in agents_seen:
                    agents_seen.append(a)

            # Snippets produced for this task — give the operator
            # something to read in the chain detail view. Pull the
            # actual content + role + intent + file path so the UI
            # has something to show, not just opaque ids.
            cur.execute(
                """
                SELECT
                    n2.id,
                    n2.attrs->>'file' AS file,
                    n2.attrs->>'role' AS role,
                    n2.attrs->>'intent' AS intent,
                    n2.attrs->>'content' AS content,
                    (n2.attrs->>'start')::int AS start_line,
                    (n2.attrs->>'end')::int AS end_line,
                    n2.kind
                  FROM agentcore_graph_edges e
                  JOIN agentcore_graph_nodes n2
                    ON n2.project_id = e.project_id AND n2.id = e.target
                 WHERE e.project_id = %s
                   AND e.source = %s
                   AND e.relation = 'produced'
              ORDER BY n2.last_seen DESC
                 LIMIT 50
                """,
                (project_id, task_id),
            )
            snippets = [
                {
                    "id": str(sid),
                    "file": file_,
                    "role": role,
                    "intent": (intent or "")[:200],
                    "content": (content or "")[:4000],
                    "lines": (
                        f"{start_line}-{end_line}"
                        if start_line and end_line and start_line != end_line
                        else (str(start_line) if start_line else "")
                    ),
                    "kind": k,
                }
                for sid, file_, role, intent, content, start_line, end_line, k
                in cur.fetchall() or []
            ]

            # Files this task touched.
            cur.execute(
                """
                SELECT replace(target, 'file:', '') AS path
                  FROM agentcore_graph_edges
                 WHERE project_id = %s AND source = %s AND relation = 'changed'
                """,
                (project_id, task_id),
            )
            files = [str(p) for (p,) in cur.fetchall() or []]

        hops = [
            {
                "agent": a,
                "status": "ok",
                "output": None,
                "delegate_to": None,
            }
            for a in agents_seen
        ]
        return {
            "chain_id": chain_id,
            "status": status,
            "hops": hops,
            "files_touched": files,
            "snippets_produced": snippets,
            "last_seen": _age(last_seen),
            "_source": "graph",
        }
    except Exception as exc:
        log.warning("ui.chain_from_graph_failed", error=str(exc))
        return None


def _agent_activity(  # type: ignore[no-untyped-def]
    settings, project_id: str
) -> dict[str, dict[str, Any]]:
    """Per-agent activity rollup from the graph: how many tasks each
    agent worked on, recent tasks with ages, and how many handoffs each
    side of the agent->agent edge participated in.

    Returns `{agent_name: {tasks_count, recent_tasks, delegations_made,
    delegations_received}}`. Empty dict on DB miss (UI degrades to
    static info).
    """
    import psycopg

    out: dict[str, dict[str, Any]] = {}
    try:
        with (
            psycopg.connect(settings.pg_dsn, autocommit=True, connect_timeout=2) as conn,
            conn.cursor() as cur,
        ):
            # Total tasks each agent worked on (one edge per agent per
            # task; counts dedupe via DISTINCT).
            cur.execute(
                """
                SELECT replace(source, 'agent:', '') AS agent,
                       count(DISTINCT target) AS n
                  FROM agentcore_graph_edges
                 WHERE project_id = %s
                   AND relation = 'worked_on'
                   AND source LIKE 'agent:%%'
              GROUP BY source
                """,
                (project_id,),
            )
            for agent, n in cur.fetchall() or []:
                out.setdefault(str(agent), {})["tasks_count"] = int(n)

            # Recent tasks per agent — last 5 by task last_seen.
            cur.execute(
                """
                SELECT
                    replace(e.source, 'agent:', '') AS agent,
                    replace(n.id, 'task:', '')      AS chain_id,
                    n.last_seen
                  FROM agentcore_graph_edges e
                  JOIN agentcore_graph_nodes n
                    ON n.id = e.target AND n.project_id = e.project_id
                 WHERE e.project_id = %s
                   AND e.relation = 'worked_on'
                   AND e.source LIKE 'agent:%%'
                   AND n.kind = 'task'
              ORDER BY n.last_seen DESC
                """,
                (project_id,),
            )
            for agent, chain_id, last_seen in cur.fetchall() or []:
                bucket = out.setdefault(str(agent), {}).setdefault("recent_tasks", [])
                if len(bucket) >= 5:
                    continue
                bucket.append({
                    "chain_id": str(chain_id),
                    "updated_at": _age(last_seen),
                })

            # Handoff edges (delegations) — once each direction.
            cur.execute(
                """
                SELECT replace(source, 'agent:', '') AS src,
                       replace(target, 'agent:', '') AS tgt,
                       count(*) AS n
                  FROM agentcore_graph_edges
                 WHERE project_id = %s AND relation = 'handoff'
              GROUP BY source, target
                """,
                (project_id,),
            )
            for src, tgt, n in cur.fetchall() or []:
                out.setdefault(str(src), {})
                out.setdefault(str(tgt), {})
                out[str(src)]["delegations_made"] = (
                    out[str(src)].get("delegations_made", 0) + int(n)
                )
                out[str(tgt)]["delegations_received"] = (
                    out[str(tgt)].get("delegations_received", 0) + int(n)
                )
    except Exception as exc:
        log.warning("ui.agent_activity_failed", error=str(exc))
    return out


def _graph_sizes(settings, project_id: str) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    """(nodes, edges) for this project. Two count queries — cheap."""
    import psycopg

    with (
        psycopg.connect(settings.pg_dsn, autocommit=True, connect_timeout=2) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT count(*) FROM agentcore_graph_nodes WHERE project_id = %s",
            (project_id,),
        )
        n = int((cur.fetchone() or [0])[0] or 0)
        cur.execute(
            "SELECT count(*) FROM agentcore_graph_edges WHERE project_id = %s",
            (project_id,),
        )
        e = int((cur.fetchone() or [0])[0] or 0)
    return n, e


def _job_counts(job_queue, project_id: str) -> dict[str, int]:  # type: ignore[no-untyped-def]
    """Aggregate counts for the dashboard stat cards. One query."""
    import psycopg

    out = {
        "queued": 0,
        "running": 0,
        "dead_letter": 0,
        "chain_running": 0,
        "chain_24h": 0,
    }
    if not job_queue.is_persistent:
        return out
    sql = """
    SELECT
      count(*) FILTER (WHERE status = 'queued')                             AS queued,
      count(*) FILTER (WHERE status = 'running')                            AS running,
      count(*) FILTER (WHERE status = 'dead_letter')                        AS dead_letter,
      count(*) FILTER (WHERE kind = 'runtime.chain.advance'
                         AND status IN ('queued','running'))                AS chain_running,
      count(*) FILTER (WHERE kind = 'runtime.chain.advance'
                         AND created_at > now() - interval '24 hours')      AS chain_24h
    FROM agentcore_jobs
    WHERE project_id = %s
    """
    with (
        psycopg.connect(job_queue.settings.pg_dsn, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, (project_id,))
        row = cur.fetchone()
    if row:
        out["queued"] = int(row[0] or 0)
        out["running"] = int(row[1] or 0)
        out["dead_letter"] = int(row[2] or 0)
        out["chain_running"] = int(row[3] or 0)
        out["chain_24h"] = int(row[4] or 0)
    return out


def _recent_jobs(  # type: ignore[no-untyped-def]
    job_queue, project_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Most-recent N jobs across all statuses, with a pretty age."""
    import psycopg

    if not job_queue.is_persistent:
        return []
    sql = """
    SELECT id, kind, status, attempts, max_attempts, locked_by,
           created_at, started_at, finished_at
      FROM agentcore_jobs
     WHERE project_id = %s
  ORDER BY created_at DESC
     LIMIT %s
    """
    with (
        psycopg.connect(job_queue.settings.pg_dsn, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, (project_id, int(limit)))
        rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "kind": r[1],
            "status": r[2],
            "attempts": r[3],
            "max_attempts": r[4],
            "locked_by": r[5],
            "age": _age(r[6]),
        }
        for r in rows or []
    ]


def _chain_in_flight_jobs(  # type: ignore[no-untyped-def]
    job_queue, chain_id: str, *, project_id: str
) -> list[dict[str, Any]]:
    """Any `runtime.chain.advance` jobs still referencing this chain.

    Useful while the chain is mid-flight — the terminal idempotency
    row only appears on done/failed/cancelled, so for a live chain
    this is the only signal of progress beyond the hop counter in
    the cached state.
    """
    import psycopg

    if not job_queue.is_persistent:
        return []
    sql = """
    SELECT id, status, attempts, created_at, locked_by
      FROM agentcore_jobs
     WHERE project_id = %s
       AND kind = 'runtime.chain.advance'
       AND payload->>'chain_id' = %s
  ORDER BY created_at DESC
     LIMIT 20
    """
    with (
        psycopg.connect(job_queue.settings.pg_dsn, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, (project_id, chain_id))
        return [
            {
                "id": r[0],
                "status": r[1],
                "attempts": r[2],
                "age": _age(r[3]),
                "locked_by": r[4],
            }
            for r in cur.fetchall() or []
        ]


def _graph_snapshot(  # type: ignore[no-untyped-def]
    settings, *, limit_nodes: int = 200, project_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Pull the top-weighted slice of the graph for a static SVG view.

    We intentionally cap at `limit_nodes` — a full graph dump quickly
    swamps the browser. The slice is: the N highest-evidence nodes
    (by incident active_weight) plus every edge between members.
    Returns `(nodes, edges, kind_counts)`.
    """
    import psycopg

    nodes_sql = """
    WITH weighted AS (
      SELECT n.id, n.kind, n.attrs,
             coalesce(sum(e.active_weight), 0.0) AS score
        FROM agentcore_graph_nodes n
        LEFT JOIN agentcore_graph_edges e
          ON e.source = n.id OR e.target = n.id
         AND e.project_id = n.project_id
       WHERE n.project_id = %s
       GROUP BY n.id, n.kind, n.attrs
    )
    SELECT id, kind, attrs, score
      FROM weighted
  ORDER BY score DESC, id
     LIMIT %s
    """
    edges_sql = """
    SELECT source, target, relation, active_weight
      FROM agentcore_graph_edges
     WHERE project_id = %s
       AND source = ANY(%s)
       AND target = ANY(%s)
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    kinds: dict[str, int] = {}
    try:
        with (
            psycopg.connect(settings.pg_dsn, autocommit=True) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(nodes_sql, (project_id, int(limit_nodes)))
            for node_id, kind, attrs, score in cur.fetchall() or []:
                nodes.append(
                    {
                        "id": str(node_id),
                        "kind": str(kind),
                        "label": str(node_id).split(":", 1)[-1][:40],
                        "score": float(score or 0.0),
                        "attrs": attrs if isinstance(attrs, dict) else {},
                    }
                )
                kinds[str(kind)] = kinds.get(str(kind), 0) + 1

            if nodes:
                ids = [n["id"] for n in nodes]
                cur.execute(edges_sql, (project_id, ids, ids))
                for source, target, relation, w in cur.fetchall() or []:
                    edges.append(
                        {
                            "source": str(source),
                            "target": str(target),
                            "relation": str(relation),
                            "weight": float(w or 1.0),
                        }
                    )
    except Exception:
        # Graph not initialised yet — empty slice is fine, template
        # renders an empty-state card.
        return [], [], {}
    return nodes, edges, kinds


def _recent_chains(  # type: ignore[no-untyped-def]
    settings, job_queue, *, limit: int = 50, project_id: str | None = None
) -> list[dict[str, Any]]:
    """Recent chains, unioning two sources:

      1. `agentcore_idempotency` (scope='chain') — written by HTTP
         `POST /run` and the durable chain handler. Carries a rich
         payload (status, full hops array).
      2. `agentcore_graph_nodes` (kind='task') — written by EVERY
         chain hop. Catches `agentcore plan` (CLI) runs that never
         touched the HTTP path. Status is inferred from labels
         (`qa_passed` / `qa_failed`); hops not populated here.

    Idempotency entries take precedence when a chain id appears in
    both sources (richer payload).
    """
    import psycopg

    if not job_queue.is_persistent:
        return []

    pid = project_id or settings.project_name
    out: dict[str, dict[str, Any]] = {}

    with (
        psycopg.connect(job_queue.settings.pg_dsn, autocommit=True) as conn,
        conn.cursor() as cur,
    ):
        # Source 1: HTTP-driven chains via idempotency cache.
        cur.execute(
            """
            SELECT key, payload, created_at
              FROM agentcore_idempotency
             WHERE project_id = %s AND scope = 'chain'
          ORDER BY created_at DESC
             LIMIT %s
            """,
            (pid, int(limit)),
        )
        for key, payload, created_at in cur.fetchall() or []:
            body = payload if isinstance(payload, dict) else {}
            out[str(key)] = {
                "chain_id": str(key),
                "status": body.get("status", "done"),
                "hops": body.get("hops") or [],
                "updated_at": _age(created_at),
                "_ts": created_at,
                "source": "http",
            }

        # Source 2: CLI / direct chains via graph task nodes.
        # We pull two metrics in the same query: how many distinct
        # agents handed off on this chain (= hop count) and the labels
        # (for status inference).
        cur.execute(
            """
            SELECT
                replace(n.id, 'task:', '') AS chain_id,
                n.labels,
                n.last_seen,
                COALESCE(h.hop_count, 0) AS hop_count
              FROM agentcore_graph_nodes n
              LEFT JOIN (
                SELECT target, count(*) AS hop_count
                  FROM agentcore_graph_edges
                 WHERE project_id = %s AND relation = 'worked_on'
                 GROUP BY target
              ) AS h ON h.target = n.id
             WHERE n.project_id = %s AND n.kind = 'task'
          ORDER BY n.last_seen DESC
             LIMIT %s
            """,
            (pid, pid, int(limit)),
        )
        for chain_id, labels, last_seen, hop_count in cur.fetchall() or []:
            cid = str(chain_id)
            if cid in out:
                continue  # idempotency entry wins (richer)
            lbls = labels if isinstance(labels, dict) else {}
            # Infer status from PRF labels. None of these mean the
            # chain is currently *executing* — these are post-mortem
            # states inferred from labels written during the chain's
            # lifecycle. The CLI / orchestrator process is long gone
            # by the time the UI reads these.
            if "qa_passed" in lbls or "positive" in lbls:
                status = "done"            # converged + tests passed
            elif "qa_failed" in lbls or "negative" in lbls:
                status = "failed"          # ran but tests failed
            elif "dev_revised" in lbls or "architect_revised" in lbls:
                status = "incomplete"      # entered review, never converged
            elif int(hop_count) > 0:
                status = "incomplete"      # ran some hops, never tagged terminal
            else:
                status = "unknown"
            out[cid] = {
                "chain_id": cid,
                "status": status,
                # Hops as a list-of-dicts so the template's `c.hops|length`
                # reads the same as for HTTP-source rows. Content is a
                # placeholder (we know the count, not the per-hop detail).
                "hops": [{"agent": "?"}] * int(hop_count),
                "updated_at": _age(last_seen),
                "_ts": last_seen,
                "source": "cli",
            }

    rows = sorted(
        out.values(),
        key=lambda r: r.get("_ts") or 0,
        reverse=True,
    )[: int(limit)]
    for r in rows:
        r.pop("_ts", None)
    return rows


def _age(ts) -> str:  # type: ignore[no-untyped-def]
    """'2s ago', '4m ago', '3h ago' — tiny pretty-printer for the UI."""
    if ts is None:
        return ""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    delta = now - ts
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"
