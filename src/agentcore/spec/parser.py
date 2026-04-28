"""Parse `*.agent.md` files into typed AgentSpec instances."""

from __future__ import annotations

import hashlib
from pathlib import Path

import frontmatter

from agentcore.spec.models import AgentSpec


class SpecParseError(ValueError):
    """Raised when an agent file cannot be turned into a valid AgentSpec."""

    def __init__(self, source: str, cause: Exception) -> None:
        super().__init__(f"failed to parse agent spec from {source!r}: {cause}")
        self.source = source
        self.cause = cause


def parse_agent_text(text: str, *, source: str = "<inline>") -> AgentSpec:
    """Parse a markdown string with YAML frontmatter into an AgentSpec.

    The body of the document becomes the agent's `system_prompt`.
    """
    try:
        post = frontmatter.loads(text)
        data = dict(post.metadata)
        data["system_prompt"] = post.content.strip()
        spec = AgentSpec.model_validate(data)
    except Exception as exc:  # pragma: no cover - re-raised with context
        raise SpecParseError(source, exc) from exc

    spec.source_path = source if source != "<inline>" else None
    spec.checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return spec


def parse_agent_file(path: Path | str) -> AgentSpec:
    """Read a file from disk and parse it. Source path is recorded for traces."""
    p = Path(path)
    return parse_agent_text(p.read_text(encoding="utf-8"), source=str(p.resolve()))
