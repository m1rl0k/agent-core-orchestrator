"""Router dispatch logic — verify provider plumbing without real network calls."""

from __future__ import annotations

import pytest
from pydantic_settings import SettingsConfigDict

from agentcore.llm.router import LLMRouter
from agentcore.settings import Settings
from agentcore.spec.models import ModelConfig


class _IsolatedSettings(Settings):
    """Settings that ignore any host `.env` so tests stay deterministic."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)


@pytest.mark.asyncio
async def test_unknown_provider_raises() -> None:
    router = LLMRouter(_IsolatedSettings())
    cfg = ModelConfig(provider="anthropic", model="claude-sonnet-4-6")
    cfg = cfg.model_copy(update={"provider": "made_up"})  # bypass Literal narrowing
    with pytest.raises(ValueError):
        await router.complete([], cfg)


@pytest.mark.asyncio
async def test_anthropic_requires_key() -> None:
    router = LLMRouter(_IsolatedSettings(ANTHROPIC_API_KEY=None))
    cfg = ModelConfig(provider="anthropic", model="claude-sonnet-4-6")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        await router.complete([], cfg)


@pytest.mark.asyncio
async def test_zai_requires_key() -> None:
    router = LLMRouter(_IsolatedSettings(ZAI_API_KEY=None))
    cfg = ModelConfig(provider="zai", model="glm-4")
    with pytest.raises(RuntimeError, match="ZAI_API_KEY"):
        await router.complete([], cfg)
