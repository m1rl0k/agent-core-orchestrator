"""Typer-based CLI entrypoint for `agentcore`."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
import uvicorn
from rich.console import Console
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
from agentcore.llm.router import LLMRouter
from agentcore.logging_setup import configure_logging
from agentcore.orchestrator.runtime import Runtime
from agentcore.orchestrator.traces import TraceLog
from agentcore.settings import get_settings
from agentcore.spec.loader import AgentRegistry

app = typer.Typer(no_args_is_help=True, add_completion=False, help="agent-core-orchestrator CLI")
console = Console()


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
            (spec.source_path or "")[-60:],
        )
    console.print(a_table)
    if registry.errors():
        console.rule("[bold red]parse errors")
        for path, err in registry.errors().items():
            console.print(f"[red]{path}[/red]: {err}")


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
) -> None:
    """Run the role mesh end-to-end on this repo."""
    asyncio.run(_plan_async(brief, chain, max_hops))


async def _plan_async(brief: str, chain: bool, max_hops: int) -> None:
    from agentcore.memory import prf
    from agentcore.memory.graph import KnowledgeGraph

    settings = get_settings()
    configure_logging(settings.log_level)

    registry = AgentRegistry()
    registry.load_dir(settings.agents_dir)
    if registry.get("architect") is None:
        console.print("[red]architect agent not loaded[/red]; check agents/")
        raise typer.Exit(code=1)

    router = LLMRouter(settings)
    traces = TraceLog()
    import contextlib

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
    # Best-effort retriever: only if pgvector + fastembed are usable.
    from agentcore.retrieval.factory import try_build_retriever

    retriever = try_build_retriever(settings, graph)
    if retriever is None:
        console.print("[yellow]retriever offline (see structured logs); plans will run without semantic context[/yellow]")

    runtime = Runtime(
        registry=registry, router=router, traces=traces,
        graph=graph, graphify=graphify, retriever=retriever,
    )

    handoff = Handoff(
        task_id=new_task_id(),
        from_agent="user",
        to_agent="architect",
        payload={"brief": brief},
    )
    console.print(f"[bold]task {handoff.task_id}[/bold]")

    final_outcome = None
    current: Handoff | None = handoff
    for _ in range(max_hops):
        if current is None:
            break
        outcome, nxt = await runtime.execute(current)
        final_outcome = outcome
        console.rule(f"[bold]{outcome.agent}[/bold] · {outcome.status}")
        console.print_json(json.dumps(outcome.output))
        if not (chain and nxt):
            break
        current = nxt

    # Chain-end PRF tagging.
    if final_outcome is not None:
        out = final_outcome.output
        if final_outcome.agent == "qa":
            failed = out.get("failed") or []
            if failed:
                graph.tag_relevance(handoff.task_id, prf.QA_FAILED,
                                    score=float(len(failed)),
                                    reason="qa returned failures")
                graph.tag_task(handoff.task_id, prf.DEV_REVISED)
            else:
                graph.tag_relevance(handoff.task_id, prf.QA_PASSED)
                graph.tag_task(handoff.task_id, prf.POSITIVE)
        elif final_outcome.agent == "ops":
            status = str(out.get("pipeline_status", ""))
            if status == "passed":
                graph.tag_task(handoff.task_id, prf.SHIPPED)
            elif status == "failed":
                graph.tag_task(handoff.task_id, prf.OPS_BLOCKED)

    graph.save()


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


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
    from agentcore.llm.router import LLMRouter
    from agentcore.wiki.curator import WikiCurator
    from agentcore.wiki.index import WikiIndex
    from agentcore.wiki.storage import WikiStorage

    branch = _resolve_branch(repo_root)
    storage = WikiStorage(settings.wiki_root, settings.project_name, branch)

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
    )
    return storage, index, curator


@wiki_app.command("rebuild")
def wiki_rebuild(
    repo: Path = typer.Argument(Path("."), help="Repo root to ingest"),
) -> None:
    """Bulk-seed module pages by reading the repo. Idempotent."""
    settings = get_settings()
    if not settings.enable_wiki:
        console.print(
            "[yellow]wiki disabled[/yellow] — set AGENTCORE_ENABLE_WIKI=true in .env to enable."
        )
        raise typer.Exit(code=1)
    configure_logging(settings.log_level)
    _, _, curator = _build_wiki_stack(settings, repo)

    async def _run() -> list[str]:
        return await curator.seed_from_repo(repo.resolve())

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
    import os
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
