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
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agentcore.state.db import pg_conn

log = structlog.get_logger(__name__)

_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))


# 3-second TTL cache for graph snapshots + stat counts. The graph view
# reloads often as operators click around; re-running the full node/edge
# query every time is wasteful when the data hasn't changed. Keyed by
# (project_id, fn_name) so multi-tenant queries don't cross.
_CACHE_TTL = 3.0
_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}
ALL_PROJECTS = "__all__"


def _is_all_projects(project_id: str | None) -> bool:
    return project_id in {ALL_PROJECTS, "all", "*"}


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
            return ALL_PROJECTS if _is_all_projects(q) else q
        h = request.headers.get("x-project-id")
        if h:
            return ALL_PROJECTS if _is_all_projects(h) else h
        return settings.project_name

    def _project_qs(request: Request, pid: str) -> str:
        """`?project=<pid>` suffix to thread through nav links so the
        operator's tenant choice survives navigation. Empty when the
        project is implicit (server default with no explicit override)
        — keeps default-tenant URLs clean and shareable."""
        explicit = (
            request.query_params.get("project")
            or request.headers.get("x-project-id")
        )
        if not explicit:
            return ""
        from urllib.parse import quote

        return f"?project={quote(pid, safe='')}"

    def _ctx(request: Request, active: str, **extra: Any) -> dict[str, Any]:
        pid = _pid(request)
        projects = _known_projects(settings)
        return {
            "request": request,
            "active": active,
            "project": pid,
            "project_qs": _project_qs(request, pid),
            "project_switch_url": request.url.path,
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
        detail = _cached(
            (pid, f"chain_detail:{chain_id}"),
            lambda: _load_chain_detail(
                settings, job_queue, idem_cache, chain_id, project_id=pid
            ),
        )
        return _TEMPLATES.TemplateResponse(
            request,
            "chain_detail.html",
            _ctx(
                request,
                "chains",
                chain_id=chain_id,
                chain=detail["chain"],  # None if neither source has it
                chain_jobs=detail["chain_jobs"],
                review_history=detail["review_history"],
            ),
        )

    @app.get("/ui/graph", response_class=HTMLResponse, name="ui-graph")
    async def ui_graph(request: Request) -> HTMLResponse:
        pid = _pid(request)
        raw_limit = request.query_params.get("limit", "1000")
        try:
            limit = int(raw_limit)
        except ValueError:
            limit = 1000
        limit = max(50, min(limit, 5000))
        nodes, edges, kinds = _cached(
            (pid, f"graph_snapshot:{limit}"),
            lambda: _graph_snapshot(settings, limit_nodes=limit, project_id=pid),
        )
        total_nodes, total_edges = _cached(
            (pid, "graph_sizes"),
            lambda: _graph_sizes(settings, pid),
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
                total_node_count=total_nodes,
                total_edge_count=total_edges,
                graph_limit=limit,
            ),
        )

    @app.get("/ui/jobs", response_class=HTMLResponse, name="ui-jobs")
    async def ui_jobs(request: Request) -> HTMLResponse:
        pid = _pid(request)
        jobs = _recent_jobs(job_queue, pid, limit=50)
        dead_letter = []
        with contextlib.suppress(Exception):
            dead_letter = job_queue.list_dead_letter(project_id=pid, limit=20)
        counts = _job_counts(job_queue, pid)
        counts["dead_letter"] = max(counts.get("dead_letter", 0), len(dead_letter))
        return _TEMPLATES.TemplateResponse(
            request,
            "jobs.html",
            _ctx(request, "jobs", jobs=jobs, dead_letter=dead_letter, counts=counts),
        )

    @app.post("/ui/jobs/{job_id}/retry", include_in_schema=False, name="ui-job-retry")
    async def ui_job_retry(request: Request, job_id: int) -> RedirectResponse:
        pid = _pid(request)
        job_queue.retry_dead_letter(job_id, project_id=pid)
        return RedirectResponse(
            url=f"{request.url_for('ui-jobs')}{_project_qs(request, pid)}",
            status_code=303,
        )

    if settings.enable_wiki and wiki_storage is not None:
        def _wiki_storage_for_project(  # type: ignore[no-untyped-def]
            project_id: str, branch: str | None = None
        ):
            target_branch = branch or wiki_storage.branch
            if (
                getattr(wiki_storage, "project", None) == project_id
                and getattr(wiki_storage, "branch", None) == target_branch
            ):
                return wiki_storage
            from agentcore.wiki.storage import WikiStorage

            return WikiStorage(
                wiki_storage.wiki_root,
                project_id,
                target_branch,
                settings=wiki_storage.settings,
            )

        @app.get("/ui/wiki", response_class=HTMLResponse, name="ui-wiki")
        async def ui_wiki(request: Request) -> HTMLResponse:
            pid = _pid(request)
            if _is_all_projects(pid):
                pages = _all_wiki_pages(settings, wiki_storage)
                root = str(wiki_storage.wiki_root)
                branch = "*"
            else:
                storage = _wiki_storage_for_project(
                    pid, request.query_params.get("branch")
                )
                pages = [
                    {
                        "project_id": pid,
                        "branch": storage.branch,
                        "rel": p.rel,
                        "title": p.title,
                        "sources": p.sources,
                    }
                    for p in storage.walk()
                ]
                root = str(storage.root)
                branch = storage.branch
            total_sources = sum(len(p["sources"]) for p in pages)
            return _TEMPLATES.TemplateResponse(
                request,
                "wiki.html",
                _ctx(
                    request,
                    "wiki",
                    pages=pages,
                    root=root,
                    branch=branch,
                    total_sources=total_sources,
                    project_switch_url=str(request.url_for("ui-wiki")),
                ),
            )

        @app.get(
            "/ui/wiki/page/{rel:path}",
            response_class=HTMLResponse,
            name="ui-wiki-page",
        )
        async def ui_wiki_page(request: Request, rel: str) -> HTMLResponse:
            from fastapi import HTTPException as _HTTPException

            storage = _wiki_storage_for_project(
                _pid(request), request.query_params.get("branch")
            )
            page = storage.read(rel)
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
                    project_switch_url=str(request.url_for("ui-wiki")),
                ),
            )


