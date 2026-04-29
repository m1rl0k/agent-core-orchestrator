"""Pre-LLM executors.

Each agent.md may declare zero or more executors that run BEFORE the
LLM is called. Output is merged into the handoff payload so the LLM
sees real tool output instead of inventing pass/fail.

Two forms are accepted in `<role>.agent.md`:

  executors:
    # Inline command — preferred. Project owns its pipeline.
    - name: pytest
      command: [pytest, -q]
    - name: ruff
      command: [ruff, check, .]

  executors:
    # Legacy: a string names a registered executor. Kept for
    # back-compat with earlier specs; new specs should declare
    # commands inline.
    - tests

Each executor runs in a temp git worktree with the developer's diffs
already applied (see `agentcore.runtime.sandbox.apply_in_worktree`).
This keeps the live tree clean and makes runs reproducible across
re-review loops.

Adding a new generic executor (string-name form) is two steps: write
the function, register it in `EXECUTORS`. Most projects shouldn't
need this — declare commands inline in agent.md instead.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from agentcore.runtime.sandbox import run_in_worktree
from agentcore.spec.models import ExecutorSpec

log = structlog.get_logger(__name__)

Executor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Registered named executors (legacy form: `executors: [<name>]`)
#
# Inline command form (`executors: [{name, command}]`) is preferred —
# this registry is intentionally minimal so we don't reintroduce the
# brittle per-language heuristics. Projects own their pipeline.
# ---------------------------------------------------------------------------


EXECUTORS: dict[str, Executor] = {}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def run_inline(spec: ExecutorSpec, payload: dict[str, Any]) -> dict[str, Any]:
    """Run an inline ExecutorSpec command in a worktree against
    `payload['diffs']`. Returns `{<name>: {exit_code, stdout_tail,
    stderr_tail, artifact?}}` so the LLM sees real output keyed by
    the executor's declared name.
    """
    diffs = payload.get("diffs") or []
    file_ops = payload.get("file_ops") or []
    repo_root = payload.get("repo_root")
    try:
        result = await run_in_worktree(
            spec.command,
            diffs=diffs,
            file_ops=file_ops,
            repo_root=repo_root,
            timeout_seconds=spec.timeout_seconds,
            artifact=spec.artifact,
        )
    except Exception as exc:
        log.warning("executor.inline_failed", name=spec.name, error=str(exc))
        result = {
            "exit_code": -1,
            "stdout_tail": "",
            "stderr_tail": str(exc)[:500],
            "executor_status": "error",
        }
    return {spec.name: result}


async def run_named(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    fn = EXECUTORS.get(name)
    if fn is None:
        log.warning("executor.unknown", name=name)
        return {}
    try:
        return await fn(payload)
    except Exception as exc:
        log.warning("executor.named_failed", name=name, error=str(exc))
        return {}


async def run_executor(
    item: ExecutorSpec | str | dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Dispatch one executor entry from agent.md. Accepts the typed
    ExecutorSpec, the string short-form, or a raw dict for tolerance.
    """
    if isinstance(item, str):
        return await run_named(item, payload)
    if isinstance(item, ExecutorSpec):
        return await run_inline(item, payload)
    if isinstance(item, dict):
        try:
            return await run_inline(ExecutorSpec(**item), payload)
        except Exception as exc:
            log.warning("executor.bad_spec", error=str(exc))
            return {}
    log.warning("executor.invalid_type", value=str(item)[:100])
    return {}


# Re-exports for type checkers / older callers.
__all__ = [
    "EXECUTORS",
    "ExecutorSpec",
    "run_executor",
    "run_inline",
    "run_named",
]
