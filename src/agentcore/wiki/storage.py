"""Filesystem + optional Postgres layer for the wiki.

`WikiStorage` is a small, deterministic IO module:
  - read / write / delete pages under `<root>/<project>/<branch>/...`
  - atomic writes (temp + rename) so partial files never appear
  - content-hash skip: writes are no-ops when the page hasn't actually changed
  - frontmatter merge: never silently drops `sources[]` or audit fields
  - walk: yields every WikiPage under the project/branch root

When constructed with `settings`, pages are also mirrored to Postgres so
multi-node orchestrators can serve the same wiki. Disk remains the local
fallback and compatibility layer for Obsidian/vault workflows.

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
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import frontmatter
import psycopg
import structlog

from agentcore.settings import Settings

log = structlog.get_logger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS agentcore_wiki_pages (
  project_id    TEXT NOT NULL,
  branch        TEXT NOT NULL,
  rel           TEXT NOT NULL,
  title         TEXT NOT NULL,
  frontmatter   JSONB NOT NULL DEFAULT '{}'::jsonb,
  body          TEXT NOT NULL DEFAULT '',
  content_hash  TEXT NOT NULL DEFAULT '',
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, branch, rel)
);
CREATE INDEX IF NOT EXISTS idx_agentcore_wiki_pages_project_branch
  ON agentcore_wiki_pages (project_id, branch);
CREATE INDEX IF NOT EXISTS idx_agentcore_wiki_pages_updated
  ON agentcore_wiki_pages (project_id, branch, updated_at DESC);
"""


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
        *,
        settings: Settings | None = None,
    ) -> None:
        self.wiki_root = Path(wiki_root)
        self.project = project
        self.branch = branch or "default"
        self.settings = settings
        self._persistent = False
        self.root = self.wiki_root / self.project / self.branch
        self._root_resolved = self.root.resolve()
        if self.settings is not None:
            self.init_schema()

    def init_schema(self) -> bool:
        if self.settings is None:
            self._persistent = False
            return False
        try:
            with (
                psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(DDL)
            self._persistent = True
            log.info("wiki.pages_persistent")
            return True
        except Exception as exc:
            self._persistent = False
            log.info("wiki.pages_disk_only", reason=str(exc))
            return False

    @property
    def is_persistent(self) -> bool:
        return self._persistent

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
        # Validate path even when Postgres serves the content: `rel` still
        # names a page in the wiki namespace and must not escape the root.
        self.page_path(rel)
        if self._persistent:
            page = self._read_pg(rel)
            if page is not None:
                return page
        return self._read_disk(rel)

    def _read_disk(self, rel: str) -> WikiPage | None:
        p = self.page_path(rel)
        if not p.exists():
            return None
        try:
            post = frontmatter.load(p)
        except Exception:
            return None
        return WikiPage(rel=rel, frontmatter=dict(post.metadata), body=post.content)

    def _read_pg(self, rel: str) -> WikiPage | None:
        if self.settings is None:
            return None
        try:
            with (
                psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    """
                    SELECT frontmatter, body
                      FROM agentcore_wiki_pages
                     WHERE project_id = %s
                       AND branch = %s
                       AND rel = %s
                    """,
                    (self.project, self.branch, rel),
                )
                row = cur.fetchone()
        except Exception as exc:
            log.warning("wiki.page_read_failed", rel=rel, error=str(exc))
            return None
        if not row:
            return None
        fm, body = row
        return WikiPage(
            rel=rel,
            frontmatter=fm if isinstance(fm, dict) else {},
            body=str(body or ""),
        )

    def walk(self) -> Iterator[WikiPage]:
        yielded: set[str] = set()
        if self._persistent:
            for page in self._walk_pg():
                yielded.add(page.rel)
                yield page
        if not self.root.exists():
            return
        for p in sorted(self.root.rglob("*.md")):
            rel = p.relative_to(self.root).as_posix()
            if rel in yielded:
                continue
            page = self._read_disk(rel)
            if page is not None:
                yield page

    def _walk_pg(self) -> Iterator[WikiPage]:
        if self.settings is None:
            return
        try:
            with (
                psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    """
                    SELECT rel, frontmatter, body
                      FROM agentcore_wiki_pages
                     WHERE project_id = %s
                       AND branch = %s
                  ORDER BY rel ASC
                    """,
                    (self.project, self.branch),
                )
                rows = cur.fetchall() or []
        except Exception as exc:
            log.warning("wiki.page_walk_failed", error=str(exc))
            return
        for rel, fm, body in rows:
            yield WikiPage(
                rel=str(rel),
                frontmatter=fm if isinstance(fm, dict) else {},
                body=str(body or ""),
            )

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
            # Still mirror to Postgres so a disk-only page backfills on the
            # next no-op curator write.
            self._write_pg(new)
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
        self._write_pg(new)
        return True

    def _write_pg(self, page: WikiPage) -> None:
        if not self._persistent or self.settings is None:
            return
        with suppress(Exception):
            with (
                psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    """
                    INSERT INTO agentcore_wiki_pages
                      (project_id, branch, rel, title, frontmatter, body,
                       content_hash, updated_at)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, now())
                    ON CONFLICT (project_id, branch, rel)
                    DO UPDATE SET
                      title = EXCLUDED.title,
                      frontmatter = EXCLUDED.frontmatter,
                      body = EXCLUDED.body,
                      content_hash = EXCLUDED.content_hash,
                      updated_at = now()
                    """,
                    (
                        self.project,
                        self.branch,
                        page.rel,
                        page.title,
                        json.dumps(page.frontmatter),
                        page.body,
                        str(page.frontmatter.get("content_hash") or ""),
                    ),
                )

    def delete(self, rel: str) -> bool:
        p = self.page_path(rel)
        disk_deleted = False
        if p.exists():
            p.unlink()
            disk_deleted = True
        db_deleted = self._delete_pg(rel)
        return disk_deleted or db_deleted

    def _delete_pg(self, rel: str) -> bool:
        if not self._persistent or self.settings is None:
            return False
        try:
            with (
                psycopg.connect(self.settings.pg_dsn, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    """
                    DELETE FROM agentcore_wiki_pages
                     WHERE project_id = %s
                       AND branch = %s
                       AND rel = %s
                    """,
                    (self.project, self.branch, rel),
                )
                return (cur.rowcount or 0) > 0
        except Exception as exc:
            log.warning("wiki.page_delete_failed", rel=rel, error=str(exc))
            return False

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
        """The pgvector collection this storage's pages should be indexed under.

        Routed through `wiki.naming.wiki_collection` so the format is
        constructed identically across the codebase — no string-format
        drift between writers and readers, no chance of a typo
        crossing tenants.
        """
        from agentcore.wiki.naming import wiki_collection

        return wiki_collection(self.project, self.branch)