# ---------------------------------------------------------------------------
# Read helpers — all best-effort, UI degrades gracefully if Postgres is down.
# ---------------------------------------------------------------------------


def _known_projects(settings) -> list[str]:  # type: ignore[no-untyped-def]
    """All project_ids that have state anywhere in the DB. Populates the
    header switcher so an operator can jump between tenants without
    knowing the names ahead of time. Best-effort; empty on DB miss."""
    tables = (
        "agentcore_jobs",
        "agentcore_idempotency",
        "agentcore_graph_nodes",
        "agentcore_graph_edges",
        "agentcore_traces",
        "agentcore_wiki_pages",
    )
    found: set[str] = set()
    try:
        with (
            pg_conn(settings, timeout=2.0) as conn,
            conn.cursor() as cur,
        ):
            for table in tables:
                with contextlib.suppress(Exception):
                    cur.execute(f"SELECT DISTINCT project_id FROM {table}")
                    found.update(str(r[0]) for r in cur.fetchall() or [])
    except Exception:
        found = set()
    # Always include the server default so the switcher renders even
    # on a fresh DB.
    projects = sorted(found)
    if settings.project_name not in projects:
        projects.insert(0, settings.project_name)
    if len(projects) > 1:
        projects.insert(0, ALL_PROJECTS)
    return projects


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
            if _is_all_projects(project_id):
                stats["wiki_pages"] = _wiki_page_count(settings, project_id)
            else:
                pages = list(wiki_storage.walk())
                stats["wiki_pages"] = len(pages)
    return stats


