"""Typer-based CLI entrypoint for `agentcore`."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from agentcore.adapters.claude_code import link as claude_link
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

    store = VectorStore(settings)
    if init_schema:
        store.init_schema()
    idx = CodeIndex(repo)
    symbols = idx.index()
    console.print(f"[green]found[/green] {len(symbols)} symbols under {repo.resolve()}")
    if not symbols:
        return

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
    graph = KnowledgeGraph()
    graph.load()
    graphify = (
        GraphifyAdapter(repo_root=settings.graphify_repo_root, enabled=True)
        if settings.enable_graphify
        else None
    )
    runtime = Runtime(
        registry=registry, router=router, traces=traces, graph=graph, graphify=graphify
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


if __name__ == "__main__":  # pragma: no cover
    app()
