"""Common interface for optional, host-credentialed adapters.

Adapters never own credentials. They wrap host CLIs (`gh`, `aws`, `az`, `git`)
that the operator has already installed and authenticated. Each adapter knows
how to (a) confirm it's usable and (b) yield Signals when polled.
"""

from __future__ import annotations

import shlex
import subprocess
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence

from agentcore.capabilities import Capability
from agentcore.contracts.domain import Signal


class Adapter(ABC):
    """Base class for opt-in adapters."""

    name: str
    cli: str

    def __init__(self, capability: Capability) -> None:
        self.capability = capability

    @property
    def is_ready(self) -> bool:
        return self.capability.status == "ready"

    # Default scan loop is "yield nothing"; subclasses override for triggers.
    async def scan(self) -> AsyncIterator[Signal]:
        if False:  # pragma: no cover - keeps this an async generator
            yield  # type: ignore[unreachable]

    @abstractmethod
    def short_status(self) -> str:
        """One-line status for `agentcore doctor`."""

    # ---- helpers --------------------------------------------------------

    def _shell(self, args: Sequence[str], *, timeout: float = 30.0) -> tuple[int, str, str]:
        """Run a CLI command with no shell expansion. Returns (rc, stdout, stderr)."""
        out = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return out.returncode, out.stdout, out.stderr

    @staticmethod
    def _quote(s: str) -> str:
        return shlex.quote(s)