def _wiki_page_count(settings, project_id: str) -> int:  # type: ignore[no-untyped-def]
    where = "" if _is_all_projects(project_id) else "WHERE project_id = %s"
    params: tuple[Any, ...] = () if _is_all_projects(project_id) else (project_id,)
    with pg_conn(settings, timeout=2.0) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM agentcore_wiki_pages {where}", params)
        row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def _all_wiki_pages(settings, wiki_storage) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """All persisted wiki pages across projects/branches for the UI."""
    pages: list[dict[str, Any]] = []
    with contextlib.suppress(Exception), pg_conn(settings, timeout=2.0) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT project_id, branch, rel, title, frontmatter
              FROM agentcore_wiki_pages
          ORDER BY project_id, branch, rel
            """
        )
        for project_id, branch, rel, title, fm in cur.fetchall() or []:
            frontmatter = fm if isinstance(fm, dict) else {}
            sources = frontmatter.get("sources") or []
            pages.append(
                {
                    "project_id": str(project_id),
                    "branch": str(branch),
                    "rel": str(rel),
                    "title": str(title or frontmatter.get("title") or rel),
                    "sources": [str(s) for s in sources],
                }
            )
    if pages:
        return pages

    # Fresh/dev fallback for disk-only wiki pages.
    root = getattr(wiki_storage, "wiki_root", None)
    if root is None:
        return []
    from agentcore.wiki.storage import WikiStorage

    for project_dir in sorted(Path(root).iterdir() if Path(root).exists() else []):
        if not project_dir.is_dir():
            continue
        for branch_dir in sorted(p for p in project_dir.iterdir() if p.is_dir()):
            storage = WikiStorage(
                root,
                project_dir.name,
                branch_dir.name,
                settings=getattr(wiki_storage, "settings", None),
            )
            for page in storage.walk():
                pages.append(
                    {
                        "project_id": project_dir.name,
                        "branch": branch_dir.name,
                        "rel": page.rel,
                        "title": page.title,
                        "sources": page.sources,
                    }
                )
    return pages


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
    idem_where = "" if _is_all_projects(project_id) else "project_id = %s AND"
    graph_where = "" if _is_all_projects(project_id) else "project_id = %s AND"
    sql = f"""
    SELECT count(*) FROM (
      SELECT key AS id FROM agentcore_idempotency
       WHERE {idem_where}
         scope IN ('chain', 'run')
         AND created_at > now() - interval '24 hours'
      UNION
      SELECT replace(id, 'task:', '') AS id FROM agentcore_graph_nodes
       WHERE {graph_where}
         kind = 'task'
         AND last_seen > now() - interval '24 hours'
    ) AS u
    """
    params: tuple[Any, ...] = () if _is_all_projects(project_id) else (project_id, project_id)
    with (
        pg_conn(settings, timeout=2.0) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, params)
        row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def _load_chain_detail(  # type: ignore[no-untyped-def]
    settings, job_queue, idem_cache, chain_id: str, *, project_id: str
) -> dict[str, Any]:
    """Load chain detail once for the UI TTL cache."""
    # First source: idempotency cache (HTTP-driven chains carry full
    # hop arrays here). Fall back to reconstruction from the graph
    # so CLI-driven chains (which never touch the idempotency cache)
    # still render a useful detail view.
    chain = idem_cache.get("chain", chain_id, project_id=project_id)
    graph_chain = _chain_detail_from_graph(settings, chain_id, project_id)
    if chain is None:
        chain = graph_chain
    elif graph_chain:
        chain = _merge_graph_chain_detail(chain, graph_chain)
    return {
        "chain": chain,
        "chain_jobs": _chain_jobs(job_queue, chain_id, project_id=project_id),
        "review_history": _chain_review_history(settings, chain_id, project_id),
    }


def _merge_graph_chain_detail(
    chain: dict[str, Any], graph_chain: dict[str, Any]
) -> dict[str, Any]:
    """Add graph-only UI context without replacing richer cached hops."""
    merged = dict(chain)
    for key in ("files_touched", "snippets_produced", "last_seen", "_source"):
        value = graph_chain.get(key)
        if value and not merged.get(key):
            merged[key] = value
    return merged


def _chain_review_history(
    settings, chain_id: str, project_id: str  # type: ignore[no-untyped-def]
) -> list[dict[str, Any]]:
    """Extract review-round verdicts and route-backs from durable traces.

    Postgres is preferred so multi-node orchestrators share review history.
    The local JSONL trace remains a fallback for disk-only CLI workflows.
    """
    out = _chain_review_history_pg(settings, chain_id, project_id)
    if out:
        return out
    return _chain_review_history_jsonl(chain_id)


def _chain_review_history_pg(
    settings, chain_id: str, project_id: str  # type: ignore[no-untyped-def]
) -> list[dict[str, Any]]:
    kinds = ("verdict", "route_back", "result", "review_round")
    sql = """
    SELECT step, kind, actor, at, detail
      FROM agentcore_traces
     WHERE project_id = %s
       AND task_id = %s
       AND kind = ANY(%s)
  ORDER BY at ASC, id ASC
    """
    try:
        with (
            pg_conn(settings, timeout=2.0) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(sql, (project_id, chain_id, list(kinds)))
            rows = cur.fetchall() or []
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for step, kind, actor, at, detail in rows:
        out.append({
            "kind": str(kind),
            "round": step,
            "actor": actor,
            "at": at.isoformat() if hasattr(at, "isoformat") else at,
            **(detail if isinstance(detail, dict) else {}),
        })
    return out


def _chain_review_history_jsonl(chain_id: str) -> list[dict[str, Any]]:
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
    task_id = f"task:{chain_id}"
    try:
        with (
            pg_conn(settings, timeout=2.0) as conn,
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
    out: dict[str, dict[str, Any]] = {}
    try:
        with (
            pg_conn(settings, timeout=2.0) as conn,
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
    where = "" if _is_all_projects(project_id) else "WHERE project_id = %s"
    params: tuple[Any, ...] = () if _is_all_projects(project_id) else (project_id,)
    with (
        pg_conn(settings, timeout=2.0) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            f"SELECT count(*) FROM agentcore_graph_nodes {where}",
            params,
        )
        n = int((cur.fetchone() or [0])[0] or 0)
        cur.execute(
            f"SELECT count(*) FROM agentcore_graph_edges {where}",
            params,
        )
        e = int((cur.fetchone() or [0])[0] or 0)
    return n, e


def _job_counts(job_queue, project_id: str) -> dict[str, int]:  # type: ignore[no-untyped-def]
    """Aggregate counts for the dashboard stat cards. One query."""
    out = {
        "queued": 0,
        "running": 0,
        "done": 0,
        "failed": 0,
        "cancelled": 0,
        "dead_letter": 0,
        "chain_running": 0,
        "chain_24h": 0,
    }
    if not job_queue.is_persistent:
        return out
    where = "" if _is_all_projects(project_id) else "WHERE project_id = %s"
    params: tuple[Any, ...] = () if _is_all_projects(project_id) else (project_id,)
    sql = f"""
    SELECT
      count(*) FILTER (WHERE status = 'queued')                             AS queued,
      count(*) FILTER (WHERE status = 'running')                            AS running,
      count(*) FILTER (WHERE status = 'done')                               AS done,
      count(*) FILTER (WHERE status = 'failed')                             AS failed,
      count(*) FILTER (WHERE status = 'cancelled')                          AS cancelled,
      count(*) FILTER (WHERE status = 'dead_letter')                        AS dead_letter,
      count(*) FILTER (WHERE kind = 'runtime.chain.advance'
                         AND status IN ('queued','running'))                AS chain_running,
      count(*) FILTER (WHERE kind = 'runtime.chain.advance'
                         AND created_at > now() - interval '24 hours')      AS chain_24h
    FROM agentcore_jobs
    {where}
    """
    with (
        pg_conn(job_queue.settings) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, params)
        row = cur.fetchone()
    if row:
        out["queued"] = int(row[0] or 0)
        out["running"] = int(row[1] or 0)
        out["done"] = int(row[2] or 0)
        out["failed"] = int(row[3] or 0)
        out["cancelled"] = int(row[4] or 0)
        out["dead_letter"] = int(row[5] or 0)
        out["chain_running"] = int(row[6] or 0)
        out["chain_24h"] = int(row[7] or 0)
    return out


def _recent_jobs(  # type: ignore[no-untyped-def]
    job_queue, project_id: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Most-recent queue jobs + chain executions, with a pretty age."""
    if not job_queue.is_persistent:
        return []
    where = "" if _is_all_projects(project_id) else "WHERE project_id = %s"
    params: tuple[Any, ...] = (int(limit),) if _is_all_projects(project_id) else (project_id, int(limit))
    sql = f"""
    SELECT project_id, id, kind, status, attempts, max_attempts, locked_by,
           created_at, started_at, finished_at, payload
      FROM agentcore_jobs
     {where}
  ORDER BY created_at DESC
     LIMIT %s
    """
    with (
        pg_conn(job_queue.settings) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, params)
        rows = cur.fetchall()

    out: list[dict[str, Any]] = []
    seen_chains: set[str] = set()
    for r in rows or []:
        row_project = str(r[0])
        payload = r[10] if isinstance(r[10], dict) else {}
        chain_id = payload.get("chain_id") or payload.get("source_task_id")
        if chain_id:
            seen_chains.add(f"{row_project}:{chain_id}")
        out.append(
            {
                "id": r[1],
                "project_id": row_project,
                "kind": r[2],
                "status": r[3],
                "attempts": r[4],
                "max_attempts": r[5],
                "locked_by": r[6],
                "age": _age(r[7]),
                "_ts": r[7],
                "chain_id": chain_id,
            }
        )

    out.extend(
        _chain_execution_jobs(
            job_queue.settings,
            project_id,
            exclude_chain_ids=seen_chains,
            limit=int(limit),
        )
    )
    out = sorted(out, key=lambda r: r.get("_ts") or 0, reverse=True)[: int(limit)]
    for r in out:
        r.pop("_ts", None)
    return out


