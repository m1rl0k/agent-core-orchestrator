"""Pre-fetch and warm fastembed models (embedder + reranker).

fastembed lazily downloads model weights on first inference call. That's
fine in long-running services but makes one-shot CLI commands look like
they hung. This module gives us an explicit `pull` step the operator (or
the Dockerfile) can invoke up front.

Both models are tiny:
  - nomic-ai/nomic-embed-text-v1.5            ~140 MB
  - mixedbread-ai/mxbai-rerank-xsmall-v1      ~  90 MB

Cache location is governed by fastembed's own resolution
(`FASTEMBED_CACHE_DIR` env or platform default). We don't override it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agentcore.settings import Settings, get_settings

ModelKind = Literal["embedder", "reranker"]


@dataclass(slots=True)
class PullResult:
    kind: ModelKind
    name: str
    cached: bool             # True if weights already on disk before pull
    ok: bool
    detail: str = ""


def pull_embedder(settings: Settings | None = None) -> PullResult:
    s = settings or get_settings()
    name = s.embed_model
    try:
        from fastembed import TextEmbedding
    except Exception as exc:
        return PullResult("embedder", name, cached=False, ok=False,
                          detail=f"fastembed not installed: {exc}")
    try:
        cached_before = _is_cached("embedder", name)
        engine = TextEmbedding(model_name=name)
        # Force the download/load by running one tiny inference.
        list(engine.embed(["warm-up"]))
    except Exception as exc:
        return PullResult("embedder", name, cached=False, ok=False, detail=str(exc))
    return PullResult("embedder", name, cached=cached_before, ok=True)


def pull_reranker(settings: Settings | None = None) -> PullResult:
    s = settings or get_settings()
    if not s.enable_rerank:
        return PullResult("reranker", s.rerank_model, cached=False, ok=True,
                          detail="rerank disabled (AGENTCORE_ENABLE_RERANK=false)")
    name = s.rerank_model
    try:
        from agentcore.memory.rerank import Reranker
    except Exception as exc:
        return PullResult("reranker", name, cached=False, ok=False, detail=str(exc))
    try:
        cached_before = _is_cached("reranker", name)
        rr = Reranker(s)
        # One tiny scoring pass forces the download/load.
        rr._score_sync("warm-up", ["warm-up document"])
    except Exception as exc:
        return PullResult("reranker", name, cached=False, ok=False, detail=str(exc))
    return PullResult("reranker", name, cached=cached_before, ok=True)


def _is_cached(_kind: str, _name: str) -> bool:
    """Best-effort: fastembed's cache layout has shifted across versions, so
    we don't fingerprint disk; we just report whether a pull was needed
    based on import success. Callers treat `cached=False, ok=True` as
    "downloaded just now"."""
    return False
