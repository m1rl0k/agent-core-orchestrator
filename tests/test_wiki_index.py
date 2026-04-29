"""WikiIndex: collection naming, embed+upsert, search ordering, branch isolation.

We mock Embedder + VectorStore so these tests run without pgvector or
fastembed weights.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentcore.memory.vector import Hit
from agentcore.wiki.index import WikiHit, WikiIndex
from agentcore.wiki.storage import WikiPage, WikiStorage


class _FakeEmbedder:
    """Deterministic embeddings: a 4-dim vector keyed off a string hash."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            h = hash(t)
            out.append([
                ((h >> 0) & 0xFFFF) / 65535.0,
                ((h >> 16) & 0xFFFF) / 65535.0,
                ((h >> 32) & 0xFFFF) / 65535.0,
                ((h >> 48) & 0xFFFF) / 65535.0,
            ])
        return out


class _FakeVector:
    """In-memory stand-in for VectorStore — captures upserts, returns deterministic hits."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[tuple[str, str, dict[str, Any], list[float]]]]] = []
        self.deleted: list[tuple[str, str]] = []
        self.canned_hits: dict[str, list[Hit]] = {}

    def upsert(
        self,
        collection: str,
        items: list[tuple[str, str, dict[str, Any], list[float]]],
    ) -> int:
        self.calls.append((collection, items))
        return len(items)

    def search(self, collection: str, query_emb: list[float], k: int = 8) -> list[Hit]:
        return self.canned_hits.get(collection, [])[:k]

    def delete_by_ref(self, collection: str, ref: str) -> int:
        self.deleted.append((collection, ref))
        return 1


@pytest.mark.asyncio
async def test_collection_name_is_branch_scoped(tmp_path: Path) -> None:
    s_main = WikiStorage(tmp_path, "proj", "main")
    s_dev = WikiStorage(tmp_path, "proj", "feature-x")
    idx_main = WikiIndex(s_main, _FakeEmbedder(), _FakeVector())  # type: ignore[arg-type]
    idx_dev = WikiIndex(s_dev, _FakeEmbedder(), _FakeVector())  # type: ignore[arg-type]
    assert idx_main.collection_name() == "wiki:proj:main"
    assert idx_dev.collection_name() == "wiki:proj:feature-x"
    assert idx_main.collection_name() != idx_dev.collection_name()


@pytest.mark.asyncio
async def test_upsert_writes_to_correct_collection(tmp_path: Path) -> None:
    storage = WikiStorage(tmp_path, "proj", "main")
    vec = _FakeVector()
    idx = WikiIndex(storage, _FakeEmbedder(), vec)  # type: ignore[arg-type]
    page = WikiPage(
        rel="modules/foo.md",
        frontmatter={"title": "Foo module", "sources": ["src/foo.py"]},
        body="Foo summary.",
    )
    ok = await idx.upsert_page(page)
    assert ok is True
    assert len(vec.calls) == 1
    coll, items = vec.calls[0]
    assert coll == "wiki:proj:main"
    [(ref, content, meta, emb)] = items
    assert ref == "wiki:proj:main:modules/foo.md"
    assert "Foo summary" in content
    assert meta["rel"] == "modules/foo.md"
    assert meta["title"] == "Foo module"
    assert len(emb) == 4  # _FakeEmbedder dim


@pytest.mark.asyncio
async def test_upsert_no_op_when_not_ready(tmp_path: Path) -> None:
    storage = WikiStorage(tmp_path, "proj", "main")
    idx = WikiIndex(storage, embedder=None, vector=None)
    assert idx.is_ready is False
    page = WikiPage(rel="x.md", frontmatter={}, body="hi")
    assert await idx.upsert_page(page) is False


@pytest.mark.asyncio
async def test_search_passes_through_hits(tmp_path: Path) -> None:
    storage = WikiStorage(tmp_path, "proj", "main")
    vec = _FakeVector()
    vec.canned_hits["wiki:proj:main"] = [
        Hit(
            ref="wiki:proj:main:modules/foo.md",
            content="Foo body",
            score=0.91,
            metadata={"rel": "modules/foo.md", "title": "Foo module"},
        ),
        Hit(
            ref="wiki:proj:main:modules/bar.md",
            content="Bar body",
            score=0.42,
            metadata={"rel": "modules/bar.md", "title": "Bar module"},
        ),
    ]
    idx = WikiIndex(storage, _FakeEmbedder(), vec)  # type: ignore[arg-type]
    hits = await idx.search("anything")
    assert len(hits) == 2
    assert all(isinstance(h, WikiHit) for h in hits)
    assert hits[0].rel == "modules/foo.md"
    assert hits[0].score == pytest.approx(0.91)


@pytest.mark.asyncio
async def test_search_isolated_per_branch(tmp_path: Path) -> None:
    """A search on branch A's index never returns branch B's hits."""
    s_main = WikiStorage(tmp_path, "proj", "main")
    s_dev = WikiStorage(tmp_path, "proj", "dev")
    vec = _FakeVector()
    vec.canned_hits["wiki:proj:main"] = [
        Hit(
            ref="wiki:proj:main:modules/foo.md",
            content="main",
            score=0.9,
            metadata={"rel": "modules/foo.md", "title": "main page"},
        )
    ]
    vec.canned_hits["wiki:proj:dev"] = [
        Hit(
            ref="wiki:proj:dev:modules/foo.md",
            content="dev",
            score=0.9,
            metadata={"rel": "modules/foo.md", "title": "dev page"},
        )
    ]
    idx_main = WikiIndex(s_main, _FakeEmbedder(), vec)  # type: ignore[arg-type]
    idx_dev = WikiIndex(s_dev, _FakeEmbedder(), vec)  # type: ignore[arg-type]
    main_hits = await idx_main.search("foo")
    dev_hits = await idx_dev.search("foo")
    assert main_hits[0].title == "main page"
    assert dev_hits[0].title == "dev page"


@pytest.mark.asyncio
async def test_delete_removes_from_correct_collection(tmp_path: Path) -> None:
    storage = WikiStorage(tmp_path, "proj", "main")
    vec = _FakeVector()
    idx = WikiIndex(storage, _FakeEmbedder(), vec)  # type: ignore[arg-type]
    await idx.delete_page("modules/gone.md")
    assert vec.deleted == [("wiki:proj:main", "wiki:proj:main:modules/gone.md")]