def _chain_execution_jobs(
    settings,
    project_id: str,
    *,
    exclude_chain_ids: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Synthetic job rows for chain runs recorded outside the queue.

    Older CLI/non-durable runs were persisted to graph/traces/idempotency
    but not always to `agentcore_jobs`. The Jobs UI is an operator activity
    view, so those chain executions should still appear here.
    """
    graph_where = "" if _is_all_projects(project_id) else "project_id = %s AND"
    trace_where = "" if _is_all_projects(project_id) else "project_id = %s AND"
    idem_where = "" if _is_all_projects(project_id) else "project_id = %s AND"
    params: tuple[Any, ...] = (
        (int(limit),)
        if _is_all_projects(project_id)
        else (project_id, project_id, project_id, int(limit))
    )
    sql = f"""
    WITH chain_events AS (
      SELECT project_id,
             replace(id, 'task:', '') AS chain_id,
             labels,
             last_seen AS seen_at,
             NULL::jsonb AS payload
        FROM agentcore_graph_nodes
       WHERE {graph_where}
         kind = 'task'
      UNION ALL
      SELECT project_id,
             task_id AS chain_id,
             NULL::jsonb AS labels,
             max(at) AS seen_at,
             NULL::jsonb AS payload
        FROM agentcore_traces
       WHERE {trace_where}
         task_id IS NOT NULL
       GROUP BY project_id, task_id
      UNION ALL
      SELECT project_id,
             key AS chain_id,
             NULL::jsonb AS labels,
             created_at AS seen_at,
             payload
        FROM agentcore_idempotency
       WHERE {idem_where}
         scope = 'chain'
    ),
    latest AS (
      SELECT DISTINCT ON (project_id, chain_id) project_id, chain_id, labels, seen_at, payload
        FROM chain_events
       WHERE chain_id IS NOT NULL
    ORDER BY project_id, chain_id, seen_at DESC
    )
    SELECT project_id, chain_id, labels, seen_at, payload
      FROM latest
  ORDER BY seen_at DESC
     LIMIT %s
    """
    with (
        pg_conn(settings) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, params)
        rows = cur.fetchall() or []
    out: list[dict[str, Any]] = []
    for row_project, chain_id, labels, seen_at, payload in rows:
        row_project = str(row_project)
        cid = str(chain_id)
        if f"{row_project}:{cid}" in exclude_chain_ids:
            continue
        body = payload if isinstance(payload, dict) else {}
        status = body.get("status") or _status_from_graph_labels(labels)
        out.append(
            {
                "id": cid,
                "project_id": row_project,
                "kind": "chain.execution",
                "status": status,
                "attempts": 1,
                "max_attempts": 1,
                "locked_by": None,
                "age": _age(seen_at),
                "_ts": seen_at,
                "chain_id": cid,
            }
        )
    return out


def _status_from_graph_labels(labels: Any) -> str:
    lbls = labels if isinstance(labels, dict) else {}
    if "qa_passed" in lbls or "positive" in lbls:
        return "done"
    if "qa_failed" in lbls or "negative" in lbls:
        return "failed"
    if "dev_revised" in lbls or "architect_revised" in lbls:
        return "incomplete"
    return "ran"


def _chain_jobs(  # type: ignore[no-untyped-def]
    job_queue, chain_id: str, *, project_id: str
) -> list[dict[str, Any]]:
    """Queue rows related to a chain, across active and terminal statuses."""
    if not job_queue.is_persistent:
        return []
    sql = """
    SELECT id, kind, status, attempts, max_attempts, created_at, started_at,
           finished_at, locked_by, created_by, error
      FROM agentcore_jobs
     WHERE project_id = %s
       AND (
            payload->>'chain_id' = %s
         OR payload->>'source_task_id' = %s
         OR idempotency_key = %s
         OR idempotency_key LIKE %s
         OR created_by = %s
       )
  ORDER BY created_at DESC
     LIMIT 50
    """
    with (
        pg_conn(job_queue.settings) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            sql,
            (
                project_id,
                chain_id,
                chain_id,
                f"chain:{chain_id}",
                f"{chain_id}:%",
                f"chain:{chain_id}",
            ),
        )
        return [
            {
                "id": r[0],
                "kind": r[1],
                "status": r[2],
                "attempts": r[3],
                "max_attempts": r[4],
                "age": _age(r[5]),
                "started": _age(r[6]),
                "finished": _age(r[7]),
                "locked_by": r[8],
                "created_by": r[9],
                "error": r[10],
            }
            for r in cur.fetchall() or []
        ]


def _chain_in_flight_jobs(  # type: ignore[no-untyped-def]
    job_queue, chain_id: str, *, project_id: str
) -> list[dict[str, Any]]:
    """Backward-compatible alias for older tests/callers."""
    return _chain_jobs(job_queue, chain_id, project_id=project_id)


def _graph_snapshot(  # type: ignore[no-untyped-def]
    settings, *, limit_nodes: int = 200, project_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Pull the top-weighted slice of the graph for a static SVG view.

    We intentionally cap at `limit_nodes` — a full graph dump quickly
    swamps the browser. The slice is: the N highest-evidence nodes
    (by incident active_weight) plus every edge between members.
    Returns `(nodes, edges, kind_counts)`.
    """
    all_projects = _is_all_projects(project_id)
    node_where = "" if all_projects else "WHERE n.project_id = %s"
    nodes_params: tuple[Any, ...] = (
        (int(limit_nodes),)
        if all_projects
        else (project_id, int(limit_nodes))
    )
    nodes_sql = f"""
    WITH weighted AS (
      SELECT n.project_id, n.id, n.kind, n.attrs,
             coalesce(sum(e.active_weight), 0.0) AS score
        FROM agentcore_graph_nodes n
        LEFT JOIN agentcore_graph_edges e
          ON (e.source = n.id OR e.target = n.id)
         AND e.project_id = n.project_id
       {node_where}
       GROUP BY n.project_id, n.id, n.kind, n.attrs
    )
    SELECT project_id, id, kind, attrs, score
      FROM weighted
  ORDER BY score DESC, project_id, id
     LIMIT %s
    """
    edges_sql = """
    SELECT project_id, source, target, relation, active_weight
      FROM agentcore_graph_edges
     WHERE source = ANY(%s)
       AND target = ANY(%s)
       AND project_id = ANY(%s)
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    kinds: dict[str, int] = {}
    try:
        with (
            pg_conn(settings) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(nodes_sql, nodes_params)
            selected_projects: set[str] = set()
            db_ids: set[str] = set()
            for row_project, node_id, kind, attrs, score in cur.fetchall() or []:
                row_project = str(row_project)
                node_id = str(node_id)
                selected_projects.add(row_project)
                db_ids.add(node_id)
                ui_id = f"{row_project}:{node_id}" if all_projects else node_id
                node_attrs = attrs if isinstance(attrs, dict) else {}
                if all_projects:
                    node_attrs = {**node_attrs, "project_id": row_project}
                nodes.append(
                    {
                        "id": ui_id,
                        "kind": str(kind),
                        "label": (
                            f"{row_project}/{node_id.split(':', 1)[-1]}"[:40]
                            if all_projects
                            else node_id.split(":", 1)[-1][:40]
                        ),
                        "score": float(score or 0.0),
                        "attrs": node_attrs,
                    }
                )
                kinds[str(kind)] = kinds.get(str(kind), 0) + 1

            if nodes:
                ids = list(db_ids)
                cur.execute(edges_sql, (ids, ids, list(selected_projects)))
                for row_project, source, target, relation, w in cur.fetchall() or []:
                    row_project = str(row_project)
                    edges.append(
                        {
                            "source": (
                                f"{row_project}:{source}" if all_projects else str(source)
                            ),
                            "target": (
                                f"{row_project}:{target}" if all_projects else str(target)
                            ),
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
    if not job_queue.is_persistent:
        return []

    pid = project_id or settings.project_name
    out: dict[str, dict[str, Any]] = {}
    all_projects = _is_all_projects(pid)

    with (
        pg_conn(job_queue.settings) as conn,
        conn.cursor() as cur,
    ):
        # Source 1: HTTP-driven chains via idempotency cache.
        idem_where = "" if all_projects else "project_id = %s AND"
        idem_params: tuple[Any, ...] = (int(limit),) if all_projects else (pid, int(limit))
        cur.execute(
            f"""
            SELECT project_id, key, payload, created_at
              FROM agentcore_idempotency
             WHERE {idem_where} scope = 'chain'
          ORDER BY created_at DESC
             LIMIT %s
            """,
            idem_params,
        )
        for row_project, key, payload, created_at in cur.fetchall() or []:
            row_project = str(row_project)
            body = payload if isinstance(payload, dict) else {}
            out_key = f"{row_project}:{key}"
            out[out_key] = {
                "chain_id": str(key),
                "project_id": row_project,
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
        graph_where = "" if all_projects else "WHERE project_id = %s"
        node_where = "" if all_projects else "n.project_id = %s AND"
        graph_params: tuple[Any, ...] = (
            (int(limit),)
            if all_projects
            else (pid, pid, int(limit))
        )
        cur.execute(
            f"""
            SELECT
                n.project_id,
                replace(n.id, 'task:', '') AS chain_id,
                n.labels,
                n.last_seen,
                COALESCE(h.hop_count, 0) AS hop_count
              FROM agentcore_graph_nodes n
              LEFT JOIN (
                SELECT project_id, target, count(*) AS hop_count
                  FROM agentcore_graph_edges
                 {graph_where}
                   {"AND" if not all_projects else "WHERE"} relation = 'worked_on'
                 GROUP BY project_id, target
              ) AS h ON h.project_id = n.project_id AND h.target = n.id
             WHERE {node_where} n.kind = 'task'
          ORDER BY n.last_seen DESC
             LIMIT %s
            """,
            graph_params,
        )
        for row_project, chain_id, labels, last_seen, hop_count in cur.fetchall() or []:
            row_project = str(row_project)
            cid = str(chain_id)
            out_key = f"{row_project}:{cid}"
            if out_key in out:
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
            out[out_key] = {
                "chain_id": cid,
                "project_id": row_project,
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
