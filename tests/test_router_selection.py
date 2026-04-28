"""Router dispatch logic — verify provider plumbing without real network calls."""

from __future__ import annotations

import pytest

from agentcore.llm.router import LLMRouter
from agentcore.settings import Settings
from agentcore.spec.models import ModelConfig


@pytest.mark.asyncio
async def test_unknown_provider_raises() -> None:
    router = LLMRouter(Settings(_env_file=None))  # type: ignore[call-arg]
    cfg = ModelConfig(provider="anthropic", model="claude-sonnet-4-6")
    cfg = cfg.model_copy(update={"provider": "made_up"})  # bypass Literal narrowing
    with pytest.raises(ValueError):
        await router.complete([], cfg)


@pytest.mark.asyncio
async def test_anthropic_requires_key() -> None:
    settings = Settings(_env_file=None, ANTHROPIC_API_KEY=None)  # type: ignore[call-arg]
    router = LLMRouter(settings)
    cfg = ModelConfig(provider="anthropic", model="claude-sonnet-4-6")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        await router.complete([], cfg)


@pytest.mark.asyncio
async def test_zai_requires_key() -> None:
    settings = Settings(_env_file=None, ZAI_API_KEY=None)  # type: ignore[call-arg]
    router = LLMRouter(settings)
    cfg = ModelConfig(provider="zai", model="glm-4")
    with pytest.raises(RuntimeError, match="ZAI_API_KEY"):
        await router.complete([], cfg)
