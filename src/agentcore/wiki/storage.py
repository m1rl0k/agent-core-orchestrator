"""Filesystem layer for the wiki.

`WikiStorage` is a small, deterministic IO module:
  - read / write / delete pages under `<root>/<project>/<branch>/...`
  - atomic writes (temp + rename) so partial files never appear
  - content-hash skip: writes are no-ops when the page hasn't actually changed
  - frontmatter merge: never silently drops `sources[]` or audit fields
  - walk: yields every WikiPage under the project/branch root

The path layout is stable so other tooling (Claude Code skills mirror, web
view, the Obsidian vault) can rely on it:

  <root>/<project>/<branch>/
    index.md
    log.md
    glossary.md
    modules/<module>.md
    subsystems/<topic>.md
    decisions/<id>.md
    howto/<task>.md
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import frontmatter


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _content_hash(body: str, sources: list[str]) -> str:
    """Stable hash over body + sorted sources (mirrors what's in frontmatter).

    Lets `write()` skip writes that wouldn't change anything. The hash is
    also stored in frontmatter so external tools can detect drift.
    """
    h = hashlib.sha256()
    h.update(body.encode("utf-8"))
    for s in sorted(sources):
        h.update(b"\x00")
        h.update(s.encode("utf-8"))
    return h.hexdigest()[:16]


@dataclass(slots=True)
class WikiPage:
    """One markdown page in the wiki.

    `rel` is the path *relative to the project/branch root*, e.g.
    `modules/orchestrator.md`. `frontmatter` carries the YAML header (title,
    sources, status, etc.). `body` is the markdown body.
    """

    rel: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def title(self) -> str:
        return str(self.frontmatter.get("title") or self.rel)

    @property
    def sources(self) -> list[str]:
        srcs = self.frontmatter.get("sources") or []
        return [str(s) for s in srcs]

    def to_text(self) -> str:
        post = frontmatter.Post(self.body, **self.frontmatter)
        return frontmatter.dumps(post)

    def with_audit(self, *, commit_sha: str | None = None) -> WikiPage:
        """Return a copy with `last_updated` (and optionally `last_commit`)
        and a refreshed `content_hash` baked into frontmatter."""
        fm = dict(self.frontmatter)
        fm["last_updated"] = _utcnow_iso()
        if commit_sha:
            fm["last_commit"] = commit_sha
        fm["content_hash"] = _content_hash(self.body, self.sources)
        return WikiPage(rel=self.rel, frontmatter=fm, body=self.body)


class WikiStorage:
    """Project/branch-scoped wiki IO.

    Constructed once per (project, branch) tuple. The on-disk root is
    `<wiki_root>/<project>/<branch>` and is created on first write.
    """

    def __init__(
        self,
        wiki_root: Path | str,
        project: str,
        branch: str,
    ) -> None:
        self.wiki_root = Path(wiki_root)
        self.project = project
        self.branch = branch or "default"
        self.root = self.wiki_root / self.project / self.branch
        self._root_resolved = self.root.resolve()

    # ---- read ---------------------------------------------------------

    def page_path(self, rel: str) -> Path:
        candidate = Path(rel)
        if candidate.is_absolute():
            raise ValueError(f"wiki page path must be relative: {rel!r}")
        resolved = (self.root / candidate).resolve()
        try:
            resolved.relative_to(self._root_resolved)
        except ValueError as exc:
            raise ValueError(f"wiki page path escapes wiki root: {rel!r}") from exc
        return resolved

    def read(self, rel: str) -> WikiPage | None:
        p = self.page_path(rel)
        if not p.exists():
            return None
        try:
            post = frontmatter.load(p)
        except Exception:
            return None
        return WikiPage(rel=rel, frontmatter=dict(post.metadata), body=post.content)

    def walk(self) -> Iterator[WikiPage]:
        if not self.root.exists():
            return
        for p in sorted(self.root.rglob("*.md")):
            rel = p.relative_to(self.root).as_posix()
            page = self.read(rel)
            if page is not None:
                yield page

    # ---- write --------------------------------------------------------

    def write(self, page: WikiPage, *, commit_sha: str | None = None) -> bool:
        """Atomic write of `page`. Returns True iff the on-disk content changed.

        The new page's `content_hash` is computed and stored in frontmatter.
        If the existing page has the same hash, this is a no-op.
        """
        # Merge with any existing frontmatter so we preserve audit fields.
        existing = self.read(page.rel)
        merged_fm = self.merge_frontmatter(
            existing.frontmatter if existing else {}, page.frontmatter
        )
        new = WikiPage(rel=page.rel, frontmatter=merged_fm, body=page.body).with_audit(
            commit_sha=commit_sha
        )

        # Hash-skip.
        if existing is not None and existing.frontmatter.get("content_hash") == new.frontmatter[
            "content_hash"
        ]:
            return False

        target = self.page_path(new.rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = new.to_text()
        # Atomic temp + rename, same directory so rename is on the same fs.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".wiki-",
            suffix=".tmp",
            dir=target.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp_path, target)
        except Exception:
            # Best-effort cleanup of the orphan temp file.
            with suppress(OSError):
                os.unlink(tmp_path)
            raise
        return True

    def delete(self, rel: str) -> bool:
        p = self.page_path(rel)
        if not p.exists():
            return False
        p.unlink()
        return True

    # ---- helpers ------------------------------------------------------

    @staticmethod
    def merge_frontmatter(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        """Merge `new` over `existing`, preserving important audit fields.

        - `sources` is unioned (de-duplicated, order-preserving).
        - `last_updated` / `last_commit` / `content_hash` are owned by
          `with_audit()` and recomputed on write — we drop any inbound copies
          so callers can't smuggle stale audit values.
        """
        out: dict[str, Any] = dict(existing)
        for k, v in new.items():
            if k in {"last_updated", "last_commit", "content_hash"}:
                continue
            if k == "sources" and isinstance(v, list):
                old = list(existing.get("sources") or [])
                seen: set[str] = set(old)
                for s in v:
                    if s not in seen:
                        old.append(s)
                        seen.add(s)
                out["sources"] = old
            else:
                out[k] = v
        return out

    def collection_name(self) -> str:
        """The pgvector collection this storage's pages should be indexed under."""
        return f"wiki:{self.project}:{self.branch}"
