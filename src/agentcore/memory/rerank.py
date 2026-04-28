"""Cross-encoder reranker (MixedBread mxbai-xsmall by default).

A reranker takes the top-k vector hits and rescores each (query, candidate)
pair with a small cross-encoder. mxbai-rerank-xsmall is ~33M params, runs
fast on CPU, and scores high on MTEB Rerank.

The reranker is optional: if `fastembed` doesn't expose a rerank surface
in the installed version (older releases) or the model can't be loaded,
the constructor raises and the caller can fall back to vector-only sort.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agentcore.settings import Settings, get_settings


class Reranker:
    """Score (query, document) pairs with a tiny cross-encoder."""

    def __init__(self, settings: Settings | None = None, *, model: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.model_name = model or self.settings.rerank_model
        self._engine: Any = self._load_engine()

    def _load_engine(self) -> Any:
        # fastembed exposes rerankers as either `Rerank` or
        # `LateInteractionTextEmbedding` depending on version. We try the
        # canonical `Rerank` first and tolerate naming drift.
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder  # type: ignore

            return TextCrossEncoder(model_name=self.model_name)
        except Exception:
            pass
        try:
            from fastembed import Rerank  # type: ignore

            return Rerank(model_name=self.model_name)
        except Exception as exc:
            raise RuntimeError(
                f"could not load reranker {self.model_name!r}; "
                "upgrade fastembed or pin a known-good cross-encoder model"
            ) from exc

    async def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        return await asyncio.to_thread(self._score_sync, query, documents)

    def _score_sync(self, query: str, documents: list[str]) -> list[float]:
        # Newer fastembed: `rerank(query, documents)` yields floats.
        if hasattr(self._engine, "rerank"):
            return [float(s) for s in self._engine.rerank(query, documents)]
        # Older: `compute_score(query, documents)`.
        if hasattr(self._engine, "compute_score"):
            return [float(s) for s in self._engine.compute_score(query, documents)]
        # Last-ditch: `score(...)`.
        if hasattr(self._engine, "score"):
            return [float(s) for s in self._engine.score(query, documents)]
        raise RuntimeError("reranker engine exposes no known scoring method")
