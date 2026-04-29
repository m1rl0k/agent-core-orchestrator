"""pgvector indexer for the wiki.

Every wiki page write flows through `WikiIndex.upsert_page()` which:
  1. Composes an embedding-friendly representation (title + body + key sources)
  2. Embeds it in-process via the existing `Embedder`
  3. UPSERTs into the pgvector store under collection `wiki:<project>:<branch>`

Search is `WikiIndex.search()` — a thin wrapper over `VectorStore.search` that
returns the same `Hit` shape the `HybridRetriever` already understands.

The indexer is tolerant: if pgvector or the embedder isn't available, every
operation is a no-op so the curator can still write pages to disk and the
orchestrator stays up.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from agentcore.memory.embed import Embedder
    from agentcore.memory.vector import Hit, VectorStore
    from agentcore.wiki.storage import WikiPage, WikiStorage

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class WikiHit:
    """One retrieval hit from the wiki collection (pre-context-bundle shape)."""

    rel: str
    title: str
    excerpt: str
    score: float


class WikiIndex:
    """pgvector indexer + searcher for one (project, branch) wiki."""

    def __init__(
        self,
        storage: WikiStorage,
        embedder: Embedder | None,
        vector: VectorStore | None,
    ) -> None:
        self.storage = storage
        self.embedder = embedder
        self.vector = vector

    @property
    def is_ready(self) -> bool:
        return self.embedder is not None and self.vector is not None

    def collection_name(self) -> str:
        return self.storage.collection_name()

    # ---- write --------------------------------------------------------

    async def upsert_page(self, page: WikiPage) -> bool:
        """Embed + upsert one page. Returns True on success, False if not ready."""
        if not self.is_ready:
            return False
        text = self._embed_text(page)
        if not text.strip():
            return False
        try:
            [emb] = await self.embedder.embed([text])
        except Exception as exc:
            log.warning("wiki.embed_failed", rel=page.rel, error=str(exc))
            return False
        meta = {
            "rel": page.rel,
            "title": page.title,
            "sources": page.sources[:32],
            "status": page.frontmatter.get("status", "drafting"),
        }
        from agentcore.wiki.naming import wiki_ref

        ref = wiki_ref(self.storage.project, self.storage.branch, page.rel)
        try:
            self.vector.upsert(self.collection_name(), [(ref, text, meta, emb)])
        except Exception as exc:
            log.warning("wiki.upsert_failed", rel=page.rel, error=str(exc))
            return False
        return True

    async def delete_page(self, rel: str) -> None:
        """Remove a page from the index. Best-effort, no-op if the wiki tables
        aren't there."""
        if not self.is_ready:
            return
        from agentcore.wiki.naming import wiki_ref

        ref = wiki_ref(self.storage.project, self.storage.branch, rel)
        try:
            self.vector.delete_by_ref(self.collection_name(), ref)
        except Exception as exc:
            log.info("wiki.delete_failed", rel=rel, error=str(exc))

    async def rebuild_all(self) -> int:
        """Re-embed every page on disk. Returns count of pages indexed."""
        if not self.is_ready:
            return 0
        n = 0
        for page in self.storage.walk():
            if await self.upsert_page(page):
                n += 1
        return n

    # ---- search -------------------------------------------------------

    async def search(self, query: str, k: int = 8) -> list[WikiHit]:
        if not self.is_ready or not query.strip():
            return []
        try:
            [emb] = await self.embedder.embed([query])
        except Exception as exc:
            log.warning("wiki.search_embed_failed", error=str(exc))
            return []
        try:
            hits: Iterable[Hit] = self.vector.search(
                self.collection_name(), emb, k=k
            )
        except Exception as exc:
            log.warning("wiki.search_failed", error=str(exc))
            return []
        out: list[WikiHit] = []
        for h in hits:
            meta = h.metadata or {}
            out.append(
                WikiHit(
                    rel=str(meta.get("rel", "")),
                    title=str(meta.get("title", "")),
                    excerpt=h.content[:400],
                    score=float(h.score),
                )
            )
        return out

    # ---- helpers ------------------------------------------------------

    @staticmethod
    def _embed_text(page: WikiPage) -> str:
        """Compose what we actually embed.

        Title carries strong signal; sources help disambiguate; body is the
        substance. We cap to keep embedding cost predictable on large pages.
        """
        parts: list[str] = []
        if page.title:
            parts.append(f"# {page.title}")
        if page.sources:
            parts.append("Sources: " + ", ".join(page.sources[:16]))
        parts.append(page.body[:8000])
        return "\n\n".join(parts).strip()
