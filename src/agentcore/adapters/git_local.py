"""Local-git adapter — always-on, the Ops role's primary tool."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from agentcore.adapters.base import Adapter
from agentcore.capabilities import Capability
from agentcore.contracts.domain import Signal


class GitAdapter(Adapter):
    name = "git"
    cli = "git"

    def __init__(self, repo_root: Path | str = ".") -> None:
        # local git is universally assumed; we still expose a trivial Capability
        cap = Capability(
            name="git",
            enabled=True,
            installed=True,
            authenticated=True,
            cli="git",
            install_hint="brew install git",
            auth_hint="—",
        )
        super().__init__(cap)
        self.repo_root = Path(repo_root).resolve()

    def short_status(self) -> str:
        rc, out, _ = self._shell(["git", "-C", str(self.repo_root), "rev-parse", "--abbrev-ref", "HEAD"])
        if rc != 0:
            return "git: not a repo"
        return f"git: on {out.strip()}"

    def current_branch(self) -> str:
        _, out, _ = self._shell(["git", "-C", str(self.repo_root), "rev-parse", "--abbrev-ref", "HEAD"])
        return out.strip()

    def head_sha(self) -> str:
        _, out, _ = self._shell(["git", "-C", str(self.repo_root), "rev-parse", "HEAD"])
        return out.strip()

    def diff_against(self, base: str = "main") -> str:
        _, out, _ = self._shell(["git", "-C", str(self.repo_root), "diff", f"{base}...HEAD"])
        return out

    def recent_log(self, n: int = 20) -> str:
        _, out, _ = self._shell(
            ["git", "-C", str(self.repo_root), "log", f"-n{n}", "--oneline", "--no-decorate"]
        )
        return out

    async def scan(self) -> AsyncIterator[Signal]:
        # Local git is reactive (used by other roles), not a Signal source.
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]
