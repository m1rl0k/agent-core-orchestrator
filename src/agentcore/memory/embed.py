"""Embedding client.

Two providers, both Nomic-1.5 family:
  - "ollama"  — POST to http://OLLAMA_HOST/api/embed (recommended for dev)
  - "nomic"   — POST to https://api-atlas.nomic.ai/v1/embedding/text

The vector dimensionality for `nomic-embed-text:v1.5` is 768.
"""

from __future__ import annotations

from typing import Literal

import httpx

from agentcore.settings import Settings, get_settings

EMBED_DIM = 768


class Embedder:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.provider: Literal["ollama", "nomic"] = self.settings.embed_provider
        self.model = self.settings.embed_model
        self._client = httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.provider == "ollama":
            return await self._embed_ollama(texts)
        return await self._embed_nomic(texts)

    async def _embed_ollama(self, texts: list[str]) -> list[list[float]]:
        url = f"{self.settings.ollama_host.rstrip('/')}/api/embed"
        resp = await self._client.post(url, json={"model": self.model, "input": texts})
        resp.raise_for_status()
        data = resp.json()
        # Ollama returns either {"embeddings": [[...], ...]} or, for older
        # versions, {"embedding": [...]} for a single input.
        if "embeddings" in data:
            return data["embeddings"]
        return [data["embedding"]]

    async def _embed_nomic(self, texts: list[str]) -> list[list[float]]:
        if not self.settings.nomic_api_key:
            raise RuntimeError("NOMIC_API_KEY is not set")
        url = "https://api-atlas.nomic.ai/v1/embedding/text"
        headers = {"Authorization": f"Bearer {self.settings.nomic_api_key}"}
        resp = await self._client.post(
            url,
            headers=headers,
            json={"model": self.model, "texts": texts, "task_type": "search_document"},
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]
