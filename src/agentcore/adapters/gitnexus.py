"""GitNexus adapter — wraps the `gitnexus` (or `npx gitnexus`) CLI.

GitNexus indexes a repo into a tree-sitter-backed code knowledge graph and
exposes it as MCP tools. Here we use it through its CLI surface so any
agentcore role (Architect, Developer) can call it directly without owning
an MCP client.

Capabilities required: `node` + `npx` on PATH (npm distribution), or a
globally-installed `gitnexus` binary.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from agentcore.adapters.base import Adapter
from agentcore.capabilities import Capability


def _gitnexus_invocation() -> list[str] | None:
    """Resolve how to invoke gitnexus on this host."""
    if shutil.which("gitnexus"):
        return ["gitnexus"]
    if shutil.which("npx"):
        return ["npx", "--yes", "gitnexus"]
    return None


class GitnexusAdapter(Adapter):
    name = "gitnexus"
    cli = "gitnexus"

    def __init__(self, repo_root: Path | str = ".") -> None:
        invocation = _gitnexus_invocation()
        cap = Capability(
            name="gitnexus",
            enabled=True,
            installed=invocation is not None,
            authenticated=invocation is not None,
            cli="gitnexus",
            install_hint=(
                "macOS: brew install node && npx --yes gitnexus  ·  "
                "Linux: install Node.js, then `npx --yes gitnexus`  ·  "
                "Windows: winget install OpenJS.NodeJS, then `npx --yes gitnexus`"
            ),
            auth_hint="—",
        )
        super().__init__(cap)
        self._invocation = invocation
        self.repo_root = Path(repo_root).resolve()

    def short_status(self) -> str:
        if self._invocation is None:
            return f"gitnexus: missing — {self.capability.install_hint}"
        return f"gitnexus: ready · {' '.join(self._invocation)}"

    def _run(self, args: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
        if self._invocation is None:
            return 127, "", "gitnexus not installed"
        return self._shell([*self._invocation, *args, "--cwd", str(self.repo_root)],
                           timeout=timeout)

    # ---- subcommands ----------------------------------------------------

    def analyze(self) -> tuple[bool, str]:
        """Index the repo. Idempotent."""
        rc, out, err = self._run(["analyze"], timeout=600.0)
        return rc == 0, out + err

    def context(self, symbol: str) -> str:
        """360° view + references for a symbol."""
        _, out, _ = self._run(["context", symbol])
        return out

    def impact(self, symbol: str) -> str:
        """Blast-radius analysis."""
        _, out, _ = self._run(["impact", symbol])
        return out

    def detect_changes(self) -> str:
        """Risk analysis on the current uncommitted diff."""
        _, out, _ = self._run(["detect_changes"])
        return out

    def cypher(self, query: str) -> str:
        """Raw graph query."""
        _, out, _ = self._run(["cypher", query])
        return out
