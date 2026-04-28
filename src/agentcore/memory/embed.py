"""In-process embeddings via fastembed.

Defaults to Nomic-embed-text-v1.5 (768-dim). The model is small enough to
ship inside the orchestrator process — no separate Ollama / Nomic Atlas
server required. The first call lazily downloads weights into the
fastembed cache; subsequent calls are local.

If `fastembed` isn't installed (e.g. in a thin install of the package)
`Embedder` raises on construction so the failure is clear and early.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agentcore.settings import Settings, get_settings

EMBED_DIM = 768
DEFAULT_MODEL = "nomic-ai/nomic-embed-text-v1.5"


class Embedder:
    """Thin async wrapper around fastembed's TextEmbedding."""

    def __init__(self, settings: Settings | None = None, *, model: str | None = None) -> None:
        try:
            from fastembed import TextEmbedding
        except Exception as exc:  # pragma: no cover - import error surfaced clearly
            raise RuntimeError(
                "fastembed is not installed. Install with `pip install fastembed`."
            ) from exc

        self.settings = settings or get_settings()
        self.model_name = model or self.settings.embed_model or DEFAULT_MODEL
        self._engine: Any = TextEmbedding(model_name=self.model_name)

    async def aclose(self) -> None:
        # fastembed has no resources to release; defined for symmetry with
        # any future HTTP-backed implementation.
        return None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # fastembed is sync + CPU-bound. Push to a thread so we don't block
        # the event loop.
        return await asyncio.to_thread(self._embed_sync, texts)

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, vec)) for vec in self._engine.embed(texts)]
