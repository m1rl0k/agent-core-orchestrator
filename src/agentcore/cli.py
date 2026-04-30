"""Typer-based CLI entrypoint for `agentcore`."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import typer
import uvicorn
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.status import Status
from rich.syntax import Syntax
from rich.table import Table

from agentcore.adapters.claude_code import (
    link as claude_link,
)
from agentcore.adapters.claude_code import (
    link_copilot_wiki,
    link_cursor_wiki,
)
from agentcore.adapters.claude_code import (
    link_wiki as link_claude_wiki,
)
from agentcore.adapters.graphify import GraphifyAdapter
from agentcore.capabilities import detect_capabilities
from agentcore.contracts.envelopes import Handoff, new_task_id
from agentcore.host import detect_host, render_install_hint
from agentcore.language import detect_languages, probe_lsps
from agentcore.llm.router import ChatMessage, LLMRouter
from agentcore.logging_setup import configure_logging
from agentcore.orchestrator.runtime import Runtime, reset_trace_project, set_trace_project
from agentcore.orchestrator.traces import TraceEvent, TraceLog
from agentcore.settings import get_settings
from agentcore.spec.loader import AgentRegistry

app = typer.Typer(no_args_is_help=True, add_completion=False, help="agent-core-orchestrator CLI")
console = Console()


def _middle_truncate(s: str, width: int) -> str:
    """Shrink `s` to <= `width` chars by collapsing the middle with `…`.
    Keeps the meaningful prefix (project root) and the meaningful
    suffix (filename) visible — better than tail-truncate for paths."""
    if len(s) <= width or width < 5:
        return s
    head = (width - 1) // 2
    tail = width - 1 - head
    return f"{s[:head]}…{s[-tail:]}"


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@app.command()
def doctor(
    repo: Path = typer.Option(Path("."), help="Repo to scan for languages"),
) -> None:
    """Show host, capability, language/LSP, and registry status."""
    settings = get_settings()
    configure_logging(settings.log_level)
    host = detect_host()

    console.rule("[bold]host")
    table = Table(show_header=False)
    table.add_row("os", host.os)
    table.add_row("arch", host.arch)
    table.add_row("shell", host.shell)
    table.add_row("python", host.python_version)
    console.print(table)

    console.rule("[bold]capabilities")
    cap_table = Table("name", "status", "detail")
    for name, cap in detect_capabilities(settings).items():
        if cap.status == "missing":
            detail = render_install_hint(cap.install_hint, host)
        elif cap.status == "unauthed":
            detail = cap.auth_hint
        elif cap.status == "off":
            detail = "set AGENTCORE_ENABLE_" + name.upper() + "=true"
        else:
            detail = (cap.detail.splitlines() or [""])[0]
        cap_table.add_row(name, cap.status, detail)
    console.print(cap_table)

    console.rule("[bold]languages & LSPs")
    profile = detect_languages(repo)
    if profile.primary is None:
        console.print(f"[yellow]no source files detected under {repo.resolve()}[/yellow]")
    else:
        lang_table = Table("language", "files", "lsp", "status")
        for lang, count in sorted(profile.counts.items(), key=lambda x: -x[1]):
            statuses = probe_lsps([lang])
            if not statuses:
                lang_table.add_row(lang, str(count), "—", "no recommendation")
                continue
            s = statuses[0]
            if s.available:
                lang_table.add_row(lang, str(count), s.binary or "", "[green]ready[/green]")
            else:
                lang_table.add_row(lang, str(count), "[dim]none[/dim]",
                                   f"[yellow]install[/yellow]: {s.install_hint}")
        console.print(lang_table)

    console.rule("[bold]agents")
    registry = AgentRegistry()
    registry.load_dir(settings.agents_dir)
    a_table = Table("name", "provider/model", "accepts_from", "delegates_to", "source")
    for spec in registry.all():
        a_table.add_row(
            spec.name,
            f"{spec.llm.provider}/{spec.llm.model}",
            ", ".join(spec.contract.accepts_handoff_from),
            ", ".join(spec.contract.delegates_to),
            _middle_truncate(spec.source_path or "", 60),
        )
    console.print(a_table)
    if registry.errors():
        console.rule("[bold red]parse errors")
        for path, err in registry.errors().items():
            console.print(f"[red]{path}[/red]: {err}")


# ---------------------------------------------------------------------------
# up / down — bring the docker-compose stack online for local dev
# ---------------------------------------------------------------------------


@app.command()
def up(
    service: str = typer.Argument(
        "postgres",
        help="Compose service to start (default: postgres). Pass `all` for everything.",
    ),
) -> None:
    """Bring up local infra (Postgres + pgvector by default)."""
    settings = get_settings()
    from agentcore.state.bootstrap import (
        _compose_file_path,
        docker_available,
        ensure_postgres,
    )

    if not docker_available():
        console.print("[red]docker not found on PATH[/red]; install Docker Desktop or equivalent.")
        raise typer.Exit(code=2)
    if service == "postgres":
        if ensure_postgres(settings):
            console.print(f"[green]postgres ready[/green] at {settings.pg_host}:{settings.pg_port}")
            return
        console.print("[red]postgres failed to start[/red] — check `docker compose logs postgres`.")
        raise typer.Exit(code=3)
    # generic "all"
    compose = _compose_file_path()
    if compose is None:
        console.print("[red]no docker-compose.yml found[/red] in cwd or any parent.")
        raise typer.Exit(code=2)
    rc = subprocess.run(
        ["docker", "compose", "-f", str(compose), "up", "-d"],
        check=False,
    ).returncode
    if rc != 0:
        raise typer.Exit(code=rc)
    console.print("[green]compose stack up[/green]")


@app.command()
def down() -> None:
    """Stop the local docker-compose stack."""
    from agentcore.state.bootstrap import _compose_file_path, docker_available

    if not docker_available():
        console.print("[red]docker not found on PATH[/red]")
        raise typer.Exit(code=2)
    compose = _compose_file_path()
    if compose is None:
        console.print("[red]no docker-compose.yml found[/red]")
        raise typer.Exit(code=2)
    rc = subprocess.run(
        ["docker", "compose", "-f", str(compose), "down"], check=False
    ).returncode
    raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


@app.command()
def agents() -> None:
    """List loaded agent specs as JSON."""
    settings = get_settings()
    registry = AgentRegistry()
    registry.load_dir(settings.agents_dir)
    out = [s.model_dump(exclude={"system_prompt"}) for s in registry.all()]
    console.print_json(json.dumps(out))


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


@app.command()
def index(
    repo: Path = typer.Argument(Path("."), help="Repo root to index"),
    collection: str = typer.Option("code", help="pgvector collection name"),
    init_schema: bool = typer.Option(True, help="Run DDL if needed"),
) -> None:
    """Index a repository into pgvector for retrieval."""
    settings = get_settings()
    configure_logging(settings.log_level)

    asyncio.run(_index_async(repo, collection, init_schema, settings))


async def _index_async(repo: Path, collection: str, init_schema: bool, settings) -> None:  # type: ignore[no-untyped-def]
    from agentcore.memory.code_index import CodeIndex
    from agentcore.memory.embed import Embedder
    from agentcore.memory.vector import VectorStore
    from agentcore.state.bootstrap import ensure_postgres

    if not ensure_postgres(settings):
        console.print(
            "[red]postgres unreachable[/red] — `agentcore index` needs pgvector.\n"
            "  start it with [cyan]docker compose up -d postgres[/cyan]."
        )
        raise typer.Exit(code=2)
    store = VectorStore(settings)
    if init_schema:
        store.init_schema()
    idx = CodeIndex(repo)
    symbols = idx.index()
    console.print(f"[green]found[/green] {len(symbols)} symbols under {repo.resolve()}")
    if not symbols:
        return

    # Auto-warm: first call would otherwise silently download weights.
    from agentcore.models import pull_embedder

    pull = pull_embedder(settings)
    if not pull.ok:
        console.print(f"[red]embedder unavailable: {pull.detail}[/red]")
        raise typer.Exit(code=1)

    embedder = Embedder(settings)
    try:
        # Batch in groups of 64 to keep payloads reasonable.
        batch = 64
        upserted = 0
        for i in range(0, len(symbols), batch):
            chunk = symbols[i : i + batch]
            embs = await embedder.embed([s.text for s in chunk])
            items = [
                (s.ref, s.text, {"path": s.path, "kind": s.kind, "name": s.name}, e)
                for s, e in zip(chunk, embs, strict=True)
            ]
            upserted += store.upsert(collection, items)
            console.print(f"  upserted {upserted}/{len(symbols)}")
    finally:
        await embedder.aclose()


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


@app.command()
def plan(
    brief: str = typer.Argument(..., help="What you want done"),
    chain: bool = typer.Option(True, help="Auto-chain Architect → Developer → QA"),
    max_hops: int = typer.Option(6, help="Cap on chained hops"),
    review: bool = typer.Option(
        True,
        "--review/--no-review",
        help="After the chain completes, ask every role for a ReviewVerdict. "
        "If any rejects, route back (architect|developer|qa) with the "
        "blockers and re-run the relevant slice. Iterates up to "
        "`--max-review-loops` times. Default ON.",
    ),
    apply: bool = typer.Option(
        True,
        "--apply/--no-apply",
        help="Actually apply the developer's unified diffs to disk via "
        "`git apply` once approved. Default ON. Use `--no-apply` for a "
        "dry-run that stops at the verdicts.",
    ),
    pr: bool = typer.Option(
        False,
        "--pr",
        help="Open a GitHub PR after applying. Requires AGENTCORE_ENABLE_GITHUB=true "
        "and an authenticated `gh` on the host.",
    ),
    repo: Path = typer.Option(Path("."), help="Repo root for --apply / --pr"),
    max_review_loops: int = typer.Option(
        0,
        help="Cap on review iterations. Default 0 = unlimited (loop until "
        "every reviewer approves). Set to a positive int to enforce a "
        "ceiling, e.g. `--max-review-loops 5` for CI safety.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Suppress human-readable panels and emit a single JSON object "
        "to stdout when the chain completes. Use in CI / scripting.",
    ),
) -> None:
    """Run the role mesh end-to-end on this repo.

    Default workflow: chain → review round → if any reviewer rejects, route
    back to whichever role the rejecter pointed at (developer for patch-level,
    architect for plan-level, qa for test gaps) → re-chain → review again.
    Iterates until unanimous approval, then applies the diffs and optionally
    opens a PR. Use `--max-review-loops 0` for true loop-until-approved.
    """
    asyncio.run(_plan_async(
        brief,
        chain,
        max_hops,
        review=review,
        apply=apply,
        pr=pr,
        repo=repo,
        max_review_loops=max_review_loops,
        as_json=as_json,
    ))


async def _plan_async(
    brief: str,
    chain: bool,
    max_hops: int,
    *,
    review: bool = True,
    apply: bool = True,
    pr: bool = False,
    repo: Path = Path("."),
    max_review_loops: int = 0,
    as_json: bool = False,
) -> None:
    # JSON mode: suppress every rich panel along the way (set
    # `console.quiet = True`) and emit one machine-readable JSON
    # object to stdout at the end. Behaviour for human mode is
    # unchanged.
    prior_quiet = console.quiet
    if as_json:
        console.quiet = True
    try:
        await _plan_async_body(
            brief, chain, max_hops,
            review=review, apply=apply, pr=pr, repo=repo,
            max_review_loops=max_review_loops, as_json=as_json,
        )
    finally:
        console.quiet = prior_quiet


async def _plan_async_body(
    brief: str,
    chain: bool,
    max_hops: int,
    *,
    review: bool = True,
    apply: bool = True,
    pr: bool = False,
    repo: Path = Path("."),
    max_review_loops: int = 0,
    as_json: bool = False,
) -> None:
    from agentcore.memory import prf
    from agentcore.memory.graph import KnowledgeGraph

    from datetime import UTC, datetime as _dt
    settings = get_settings()
    configure_logging(settings.log_level)
    chain_started_at = _dt.now(UTC)

    # Make the repo path visible to runtime executors (e.g. the polyglot
    # `tests` runner needs to know which repo to clone into a worktree).
    # Setting an env var rather than threading the path through every
    # handoff keeps the contracts unchanged.
    repo_abs = repo.resolve()
    repo_abs.mkdir(parents=True, exist_ok=True)
    # Greenfield bootstrap: if the path isn't a git repo yet, init one
    # so `git worktree add` (used by the validation runner) works. The
    # chain may produce diffs that scaffold a brand-new project — we
    # want that to "just work" without the user pre-running `git init`.
    if not (repo_abs / ".git").is_dir():
        with contextlib.suppress(subprocess.CalledProcessError, FileNotFoundError):
            subprocess.run(
                ["git", "-C", str(repo_abs), "init", "--quiet"],
                check=True, capture_output=True,
            )
            # Need at least one commit for `git worktree add` to be happy.
            (repo_abs / ".gitkeep").touch()
            subprocess.run(
                ["git", "-C", str(repo_abs), "add", ".gitkeep"],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_abs), "commit",
                 "--quiet", "--allow-empty",
                 "-m", "agentcore: scaffold root"],
                check=True, capture_output=True,
            )
    os.environ["AGENTCORE_REPO_ROOT"] = str(repo_abs)

    registry = AgentRegistry()
    registry.load_dir(settings.agents_dir)
    if registry.get("architect") is None:
        console.print("[red]architect agent not loaded[/red]; check agents/")
        raise typer.Exit(code=1)

    router = LLMRouter(settings)
    # Mirror trace events to ~/.agentcore/traces/<chain-id>.jsonl so the
    # `agentcore tail <chain-id>` command can follow CLI-driven chains
    # without the orchestrator needing to be running.
    traces = TraceLog(
        disk_dir=Path.home() / ".agentcore" / "traces",
        settings=settings,
    )

    graph = KnowledgeGraph(settings=settings)
    try:
        graph.load()
    except Exception as exc:
        console.print(f"[yellow]graph degraded to memory-only ({exc})[/yellow]")
        graph = KnowledgeGraph()
        with contextlib.suppress(Exception):
            graph.load()
    graphify = (
        GraphifyAdapter(repo_root=settings.graphify_repo_root, enabled=True)
        if settings.enable_graphify
        else None
    )
    from agentcore.retrieval.factory import try_build_retriever

    retriever = try_build_retriever(settings, graph)
    if retriever is None:
        console.print(
            "[yellow]retriever offline (see structured logs); plans will run "
            "without semantic context[/yellow]"
        )

    runtime = Runtime(
        registry=registry, router=router, traces=traces,
        graph=graph, graphify=graphify, retriever=retriever,
    )

    # Pre-chain wiki bootstrap — when wiki is enabled and the project
    # has zero pages, seed module pages BEFORE the architect runs so
    # agents can pull project context from the retriever. Cold start
    # only; warm projects skip this and rely on the post-converge
    # incremental refresh. Best-effort: a wiki failure must not block
    # the chain.
    if settings.enable_wiki:
        try:
            storage_check, _, curator = _build_wiki_stack(settings, repo_abs)
            if not list(storage_check.walk()):
                console.print(
                    "[dim]wiki: seeding pages for project (cold start)…[/dim]"
                )
                seeded = await curator.seed_from_repo(repo_abs)
                if seeded:
                    console.print(
                        f"[dim]wiki: seeded {len(seeded)} page(s)[/dim]"
                    )
        except Exception as exc:
            console.print(f"[yellow]wiki bootstrap skipped:[/yellow] {exc}")

    state, task_id = await _safe_run_chain(runtime, brief, chain, max_hops)

    # Review loop — default ON. If any role rejects, route back to the
    # role best-suited to fix the blockers and re-chain. Iterates until
    # unanimous approval. `max_review_loops == 0` means unlimited; any
    # positive value is a safety cap.
    verdicts: list[dict] = []
    if review and state.get("developer_output"):
        loop_idx = 0
        while True:
            verdicts = await _run_review_round(router, registry, state)
            cap = "" if max_review_loops == 0 else f"/{max_review_loops}"
            console.rule(
                f"[bold]review round {loop_idx + 1}{cap}[/bold]",
                style="cyan",
            )
            traces.record(TraceEvent(
                task_id=task_id, step=loop_idx + 1, kind="review_round",
                actor="cli",
                detail={"round": loop_idx + 1, "verdicts": len(verdicts)},
            ))
            _render_verdicts_table(verdicts)
            for v in verdicts:
                traces.record(TraceEvent(
                    task_id=task_id, step=loop_idx + 1, kind="verdict",
                    actor=v.get("agent", "?"),
                    detail={
                        "approved": v.get("approved", False),
                        "comments": (v.get("comments") or "")[:200],
                        "blockers": v.get("blockers", [])[:5],
                        "route_back_to": v.get("route_back_to"),
                    },
                ))
            if all(v["approved"] for v in verdicts):
                console.print("[bold green]all roles approved[/bold green]")
                break
            if _environment_only_review_blockers(verdicts, state):
                console.print(
                    "[yellow]review blockers are environment-only and QA report is clean; "
                    "treating as converged[/yellow]"
                )
                traces.record(TraceEvent(
                    task_id=task_id, step=loop_idx + 1, kind="review_round",
                    actor="cli",
                    detail={"round": loop_idx + 1, "convergence_override": "environment_only"},
                ))
                for v in verdicts:
                    if not v.get("approved"):
                        v["approved"] = True
                        v["comments"] = (
                            (v.get("comments") or "")
                            + " Environment-only blocker overridden after clean QA report."
                        ).strip()
                        v["blockers"] = []
                break
            if max_review_loops > 0 and loop_idx + 1 >= max_review_loops:
                console.print(
                    f"[yellow]max review loops ({max_review_loops}) reached without "
                    "unanimous approval — not applying[/yellow]"
                )
                apply = False
                break
            blockers = [b for v in verdicts if not v["approved"] for b in v.get("blockers", [])]
            # Continuous improvement loop: any role can take the re-review.
            target = next(
                (v.get("route_back_to", "architect") for v in verdicts if not v["approved"]),
                "architect",
            )
            console.print(
                f"[yellow]routing back to {target}[/yellow] with "
                f"{len(blockers)} blocker(s)"
            )
            traces.record(TraceEvent(
                task_id=task_id, step=loop_idx + 1, kind="route_back",
                actor="cli",
                detail={"target": target, "blockers": len(blockers)},
            ))
            payload = _synthesize_handoff_payload(target, state, blockers, brief)
            state, task_id = await _safe_run_chain(
                runtime,
                _compose_revision_brief(brief, blockers, state),
                chain,
                max_hops,
                start_at=target,
                payload=payload,
                task_id=task_id,
                prior_state=state,
            )
            loop_idx += 1

    applied: list[str] = []
    if apply and state.get("developer_output"):
        dev = state["developer_output"]
        # Prefer structured file_ops over fragile unified diffs. Either
        # is acceptable; if both are present, file_ops wins.
        file_ops = dev.get("file_ops") or []
        diffs = dev.get("diffs") or []
        try:
            if file_ops:
                from agentcore.runtime.sandbox import apply_file_ops
                applied = apply_file_ops(repo.resolve(), file_ops)
            else:
                applied = _apply_diffs(repo, diffs)
        except Exception as exc:
            console.print(f"[red]apply failed:[/red] {exc}")
            applied = []
        if applied:
            console.print(f"[green]applied {len(applied)} file(s):[/green] " + ", ".join(applied))
        else:
            console.print("[yellow]no edits applied[/yellow]")
        traces.record(TraceEvent(
            task_id=task_id, step=99, kind="applied", actor="cli",
            detail={"count": len(applied), "files": applied[:20]},
        ))

    pr_url: str | None = None
    if pr and applied:
        pr_url = _open_github_pr(repo, brief, state, task_id)
        if pr_url:
            console.print(f"[green]PR opened:[/green] {pr_url}")
        traces.record(TraceEvent(
            task_id=task_id, step=99, kind="pr_opened", actor="cli",
            detail={"url": pr_url or "<none>"},
        ))

    # Chain-end PRF tagging.
    converged = False
    if state.get("qa_output") is not None:
        out = state["qa_output"]
        if out.get("failed"):
            graph.tag_relevance(task_id, prf.QA_FAILED,
                                score=float(len(out["failed"])),
                                reason="qa returned failures")
            graph.tag_task(task_id, prf.DEV_REVISED)
        else:
            graph.tag_relevance(task_id, prf.QA_PASSED)
            graph.tag_task(task_id, prf.POSITIVE)
            converged = True

    graph.save()

    # Persist a row in agentcore_jobs so the jobs dashboard shows
    # CLI-driven chains (HTTP /run already surfaces — CLI used to be
    # invisible). We INSERT in a terminal status (`done`/`failed`) so
    # the worker loop ignores it. Best-effort: skip silently when
    # Postgres is offline or psycopg isn't available.
    with contextlib.suppress(Exception):
        from datetime import UTC, datetime as _dt2

        from agentcore.state.db import pg_conn
        chain_finished_at = _dt2.now(UTC)
        chain_status = "done" if converged else "failed"
        chain_payload = {
            "chain_id": task_id,
            "brief": brief[:1000],
            "repo": str(repo_abs),
            "applied_count": len(applied),
            "applied_files": applied[:50],
            "review_rounds": len(verdicts),
            "pr_url": pr_url,
        }
        chain_error = None
        if not converged and verdicts:
            blockers: list[str] = []
            for v in verdicts:
                if not v.get("approved"):
                    for b in v.get("blockers") or []:
                        if isinstance(b, dict):
                            blockers.append(b.get("issue") or b.get("name") or "rejected")
                        else:
                            blockers.append(str(b))
            chain_error = "; ".join(blockers[:5])[:500] or "rejected by review"
        with (
            pg_conn(settings) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                """
                INSERT INTO agentcore_jobs
                  (project_id, kind, status, payload, idempotency_key,
                   priority, run_after, max_attempts, created_by,
                   started_at, finished_at, error)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, kind, idempotency_key)
                  WHERE idempotency_key IS NOT NULL
                DO UPDATE SET
                  status = EXCLUDED.status,
                  payload = EXCLUDED.payload,
                  finished_at = EXCLUDED.finished_at,
                  error = EXCLUDED.error
                """,
                (
                    settings.project_name, "chain.cli", chain_status,
                    json.dumps(chain_payload),
                    f"chain:{task_id}", 0, chain_started_at, 1, "cli.plan",
                    chain_started_at, chain_finished_at, chain_error,
                ),
            )

    # Living-wiki maintenance — runs inline against the chain's repo so
    # the wiki actually exists for projects that have never been seeded.
    # Cold start: walk the repo to seed module pages. Warm: revise pages
    # whose `sources[]` overlap the touched paths. Best-effort and
    # idempotent (storage skips unchanged pages); never blocks the
    # chain or surfaces a wiki failure as a chain failure.
    if converged and applied and settings.enable_wiki:
        try:
            storage, _index, curator = _build_wiki_stack(settings, repo_abs)
            existing_pages = list(storage.walk())
            if not existing_pages:
                console.print("[dim]wiki: seeding pages (first run)…[/dim]")
                written = await curator.seed_from_repo(repo_abs)
                mode = "seed"
            else:
                console.print("[dim]wiki: refreshing affected pages…[/dim]")
                written = await curator.incremental(applied, repo_abs)
                mode = "incremental"
            if written:
                console.print(
                    f"[dim]wiki: {mode} updated {len(written)} page(s)[/dim]"
                )
            traces.record(TraceEvent(
                task_id=task_id, step=99, kind="wiki_refresh", actor="cli",
                detail={
                    "mode": mode,
                    "changed_paths": applied[:20],
                    "written": written[:20],
                    "project_id": settings.project_name,
                },
            ))
        except Exception as exc:
            console.print(f"[yellow]wiki refresh skipped:[/yellow] {exc}")
            traces.record(TraceEvent(
                task_id=task_id, step=99, kind="wiki_refresh_failed",
                actor="cli", detail={"error": str(exc)[:300]},
            ))

    # Final result trace — gives `agentcore tail` a clean terminal
    # event so it stops following confidently regardless of in-process
    # vs over-HTTP execution.
    approved = (
        all(v.get("approved") for v in verdicts) if verdicts else None
    )
    traces.record(TraceEvent(
        task_id=task_id, step=99, kind="result", actor="cli",
        detail={
            "approved": approved,
            "applied_count": len(applied),
            "applied": applied[:20],
            "pr_url": pr_url,
        },
    ))

    # End-of-run summary so the operator gets a single scannable
    # outcome line with the chain id (for tailing / re-checking) and
    # the bottom-line outcome. Suppressed in --json mode.
    if not as_json:
        if approved is True and applied:
            tone, headline = "green", f"approved · applied {len(applied)} file(s)"
        elif approved is True:
            tone, headline = "yellow", "approved · no diffs to apply"
        elif approved is False:
            tone, headline = (
                "yellow", "blocked by review · diffs not applied",
            )
        else:
            tone, headline = "cyan", "chain finished"
        body_lines = [
            f"[bold {tone}]{headline}[/bold {tone}]",
            f"[dim]chain[/dim] [bold]{task_id}[/bold]",
        ]
        if applied:
            body_lines.append(
                f"[dim]applied[/dim] {', '.join(applied)}"
            )
        if pr_url:
            body_lines.append(f"[dim]pr[/dim] {pr_url}")
        body_lines.append(
            f"[dim]inspect:[/dim] [bold]agentcore tail {task_id}[/bold]"
        )
        console.print(
            Panel(
                "\n".join(body_lines),
                title="agentcore · result",
                border_style=tone,
                padding=(0, 1),
            )
        )

    # JSON mode: emit a single structured object to stdout. The outer
    # `_plan_async` wrapper handles console.quiet restore in its
    # finally block so any raise between here and there still cleans
    # up. Schema is intentionally flat for easy `jq`-ing in CI:
    # `agentcore plan ... --json | jq '.applied'`.
    if as_json:
        result = {
            "task_id": task_id,
            "approved": all(v.get("approved") for v in verdicts) if verdicts else None,
            "verdicts": verdicts,
            "applied": applied,
            "pr_url": pr_url,
            "qa": state.get("qa_output"),
            "plan": state.get("architect_output"),
            "patch": state.get("developer_output"),
        }
        import sys as _sys

        _sys.stdout.write(json.dumps(result, default=str, indent=2) + "\n")
        _sys.stdout.flush()


# ---------------------------------------------------------------------------
# Plan helpers — chain orchestration, review round, diff apply, PR
# ---------------------------------------------------------------------------


async def _safe_run_chain(
    runtime: Runtime,
    brief: str,
    chain: bool,
    max_hops: int,
    *,
    start_at: str = "architect",
    payload: dict | None = None,
    task_id: str | None = None,
    prior_state: dict | None = None,
) -> tuple[dict, str]:
    """Run a chain hop, but never let a mid-flight crash kill the
    review loop. Contract violations, SLA blowouts, or unparseable
    LLM output get logged to the trace and surface as a synthetic
    `developer_output` carrying the error so the next review round
    routes back through architect (or whoever) cleanly. The original
    chain id is preserved across the failure."""
    from agentcore.contracts.envelopes import ContractViolation
    from agentcore.orchestrator.runtime import HandoffRejected, SLAExceeded

    try:
        return await _run_chain(
            runtime, brief, chain, max_hops,
            start_at=start_at, payload=payload, task_id=task_id,
        )
    except (ContractViolation, HandoffRejected, SLAExceeded) as exc:
        tid = task_id or new_task_id()
        console.print(
            Panel(
                f"[bold red]chain hop failed[/bold red]: {type(exc).__name__}\n"
                f"[dim]{exc}[/dim]\n\n"
                f"Treating as a synthetic rejection — review round will "
                f"route back to address it.",
                title=f"agentcore · recoverable error · {start_at}",
                border_style="red",
                padding=(0, 1),
            )
        )
        # Carry forward whatever the prior chain produced so the
        # review loop has something to evaluate against. If nothing
        # exists yet, synthesize a minimal `developer_output` so the
        # `if state.get("developer_output")` review-loop guard
        # passes and the verdict round can do its job.
        state = dict(prior_state or {})
        if not state.get("developer_output"):
            state["developer_output"] = {
                "plan_summary": (
                    f"<chain failed at {start_at}>: {type(exc).__name__}: "
                    f"{str(exc)[:300]}"
                ),
                "diffs": [],
                "notes": (
                    "The chain hop crashed before producing a clean patch. "
                    "Reviewers should treat this as a hard rejection and "
                    "route back to architect for a re-plan that yields "
                    "a more compact diff."
                ),
            }
        return state, tid


async def _run_chain(
    runtime: Runtime,
    brief: str,
    chain: bool,
    max_hops: int,
    *,
    start_at: str = "architect",
    payload: dict | None = None,
    task_id: str | None = None,
) -> tuple[dict, str]:
    """Drive the role mesh from `start_at`. Captures each role's output by name.

    Pretty output: rich Live status while each agent is thinking, Panel +
    syntax-highlighted JSON for each completed hop, and inline Syntax for any
    unified diffs in the developer's payload.

    `from_agent` on the synthesized handoff is auto-resolved against the
    target's contract: most roles only accept handoffs from specific
    upstream roles, so a route-back to e.g. 'developer' must come from
    'architect', not 'user' (else the runtime rejects it).

    `payload` overrides the default `{"brief": brief}` — used by the
    review loop to feed the right upstream output (TechnicalPlan,
    ImplementationPatch, etc.) when re-entering mid-chain.
    """
    spec = runtime.registry.get(start_at)
    accepts = list(getattr(spec.contract, "accepts_handoff_from", [])) if spec else []
    from_agent = "user" if "user" in accepts or not accepts else accepts[0]
    handoff = Handoff(
        task_id=task_id or new_task_id(),
        from_agent=from_agent,
        to_agent=start_at,
        payload=payload if payload is not None else {"brief": brief},
    )
    # Up-front banner — shows the chain id prominently so the operator
    # can tail it from another shell, plus the brief and entry role.
    console.print(
        Panel(
            f"[bold cyan]chain[/bold cyan]  [bold]{handoff.task_id}[/bold]\n"
            f"[dim]follow:[/dim] [bold]agentcore tail {handoff.task_id}[/bold]\n"
            f"[bold cyan]brief[/bold cyan]\n{brief}",
            title=f"agentcore · starting at [bold]{start_at}[/bold]",
            border_style="cyan",
            padding=(0, 1),
        )
    )
    state: dict = {}
    current: Handoff | None = handoff
    for _ in range(max_hops):
        if current is None:
            break
        spinner = Status(
            f"[bold yellow]{current.to_agent}[/bold yellow] thinking…  (step {current.step})",
            console=console,
            spinner="dots",
        )
        token = set_trace_project(get_settings().project_name)
        try:
            with spinner:
                outcome, nxt = await runtime.execute(current)
        finally:
            reset_trace_project(token)
        state[f"{outcome.agent}_output"] = outcome.output
        _print_outcome(outcome)
        if not (chain and nxt):
            break
        current = nxt
    return state, handoff.task_id


def _print_outcome(outcome) -> None:  # type: ignore[no-untyped-def]
    """Render one chain hop: JSON panel + any unified diffs."""
    try:
        body = Syntax(
            json.dumps(outcome.output, indent=2),
            "json",
            theme="ansi_dark",
            line_numbers=False,
            word_wrap=True,
        )
    except (TypeError, ValueError):
        body = Markdown(str(outcome.output)[:4000])
    border = "green" if outcome.status in ("ok", "delegated") else "yellow"
    console.print(
        Panel(
            body,
            title=f"[bold]{outcome.agent}[/bold] · {outcome.status}",
            border_style=border,
            padding=(0, 1),
        )
    )
    for d in outcome.output.get("diffs", []) or []:
        if isinstance(d, dict) and d.get("unified_diff"):
            console.print(
                Panel(
                    Syntax(d["unified_diff"], "diff", theme="ansi_dark"),
                    title=f"diff · {d.get('path', '?')}",
                    border_style="blue",
                    padding=(0, 1),
                )
            )


async def _run_review_round(
    router,  # type: ignore[no-untyped-def]
    registry,  # type: ignore[no-untyped-def]
    state: dict,
) -> list[dict]:
    """Ask every loaded SDLC role for a ReviewVerdict on the current state.

    Each agent's persona shapes its verdict — architect on minimality,
    developer on patch correctness, qa on test coverage, ops on shipping
    safety. Returns one dict per agent (parallel of ReviewVerdict).
    """
    payload_summary = json.dumps(
        {
            "plan": state.get("architect_output"),
            "patch": state.get("developer_output"),
            "qa_report": state.get("qa_output"),
        },
        default=str,
    )[:8000]

    # Exclude the producer of the artifact under review. The chain's
    # primary artifact is the developer's patch; developer voting on
    # its own patch is a logical contradiction — if it knew the patch
    # was broken, it wouldn't have produced it. The reviewer panel is
    # its peers: architect (plan-level), qa (tests), ops (ship).
    producer = "developer" if state.get("developer_output") else None
    review_roles = [
        r for r in ("architect", "developer", "qa", "ops")
        if registry.get(r) and r != producer
    ]
    verdicts: list[dict] = []
    for role in review_roles:
        spec = registry.get(role)
        if spec is None:
            continue
        # Spinner so the human sees we're not stuck while the LLM thinks.
        spinner = Status(
            f"[bold yellow]{role}[/bold yellow] reviewing…",
            console=console,
            spinner="dots",
        )
        # QA-specific hard rule: never approve with failing tests. Other
        # reviewers may use judgement, but QA is the ground-truth role —
        # if pytest/equivalent reported a non-zero exit code, that's a
        # broken patch and approval is off the table.
        qa_hard_rule = (
            "\n\n## QA HARD RULE — overrides bias-toward-shipping\n"
            "Look at `qa_report.test_run.exit_code` and "
            "`qa_report.failed[]`. If exit_code != 0 OR `failed[]` is "
            "non-empty, you MUST set approved=false and list the "
            "failing test(s) as blockers. Real test failures are NEVER "
            "nits — they're hard ship-blockers. The 'material blocker' "
            "bar is automatically met when the runner says so.\n"
            "Conversely: if exit_code == 0 AND failed[] is empty AND "
            "the diff addresses the brief, you almost certainly "
            "should approve."
            if role == "qa" else ""
        )
        sys = (
            f"You are the {spec.soul.role}. Voice: {spec.soul.voice}. "
            f"Values: {', '.join(spec.soul.values)}.\n\n"
            "You are casting ONE vote on whether the proposed change is "
            "good enough to ship. Respond with a single JSON object:\n"
            "{\n"
            f'  "agent": "{role}",\n'
            '  "approved": <bool>,\n'
            '  "blockers": [<material reasons it cannot ship; empty if approved>],\n'
            '  "comments": "<one-sentence summary>",\n'
            '  "route_back_to": "<architect|developer|qa|ops>"\n'
            "}\n\n"
            "## Approval bar — bias toward shipping\n"
            "Approve if the change is correct, in scope, and addresses "
            "the brief. The bar is 'I would ship this', NOT 'I would "
            "ship this if every nit were fixed'. You're voting on "
            "merge-readiness, not on whether you'd nominate it for an "
            "engineering award.\n\n"
            "## Reject only on MATERIAL blockers\n"
            "A material blocker is something that would actually break "
            "production: bug not fixed, test that doesn't run, missing "
            "required scaffolding, incorrect contract. Style/naming "
            "preferences, additional edge cases you'd nice-to-have, or "
            "'could be more thorough' are NOT material — record them "
            "in `comments` and approve. If you wouldn't open a sev2 "
            "ticket about it, it's not a blocker.\n\n"
            "## Honour prior rounds — convergence over churn\n"
            "If you can see prior blockers were addressed in good faith, "
            "approve. Do NOT raise a fresh laundry list of NEW concerns "
            "you didn't surface in earlier rounds — that's moving the "
            "goalposts. New blockers are legitimate ONLY if (a) the "
            "fix introduced them or (b) they would actually break ship.\n\n"
            "## Routing\n"
            "If you reject: set `route_back_to` to the role best "
            "positioned to address it — `developer` for patch-level "
            "(default), `architect` for plan-level, `qa` for test gaps."
            f"{qa_hard_rule}"
        )
        user = (
            "Here is the current chain state. Review and emit a verdict.\n\n"
            f"```json\n{payload_summary}\n```"
        )
        try:
            with spinner:
                resp = await router.complete(
                    [ChatMessage(role="system", content=sys),
                     ChatMessage(role="user", content=user)],
                    spec.llm,
                )
        except Exception as exc:
            verdicts.append({
                "agent": role,
                "approved": False,
                "blockers": [f"review call failed: {exc}"],
                "comments": "review aborted",
                "route_back_to": "architect",
            })
            continue
        # Parse the JSON object, lenient.
        match = re.search(r"\{.*\}", resp.text, re.DOTALL)
        if not match:
            verdicts.append({
                "agent": role,
                "approved": False,
                "blockers": [f"non-JSON review output: {resp.text[:200]!r}"],
                "comments": "could not parse",
                "route_back_to": "architect",
            })
            continue
        try:
            v = json.loads(match.group(0))
            v.setdefault("agent", role)
            v.setdefault("blockers", [])
            v.setdefault("comments", "")
            v.setdefault("route_back_to", "architect")
            verdicts.append(v)
        except json.JSONDecodeError as exc:
            verdicts.append({
                "agent": role,
                "approved": False,
                "blockers": [f"JSON decode failed: {exc}"],
                "comments": "",
                "route_back_to": "architect",
            })
    return verdicts


_ENV_BLOCKER_TERMS = (
    "pytest not installed",
    "no module named pytest",
    "pytest module not found",
    "test environment",
    "environment failure",
    "missing pytest",
    "test runner failed",
    "zero tests executed",
    "cannot execute test",
    "cannot verify",
)


def _environment_only_review_blockers(verdicts: list[dict], state: dict) -> bool:
    """Allow convergence when review LLMs only object to local test infra.

    Runtime QA already ran in the worktree before the review round. If that
    QA report has no failed cases, repeated review complaints about missing
    pytest/test environment are not actionable patch feedback and should not
    bounce the chain forever.
    """
    qa = state.get("qa_output") or {}
    if qa.get("failed"):
        return False
    rejected = [v for v in verdicts if not v.get("approved")]
    if not rejected:
        return False
    for verdict in rejected:
        text = " ".join(
            [
                str(verdict.get("comments") or ""),
                " ".join(str(b) for b in verdict.get("blockers", []) or []),
            ]
        ).lower()
        if not any(term in text for term in _ENV_BLOCKER_TERMS):
            return False
    return True


def _compose_revision_brief(
    original_brief: str, blockers: list[str], state: dict
) -> str:
    bullets = "\n".join(f"  - {b}" for b in blockers[:20])
    return (
        f"{original_brief}\n\n"
        "PRIOR ATTEMPT WAS REJECTED IN REVIEW. Address these blockers in the "
        "revised plan/patch:\n"
        f"{bullets}\n\n"
        "Keep the change minimal; do not introduce unrelated edits."
    )


def _synthesize_handoff_payload(
    target: str, state: dict, blockers: list[str], brief: str
) -> dict:
    """Build a contract-shaped payload for re-entering the chain at `target`.

    Each role expects a specific input shape (architect → brief, developer →
    TechnicalPlan, qa → ImplementationPatch, ops → QAReport). When a review
    routes back mid-chain, we synthesize that shape from the captured state
    and prepend the blockers as a revision instruction in the role's primary
    text field — so the target sees both the upstream artifact AND what
    needs fixing.
    """
    revision_brief = _compose_revision_brief(brief, blockers, state)
    if target == "architect":
        return {"brief": revision_brief}
    if target == "developer":
        plan = state.get("architect_output") or {}
        return {
            "summary": revision_brief + "\n\nORIGINAL PLAN: "
            + str(plan.get("summary", "")),
            "files_to_change": plan.get("files_to_change", []),
            "risks": plan.get("risks", []),
            "test_strategy": plan.get("test_strategy", ""),
        }
    if target == "qa":
        patch = state.get("developer_output") or {}
        return {
            "plan_summary": revision_brief + "\n\nPRIOR PATCH: "
            + str(patch.get("plan_summary", "")),
            "diffs": patch.get("diffs", []),
            "notes": patch.get("notes", ""),
        }
    if target == "ops":
        qa = state.get("qa_output") or {}
        return {
            "suite_summary": revision_brief + "\n\nQA REPORT: "
            + str(qa.get("suite_summary", "")),
            "passed": qa.get("passed", []),
            "failed": qa.get("failed", []),
        }
    return {"brief": revision_brief}


def _apply_diffs(repo: Path, diffs: list[dict]) -> list[str]:
    """Apply FileDiff objects to the real repo using `git apply`.

    Invalid diffs fail explicitly. We never reconstruct files from `+`
    lines because that corrupts source when context is wrong.
    """
    repo = Path(repo).resolve()
    patches: list[tuple[str, str]] = []
    patch_paths: list[str] = []
    for d in diffs:
        path = d.get("path") if isinstance(d, dict) else None
        diff_text = d.get("unified_diff") if isinstance(d, dict) else None
        if path and diff_text:
            patches.append((str(path), str(diff_text)))
    if not patches:
        return []

    try:
        for _, diff_text in patches:
            with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as tmp:
                tmp.write(diff_text)
                patch_paths.append(tmp.name)

        check = subprocess.run(
            ["git", "-C", str(repo), "apply", "--recount", "--check", *patch_paths],
            capture_output=True,
            text=True,
        )
        if check.returncode != 0:
            console.print(
                "[red]diff apply check failed; no files modified:[/red] "
                + (check.stderr or check.stdout or "git apply --check failed").strip()
            )
            return []

        applied = subprocess.run(
            ["git", "-C", str(repo), "apply", "--recount", *patch_paths],
            capture_output=True,
            text=True,
        )
        if applied.returncode != 0:
            console.print(
                "[red]diff apply failed; no fallback overwrite attempted:[/red] "
                + (applied.stderr or applied.stdout or "git apply failed").strip()
            )
            return []
        return [path for path, _ in patches]
    finally:
        for patch_path in patch_paths:
            with contextlib.suppress(OSError):
                Path(patch_path).unlink()


def _open_github_pr(
    repo: Path, brief: str, state: dict, task_id: str
) -> str | None:
    """Branch + commit + open PR via `gh`. Returns the URL on success.

    Skipped silently if AGENTCORE_ENABLE_GITHUB is false or the gh capability
    isn't ready — caller decides whether to surface that.
    """
    settings = get_settings()
    if not settings.enable_github:
        console.print("[yellow]--pr requested but AGENTCORE_ENABLE_GITHUB=false[/yellow]")
        return None
    repo = Path(repo).resolve()
    branch = f"agentcore/plan/{task_id[:8]}"
    title = brief.splitlines()[0][:70]
    body_lines = [
        f"Generated by agentcore (task `{task_id}`).",
        "",
        "## Plan",
        f"```json\n{json.dumps(state.get('architect_output'), indent=2, default=str)[:2000]}\n```",
        "",
        "## QA",
        f"```json\n{json.dumps(state.get('qa_output'), indent=2, default=str)[:2000]}\n```",
    ]
    body = "\n".join(body_lines)
    try:
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-b", branch],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", title],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "push", "-u", "origin", branch],
            check=True, capture_output=True, text=True,
        )
        result = subprocess.run(
            ["gh", "pr", "create", "--title", title, "--body", body, "--draft"],
            check=True, capture_output=True, text=True, cwd=str(repo),
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        console.print(f"[red]PR open failed:[/red] {exc.stderr or exc}")
        return None


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@app.command()
def tail(
    chain_id: str = typer.Argument(..., help="Chain / task id to follow"),
    url: str = typer.Option(
        "", help="Orchestrator URL (default: built from AGENTCORE_HOST/PORT)"
    ),
    interval: float = typer.Option(
        1.0, help="Poll interval in seconds"
    ),
) -> None:
    """Stream trace events for an in-flight chain in real time.

    Tries two sources, in order:

      1. The orchestrator's HTTP API (`/tasks/{id}/trace` +
         `/chains/{id}`) — works for chains submitted via `POST /run`
         to a running `agentcore serve`.
      2. The local on-disk trace at `~/.agentcore/traces/{id}.jsonl` —
         written by the CLI's in-process runs (`agentcore plan`). This
         lets you tail a CLI run from another shell without the
         orchestrator running.

    Exits when the chain reaches a terminal status, or when the JSONL
    trace stops growing for `--interval × 5` and the last event was
    an outcome.
    """
    import time

    import httpx

    settings = get_settings()
    base = url or f"http://{settings.host}:{settings.port}"
    headers = {"X-Project-Id": settings.project_name}
    if settings.api_token:
        headers["Authorization"] = f"Bearer {settings.api_token}"

    local_path = Path.home() / ".agentcore" / "traces" / f"{chain_id}.jsonl"
    if local_path.exists():
        console.print(
            Panel(
                f"[bold cyan]chain[/bold cyan] {chain_id}\n"
                f"[bold cyan]source[/bold cyan] local file [dim]({local_path})[/dim]",
                title="agentcore · tail",
                border_style="cyan",
                padding=(0, 1),
            )
        )
        _tail_local(local_path, interval=interval)
        return

    console.print(
        Panel(
            f"[bold cyan]chain[/bold cyan] {chain_id}\n"
            f"[bold cyan]source[/bold cyan] http {base}",
            title="agentcore · tail",
            border_style="cyan",
            padding=(0, 1),
        )
    )

    seen = 0
    warned = False
    try:
        with httpx.Client(timeout=30.0, headers=headers) as client:
            while True:
                try:
                    tr = client.get(f"{base}/tasks/{chain_id}/trace")
                    if tr.status_code == 200:
                        events = tr.json().get("events", [])
                        for evt in events[seen:]:
                            _render_trace_event(evt)
                        seen = len(events)
                    cr = client.get(f"{base}/chains/{chain_id}")
                    status = (
                        cr.json().get("status", "running")
                        if cr.status_code == 200
                        else "running"
                    )
                except httpx.RequestError as exc:
                    # If the orchestrator never comes up but a local
                    # trace appears, switch over.
                    if local_path.exists():
                        console.print(
                            "[yellow]http unavailable; switching to local trace…[/yellow]"
                        )
                        _tail_local(local_path, interval=interval)
                        return
                    if not warned:
                        console.print(
                            f"[yellow]http unavailable[/yellow] ({exc}); "
                            "no local trace yet — start `agentcore serve` "
                            "or run `agentcore plan` to generate one"
                        )
                        warned = True
                    status = "running"

                if status in ("done", "failed", "cancelled"):
                    border = (
                        "green" if status == "done"
                        else "red" if status == "failed"
                        else "yellow"
                    )
                    console.rule(
                        f"[bold {border}]chain {status}[/bold {border}]"
                    )
                    break
                time.sleep(interval)
    except KeyboardInterrupt:
        console.print("[yellow]tail stopped[/yellow]")


def _tail_local(path: Path, *, interval: float) -> None:
    """Follow a `~/.agentcore/traces/<id>.jsonl` file. Renders each JSON
    line as a colour-coded event. Exits when the file stops growing
    after the last event is `outcome` for the final agent, or on
    Ctrl-C."""
    import time

    pos = 0
    idle_ticks = 0
    last_kind = ""
    try:
        while True:
            try:
                with path.open("r", encoding="utf-8") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
            except FileNotFoundError:
                console.print("[yellow]trace file gone[/yellow]")
                return
            if chunk:
                idle_ticks = 0
                for line in chunk.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    _render_trace_event(evt)
                    last_kind = evt.get("kind", "")
                    # `result` is the canonical terminal CLI event.
                    if last_kind == "result":
                        d = evt.get("detail") or {}
                        approved = d.get("approved")
                        applied_count = d.get("applied_count", 0)
                        if approved is True and applied_count:
                            tone, headline = (
                                "green",
                                f"approved · applied {applied_count} file(s)",
                            )
                        elif approved is True:
                            tone, headline = "yellow", "approved · no diffs"
                        elif approved is False:
                            tone, headline = (
                                "yellow",
                                "blocked by review",
                            )
                        else:
                            tone, headline = "cyan", "finished"
                        console.rule(
                            f"[bold {tone}]chain {headline}[/bold {tone}]"
                        )
                        return
            else:
                idle_ticks += 1
                # Fallback: 10 quiet polls + last event was outcome/error
                # → assume terminal even if no `result` was recorded.
                if idle_ticks >= 10 and last_kind in ("outcome", "error", "applied"):
                    console.rule("[bold green]trace idle (chain finished)[/bold green]")
                    return
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("[yellow]tail stopped[/yellow]")


def _render_verdicts_table(verdicts: list[dict]) -> None:
    """Render review-round verdicts as a scannable Rich Table.

    Columns: agent, verdict (✓ / ✗), comment (truncated), # blockers,
    route_back_to (only when rejected). Then list the actual blockers
    underneath each rejection — indented bullet form so the eye can
    scan a single row to see WHO rejected, WHY at a glance, and HOW
    they want it fixed.
    """
    table = Table(
        show_header=True, header_style="bold dim",
        padding=(0, 1), box=None,
    )
    table.add_column("agent", style="bold", min_width=10)
    table.add_column("verdict", justify="center", min_width=8)
    table.add_column("comment", overflow="fold")
    table.add_column("blockers", justify="right", style="dim")
    table.add_column("→", style="dim", min_width=10)
    for v in verdicts:
        approved = bool(v.get("approved"))
        badge = "[green]✓ approved[/green]" if approved else "[red]✗ rejected[/red]"
        comment = (v.get("comments") or "")[:90]
        n_block = len(v.get("blockers") or [])
        rb = v.get("route_back_to") or ""
        table.add_row(
            v.get("agent", "?"), badge, comment,
            str(n_block) if n_block else "",
            "" if approved else rb,
        )
    console.print(table)
    # Detail rows for rejections — concrete blockers under each rejecter.
    for v in verdicts:
        if v.get("approved") or not v.get("blockers"):
            continue
        console.print(f"  [dim]{v.get('agent','?')} blockers:[/dim]")
        for b in v["blockers"][:5]:
            console.print(f"    [red]✗[/red] {b[:200]}")


_TRACE_PALETTE = {
    "handoff_in":   ("cyan",    "→"),
    "llm_call":     ("yellow",  "·"),
    "llm_retry":    ("yellow",  "↻"),
    "outcome":      ("green",   "✓"),
    "error":        ("red",     "✗"),
    "batch_split":  ("blue",    "⌥"),
    "batch_chunk":  ("blue",    "·"),
    "executor":     ("magenta", "▶"),
    "discovery":    ("magenta", "🔍"),
    "review_round": ("cyan",    "⟳"),
    "verdict":      ("white",   "·"),
    "route_back":   ("yellow",  "↩"),
    "applied":      ("green",   "✎"),
    "pr_opened":    ("green",   "⇡"),
    "result":       ("green",   "★"),
}


def _render_trace_event(evt: dict) -> None:
    """Color-code one trace event for the tail stream.

    Each event renders on a single line with a coloured glyph + kind +
    one human-readable summary, instead of dumping the raw detail dict.
    Falls back to the generic dict format for unknown kinds.
    """
    kind = evt.get("kind", "?")
    actor = evt.get("actor", "?")
    step = evt.get("step", "?")
    detail = evt.get("detail") or {}
    color, glyph = _TRACE_PALETTE.get(kind, ("white", "·"))

    summary = _summarise_event(kind, detail)
    head = (
        f"[{color}]{glyph}[/{color}] "
        f"[bold {color}]{kind:11s}[/bold {color}] "
        f"[dim]step[/dim] {step:<2} "
        f"[bold]{actor:<10s}[/bold]"
    )
    if summary:
        console.print(f"{head}  {summary}")
    else:
        # Generic fallback — show raw key=value detail.
        bits = [head]
        for k, v in detail.items():
            s = str(v)
            if len(s) > 60:
                s = s[:57] + "…"
            bits.append(f"[dim]{k}=[/dim]{s}")
        console.print("  ".join(bits))


def _summarise_event(kind: str, detail: dict) -> str:
    """Produce a one-line human-readable summary per event kind."""
    if kind == "verdict":
        approved = detail.get("approved")
        badge = (
            "[green]✓ approved[/green]" if approved
            else "[red]✗ rejected[/red]"
        )
        comment = (detail.get("comments") or "")[:80]
        n_block = len(detail.get("blockers") or [])
        rb = detail.get("route_back_to") or ""
        out = f"{badge}  {comment}"
        if not approved:
            if n_block:
                out += f"  [dim]· {n_block} blocker(s)[/dim]"
            if rb:
                out += f"  [dim]→ {rb}[/dim]"
        return out
    if kind == "review_round":
        return (
            f"round [bold]{detail.get('round','?')}[/bold]  "
            f"[dim]· {detail.get('verdicts','?')} verdicts[/dim]"
        )
    if kind == "route_back":
        return (
            f"→ [bold]{detail.get('target','?')}[/bold]  "
            f"[dim]· {detail.get('blockers','?')} blocker(s)[/dim]"
        )
    if kind == "applied":
        n = detail.get("count", 0)
        files = detail.get("files") or []
        if not n:
            return "[yellow]no diffs applied[/yellow]"
        head = f"[green]{n}[/green] file(s)"
        if files:
            head += f"  [dim]· {', '.join(files[:3])}"
            head += "…[/dim]" if len(files) > 3 else "[/dim]"
        return head
    if kind == "result":
        approved = detail.get("approved")
        n_applied = detail.get("applied_count", 0)
        if approved is True and n_applied:
            return f"[bold green]approved · applied {n_applied} file(s)[/bold green]"
        if approved is True:
            return "[bold yellow]approved · no diffs[/bold yellow]"
        if approved is False:
            return "[bold yellow]blocked by review[/bold yellow]"
        return "[cyan]chain finished[/cyan]"
    if kind == "discovery":
        fields = detail.get("fields") or []
        return f"[magenta]inspecting diffs[/magenta]  [dim]→ {', '.join(fields)}[/dim]"
    if kind == "executor":
        return f"[magenta]{detail.get('name','?')}[/magenta]  [dim]→ {', '.join(detail.get('fields',[])[:3])}[/dim]"
    if kind == "llm_call":
        prov = detail.get("provider", "")
        model = detail.get("model", "")
        sla = detail.get("sla_seconds")
        sla_str = f"  [dim]· sla {sla}s[/dim]" if sla else ""
        return f"[dim]{prov}/{model}[/dim]{sla_str}"
    if kind == "llm_retry":
        return (
            f"[yellow]retrying[/yellow]  "
            f"[dim]· {detail.get('reason','?')}[/dim]"
        )
    if kind == "outcome":
        st = detail.get("status", "?")
        dlg = detail.get("delegate_to")
        out = f"status=[bold]{st}[/bold]"
        if dlg:
            out += f"  [dim]→ {dlg}[/dim]"
        return out
    if kind == "handoff_in":
        return f"[dim]from[/dim] [bold]{detail.get('from','?')}[/bold]"
    if kind == "batch_split":
        return f"[blue]split into {detail.get('chunks','?')} chunks[/blue]"
    if kind == "batch_chunk":
        return f"[blue]chunk {detail.get('chunk','?')}/{detail.get('of','?')}[/blue]"
    if kind == "pr_opened":
        return f"[green]{detail.get('url','<none>')}[/green]"
    return ""  # caller falls through to generic key=value rendering


@app.command()
def serve(
    host: str = typer.Option(None, help="Bind host"),
    port: int = typer.Option(None, help="Bind port"),
) -> None:
    """Launch the FastAPI orchestrator."""
    settings = get_settings()
    uvicorn.run(
        "agentcore.orchestrator.app:app",
        host=host or settings.host,
        port=port or settings.port,
        log_level=settings.log_level,
        reload=False,
    )


# ---------------------------------------------------------------------------
# link claude
# ---------------------------------------------------------------------------


models_app = typer.Typer(help="Manage local model weights (embedder + reranker)")
app.add_typer(models_app, name="models")


@models_app.command("pull")
def models_pull(
    embedder: bool = typer.Option(True, help="Pull the embedder (Nomic-1.5)"),
    reranker: bool = typer.Option(True, help="Pull the cross-encoder reranker"),
) -> None:
    """Download and warm fastembed models so first inference is instant.

    Safe to run on host or inside the Docker image (it's invoked during
    image build to ship weights pre-cached).
    """
    from agentcore.models import pull_embedder, pull_reranker

    settings = get_settings()
    results = []
    if embedder:
        console.print("[bold]pulling embedder…[/bold]")
        results.append(pull_embedder(settings))
    if reranker:
        console.print("[bold]pulling reranker…[/bold]")
        results.append(pull_reranker(settings))

    table = Table("kind", "name", "status", "detail")
    any_failed = False
    for r in results:
        if r.ok:
            status = "[green]ready[/green]" if r.cached else "[green]downloaded[/green]"
        else:
            status = "[red]failed[/red]"
            any_failed = True
        table.add_row(r.kind, r.name, status, r.detail or "")
    console.print(table)
    if any_failed:
        raise typer.Exit(code=1)


link_app = typer.Typer(help="Link agentcore into other tools")
app.add_typer(link_app, name="link")


@link_app.command("claude")
def link_claude(
    with_hooks: bool = typer.Option(False, help="Also write .claude/settings.json hooks"),
    orchestrator_url: str = typer.Option(
        "http://localhost:8088", help="URL hooks should POST to"
    ),
) -> None:
    """Mirror agents/*.agent.md into .claude/agents/ for Claude Code."""
    settings = get_settings()
    result = claude_link(
        project_root=".",
        agents_dir=settings.agents_dir,
        with_hooks=with_hooks,
        orchestrator_url=orchestrator_url,
    )
    console.print(f"mirrored: {result.mirrored or '(none)'}")
    console.print(f"skipped:  {result.skipped or '(none)'}")
    if result.settings_written:
        console.print("[green]wrote[/green] .claude/settings.json hooks")


# ---------------------------------------------------------------------------
# migrate — Alembic wrapper for the durable Postgres schema
# ---------------------------------------------------------------------------


migrate_app = typer.Typer(help="Apply / inspect schema migrations")
app.add_typer(migrate_app, name="migrate")


def _alembic_main(args: list[str]) -> int:
    """Invoke alembic in-process so we don't shell out to a separate binary."""
    from alembic.config import main as _alembic_run

    settings = get_settings()
    # Auto-start Postgres if it's part of the local docker-compose stack.
    # Migrate is the first command users hit on a fresh checkout, so it
    # absolutely shouldn't dump a SQLAlchemy traceback when PG is down.
    from agentcore.state.bootstrap import ensure_postgres

    if not ensure_postgres(settings):
        console.print(
            "[red]postgres unreachable[/red] at "
            f"{settings.pg_host}:{settings.pg_port}.\n"
            "  start it with [cyan]docker compose up -d postgres[/cyan] or "
            "set PGHOST/PGPORT/PGUSER in your .env to a different DB."
        )
        return 2

    repo = Path(__file__).resolve().parent.parent.parent
    cfg_path = str(repo / "alembic.ini")
    try:
        return int(_alembic_run(argv=["-c", cfg_path, *args]) or 0)
    except Exception as exc:
        console.print(f"[red]alembic failed:[/red] {exc.__class__.__name__}: {exc}")
        return 3


@migrate_app.command("upgrade")
def migrate_upgrade(
    revision: str = typer.Argument("head", help="Target revision (default: head)"),
) -> None:
    """Apply all pending migrations up to <revision>. Idempotent."""
    raise typer.Exit(code=_alembic_main(["upgrade", revision]))


@migrate_app.command("downgrade")
def migrate_downgrade(
    revision: str = typer.Argument("-1", help="Target revision (default: previous)"),
) -> None:
    """Roll back to <revision>. Use cautiously."""
    raise typer.Exit(code=_alembic_main(["downgrade", revision]))


@migrate_app.command("current")
def migrate_current() -> None:
    """Print the database's current revision."""
    raise typer.Exit(code=_alembic_main(["current"]))


@migrate_app.command("history")
def migrate_history() -> None:
    """List the migration history."""
    raise typer.Exit(code=_alembic_main(["history"]))


# ---------------------------------------------------------------------------
# wiki — living codebase wiki (curated by the wikist agent, indexed in pgvector)
# ---------------------------------------------------------------------------


wiki_app = typer.Typer(help="Living codebase wiki: seed, search, link, install-hook")
app.add_typer(wiki_app, name="wiki")


def _resolve_branch(repo_root: Path) -> str:
    """Best-effort current-branch detection. Falls back to 'default'."""
    from agentcore.adapters.git_local import GitAdapter

    try:
        b = GitAdapter(repo_root=repo_root).current_branch()
        return b or "default"
    except Exception:
        return "default"


def _build_wiki_stack(settings, repo_root: Path):  # type: ignore[no-untyped-def]
    """Wire WikiStorage + WikiIndex + WikiCurator from current settings."""
    from agentcore.wiki.curator import WikiCurator
    from agentcore.wiki.index import WikiIndex
    from agentcore.wiki.storage import WikiStorage

    branch = _resolve_branch(repo_root)
    storage = WikiStorage(
        settings.wiki_root,
        settings.project_name,
        branch,
        settings=settings if settings.wiki_persistent else None,
    )

    embedder = None
    vector = None
    try:
        from agentcore.memory.embed import Embedder
        from agentcore.memory.vector import VectorStore

        v = VectorStore(settings)
        try:
            v.init_schema()
            vector = v
            embedder = Embedder(settings)
        except Exception as exc:
            console.print(
                f"[yellow]wiki retrieval offline (pgvector/embedder unavailable: {exc}); "
                "pages will be written to disk only[/yellow]"
            )
    except Exception:
        pass

    index = WikiIndex(storage, embedder, vector)
    router = LLMRouter(settings)
    curator = WikiCurator(
        router,
        storage,
        index,
        curator_model=settings.wiki_curator_model,
        curator_provider=settings.wiki_curator_provider,
    )
    return storage, index, curator


@wiki_app.command("rebuild")
def wiki_rebuild(
    repo: Path = typer.Argument(Path("."), help="Repo root to ingest"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-render every module page, ignoring the existing wiki state. "
        "Default: skip pages whose sources haven't changed since last write.",
    ),
) -> None:
    """Bulk-seed module pages. Smart by default — only re-renders modules
    whose sources have changed (or have no page yet)."""
    settings = get_settings()
    if not settings.enable_wiki:
        console.print(
            "[yellow]wiki disabled[/yellow] — set AGENTCORE_ENABLE_WIKI=true in .env to enable."
        )
        raise typer.Exit(code=1)
    configure_logging(settings.log_level)
    _, _, curator = _build_wiki_stack(settings, repo)

    async def _run() -> list[str]:
        return await curator.seed_from_repo(repo.resolve(), force=force)

    written = asyncio.run(_run())
    if not written:
        console.print("[yellow]no pages produced[/yellow] — check that the repo has source files.")
        return
    console.print(f"[green]wrote {len(written)} page(s):[/green]")
    for r in written:
        console.print(f"  · {r}")


@wiki_app.command("search")
def wiki_search(
    query: str = typer.Argument(..., help="Natural-language query"),
    repo: Path = typer.Option(Path("."), help="Repo root (resolves project + branch)"),
    k: int = typer.Option(8, help="Top-k hits"),
) -> None:
    """Semantic search over the wiki for this project + branch."""
    settings = get_settings()
    if not settings.enable_wiki:
        console.print(
            "[yellow]wiki disabled[/yellow] — set AGENTCORE_ENABLE_WIKI=true to enable."
        )
        raise typer.Exit(code=1)
    _, index, _ = _build_wiki_stack(settings, repo)
    if not index.is_ready:
        console.print(
            "[yellow]wiki search needs pgvector + the embedder; neither is available right now.[/yellow]"
        )
        raise typer.Exit(code=2)

    async def _run() -> list:
        return await index.search(query, k=k)

    hits = asyncio.run(_run())
    if not hits:
        console.print("[yellow]no hits[/yellow]")
        return
    table = Table("rel", "title", "score", "excerpt")
    for h in hits:
        excerpt = (h.excerpt or "")[:120].replace("\n", " ")
        table.add_row(h.rel, h.title, f"{h.score:.2f}", excerpt)
    console.print(table)


@wiki_app.command("link")
def wiki_link(
    tool: str = typer.Argument("all", help="claude | copilot | cursor | all"),
    repo: Path = typer.Option(Path("."), help="Repo root"),
) -> None:
    """Mirror the wiki into another tool's expected location."""
    settings = get_settings()
    if not settings.enable_wiki:
        console.print(
            "[yellow]wiki disabled[/yellow] — set AGENTCORE_ENABLE_WIKI=true to enable."
        )
        raise typer.Exit(code=1)
    storage, _, _ = _build_wiki_stack(settings, repo)
    targets = ("claude", "copilot", "cursor") if tool == "all" else (tool,)
    valid = {"claude", "copilot", "cursor"}
    bad = [t for t in targets if t not in valid]
    if bad:
        console.print(f"[red]unknown link target(s):[/red] {bad}")
        raise typer.Exit(code=2)

    project_root = Path(repo).resolve()
    summary: list[str] = []
    if "claude" in targets:
        n = link_claude_wiki(project_root, storage)
        summary.append(f"claude: {n} skill page(s)")
    if "copilot" in targets:
        n = link_copilot_wiki(project_root, storage)
        summary.append(f"copilot: {n} prompt(s)")
    if "cursor" in targets:
        ok = link_cursor_wiki(project_root, storage)
        summary.append(f"cursor: {'wrote rules' if ok else 'no-op'}")
    for line in summary:
        console.print(f"  · {line}")


@wiki_app.command("install-hook")
def wiki_install_hook(
    repo: Path = typer.Option(Path("."), help="Repo root"),
) -> None:
    """Install a git post-commit hook that POSTs changed paths to /wiki/refresh.

    Cross-platform: writes a Python hook so it works the same on POSIX and
    Windows. Git on Windows uses Git Bash to dispatch hooks; both shells
    can run `python` directly. We avoid bash-isms (chmod, tr, sed) so
    nothing is OS-specific in the script itself.
    """
    import stat

    repo_root = Path(repo).resolve()
    git_dir = repo_root / ".git"
    if not git_dir.is_dir():
        console.print("[yellow]not a git repo — nothing to install[/yellow]")
        raise typer.Exit(code=1)
    hook = git_dir / "hooks" / "post-commit"
    settings = get_settings()
    # Inline a tiny Python script; works identically on POSIX + Windows.
    body = f'''#!/usr/bin/env python3
"""Auto-installed by `agentcore wiki install-hook`. Cross-platform."""
import json, os, subprocess, sys, urllib.request

URL = os.environ.get("AGENTCORE_URL", "http://{settings.host}:{settings.port}") + "/wiki/refresh"
TOKEN = os.environ.get("AGENTCORE_API_TOKEN")

def _run(*args):
    return subprocess.run(args, capture_output=True, text=True, check=False).stdout.strip()

sha = _run("git", "rev-parse", "HEAD")
out = _run("git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
paths = [p for p in out.splitlines() if p]
if not paths:
    sys.exit(0)
payload = json.dumps({{"commit_sha": sha, "changed_paths": paths}}).encode("utf-8")
req = urllib.request.Request(
    URL,
    data=payload,
    headers={{"Content-Type": "application/json"}},
    method="POST",
)
if TOKEN:
    req.add_header("Authorization", f"Bearer {{TOKEN}}")
try:
    with urllib.request.urlopen(req, timeout=5) as _:
        pass
except Exception:
    # Hook is fire-and-forget; never fail a commit because the wiki was offline.
    pass
'''
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(body, encoding="utf-8")
    # Mark executable on POSIX. On Windows chmod is a no-op and Git uses the
    # file extension / shebang to dispatch, so this is safe to skip there.
    if os.name == "posix":
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    console.print(f"[green]installed[/green] {hook}")


if __name__ == "__main__":  # pragma: no cover
    app()
