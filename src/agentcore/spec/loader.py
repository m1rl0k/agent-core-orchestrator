"""Hot-reloading registry of AgentSpecs.

The registry is the single source of truth for "which agents exist right now".
A background watcher reparses files on change. In-flight tasks should pin the
spec version they started with (see `Registry.snapshot`).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path

import structlog
from watchfiles import Change, awatch

from agentcore.spec.models import AgentSpec
from agentcore.spec.parser import SpecParseError, parse_agent_file

log = structlog.get_logger(__name__)

AGENT_GLOB = "*.agent.md"


class AgentRegistry:
    """Thread-safe in-memory registry of currently loaded AgentSpecs."""

    def __init__(self) -> None:
        self._by_name: dict[str, AgentSpec] = {}
        self._errors: dict[str, str] = {}
        self._lock = threading.RLock()

    # ---- mutation ---------------------------------------------------------

    def upsert(self, spec: AgentSpec) -> None:
        with self._lock:
            self._by_name[spec.name] = spec
            self._errors.pop(str(spec.source_path), None)
            log.info("agent.loaded", name=spec.name, source=spec.source_path)

    def remove_by_path(self, path: str) -> None:
        with self._lock:
            removed = [n for n, s in self._by_name.items() if s.source_path == path]
            for n in removed:
                del self._by_name[n]
                log.info("agent.removed", name=n, source=path)
            self._errors.pop(path, None)

    def record_error(self, path: str, error: str) -> None:
        with self._lock:
            self._errors[path] = error
            log.warning("agent.parse_error", source=path, error=error)

    # ---- read -------------------------------------------------------------

    def get(self, name: str) -> AgentSpec | None:
        with self._lock:
            return self._by_name.get(name)

    def all(self) -> list[AgentSpec]:
        with self._lock:
            return list(self._by_name.values())

    def errors(self) -> dict[str, str]:
        with self._lock:
            return dict(self._errors)

    def snapshot(self) -> dict[str, AgentSpec]:
        """Immutable view; safe to hand to a long-running task."""
        with self._lock:
            return dict(self._by_name)

    def __iter__(self) -> Iterator[AgentSpec]:
        return iter(self.all())

    # ---- bulk ops ---------------------------------------------------------

    def load_dir(self, directory: Path | str) -> None:
        d = Path(directory)
        if not d.exists():
            # Fallback: resolve against the agentcore package's repo root
            # so a relative `agents/` default still finds the bundled
            # specs even when the CLI is invoked from outside the repo.
            bundled = (
                Path(__file__).resolve().parent.parent.parent.parent
                / "agents"
            )
            if bundled.exists() and bundled != d.resolve():
                log.info(
                    "agents_dir.using_bundled",
                    requested=str(d),
                    bundled=str(bundled),
                )
                d = bundled
            else:
                log.warning("agents_dir.missing", path=str(d))
                return
        for path in sorted(d.glob(AGENT_GLOB)):
            self._load_one(path)

    def _load_one(self, path: Path) -> None:
        try:
            spec = parse_agent_file(path)
        except SpecParseError as exc:
            self.record_error(str(path.resolve()), str(exc))
            return
        self.upsert(spec)


async def watch_agents_dir(
    directory: Path | str,
    registry: AgentRegistry,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run forever, reflecting on-disk changes into `registry`."""
    d = Path(directory).resolve()
    log.info("watcher.start", path=str(d))
    registry.load_dir(d)

    async for changes in awatch(str(d), stop_event=stop_event):
        for change, raw_path in _filter_agent_changes(changes):
            path = Path(raw_path)
            if change == Change.deleted:
                registry.remove_by_path(str(path.resolve()))
            else:
                registry._load_one(path)


def _filter_agent_changes(changes: Iterable[tuple[Change, str]]) -> Iterator[tuple[Change, str]]:
    for change, path in changes:
        if path.endswith(".agent.md"):
            yield change, path
