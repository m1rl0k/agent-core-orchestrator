"""Pre-LLM executors. Each takes a handoff payload and returns a dict
that gets merged into the payload before the LLM is called.

Executors are how we ground agents in real tool output — pytest results,
typecheck output, lint findings — instead of letting the LLM imagine them.
The QA agent declares `executors: [pytest]` so the runtime runs the test
suite against the developer's diffs in a temp git worktree, then feeds
the actual passed/failed lists into the QA's payload as `test_run`.

Adding a new executor is two steps: write the function, register it in
`EXECUTORS`. Agents opt in by listing the name in their `executors:`
list in `<role>.agent.md`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from agentcore.runtime.pytest_executor import run_tests

log = structlog.get_logger(__name__)

Executor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def _tests_executor(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply payload['diffs'] to a temp git worktree, detect the test
    runner already present on PATH (pytest, go test, cargo test, jest,
    mvn, gradle, dotnet, ctest, rspec, phpunit), execute it, and return
    structured results under the `test_run` key. Never installs anything;
    repos with no detectable runner get `executor_status='no_runner'`.
    """
    diffs = payload.get("diffs") or []
    repo_root = payload.get("repo_root")  # caller-supplied; falls back to cwd
    result = await run_tests(diffs, repo_root=repo_root)
    return {"test_run": result}


EXECUTORS: dict[str, Executor] = {
    # Polyglot test runner. Agents declare `executors: [tests]`.
    "tests": _tests_executor,
    # Back-compat alias so older agent.md files using `pytest` still work.
    "pytest": _tests_executor,
}


async def run_executor(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run a registered executor by name. Unknown names log + return {}."""
    fn = EXECUTORS.get(name)
    if fn is None:
        log.warning("executor.unknown", name=name)
        return {}
    try:
        return await fn(payload)
    except Exception as exc:
        log.warning("executor.failed", name=name, error=str(exc))
        return {}
