"""Single source of truth for wiki collection / ref naming.

Cross-tenant safety relies on `wiki:<project>:<branch>` being constructed
identically everywhere (storage, index, retrieval, search). A typo or a
stray colon in a project name would silently route a write into the
wrong tenant's collection. These helpers make that class of bug
impossible by:

  1. Centralising construction — every call site goes through
     `wiki_collection()` / `wiki_ref()`. No more f-strings scattered
     across the codebase.
  2. Sanitising inputs — `project` and `branch` are validated to a
     conservative charset; anything else is mapped to `_`. The `:`
     separator is reserved.
  3. Round-trip — `parse_collection()` returns `(project, branch)` so
     callers reading from the store can verify what they got.

If you find yourself writing `f"wiki:{project}:{branch}"` somewhere,
swap it for `wiki_collection(project, branch)`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Conservative: alnum, dot, dash, underscore. Excludes `:` (reserved
# as our separator), `/` (filesystem), whitespace, and shell-special
# characters. Branch names can include `/` legitimately (`feat/x`)
# so we map those to `_` rather than reject.
_SAFE = re.compile(r"[^A-Za-z0-9._\-]+")


def _safe(part: str) -> str:
    if not part:
        return "default"
    return _SAFE.sub("_", part).strip("_") or "default"


def wiki_collection(project: str, branch: str) -> str:
    """Canonical pgvector collection name for a project+branch wiki.

    Example: `wiki_collection("agent-core", "feat/x")` ->
    `"wiki:agent-core:feat_x"`.
    """
    return f"wiki:{_safe(project)}:{_safe(branch)}"


def wiki_ref(project: str, branch: str, rel: str) -> str:
    """Canonical per-page ref used as the pgvector row id."""
    return f"{wiki_collection(project, branch)}:{rel}"


@dataclass(frozen=True, slots=True)
class WikiCoords:
    project: str
    branch: str


def parse_collection(name: str) -> WikiCoords | None:
    """Parse a collection string back into `(project, branch)`. Returns
    None for anything that doesn't match the canonical shape — useful
    when reading rows of unknown provenance."""
    if not name.startswith("wiki:"):
        return None
    parts = name.split(":", 2)
    if len(parts) != 3:
        return None
    _, project, branch = parts
    if not project or not branch:
        return None
    return WikiCoords(project=project, branch=branch)
