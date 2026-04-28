"""Provider-agnostic LLM router.

Each AgentSpec carries a `ModelConfig` declaring `provider` + `model`. The
router picks the matching client and issues a single chat completion. No
streaming, no tool-calling indirection — that's the agent runtime's job.

Providers are imported lazily so a missing optional SDK doesn't crash startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from agentcore.settings import Settings, get_settings
from agentcore.spec.models import ModelConfig

Role = Literal["system", "user", "assistant"]


@dataclass(slots=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(slots=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    raw: Any = None


class LLMRouter:
    """Single entry point: `router.complete(messages, model_config)`."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def complete(
        self,
        messages: list[ChatMessage],
        model_config: ModelConfig,
    ) -> LLMResponse:
        provider = model_config.provider
        if provider == "anthropic":
            return await _call_anthropic(messages, model_config, self.settings)
        if provider == "bedrock":
            return await _call_bedrock(messages, model_config, self.settings)
        if provider == "azure_openai":
            return await _call_azure_openai(messages, model_config, self.settings)
        if provider == "zai":
            return await _call_zai(messages, model_config, self.settings)
        raise ValueError(f"unknown LLM provider: {provider!r}")


# ---------------------------------------------------------------------------
# Provider helpers — split sys vs non-sys for SDKs that need it.
# ---------------------------------------------------------------------------


def _split_system(messages: list[ChatMessage]) -> tuple[str, list[dict[str, str]]]:
    sys_parts = [m.content for m in messages if m.role == "system"]
    rest = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
    return "\n\n".join(sys_parts), rest


# ---------------------------------------------------------------------------
# Anthropic (native SDK)
# ---------------------------------------------------------------------------


async def _call_anthropic(
    messages: list[ChatMessage], cfg: ModelConfig, settings: Settings
) -> LLMResponse:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    system, rest = _split_system(messages)
    resp = await client.messages.create(
        model=cfg.model,
        system=system or None,
        messages=rest,  # type: ignore[arg-type]
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
    )
    text = "".join(getattr(b, "text", "") for b in resp.content)
    return LLMResponse(text=text, provider="anthropic", model=cfg.model, raw=resp)


# ---------------------------------------------------------------------------
# AWS Bedrock (Claude on Bedrock via converse API)
# ---------------------------------------------------------------------------


async def _call_bedrock(
    messages: list[ChatMessage], cfg: ModelConfig, settings: Settings
) -> LLMResponse:
    import asyncio

    import boto3

    def _invoke() -> dict:
        kwargs: dict[str, Any] = {"region_name": settings.aws_region}
        if settings.aws_profile:
            session = boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region)
            client = session.client("bedrock-runtime")
        else:
            client = boto3.client("bedrock-runtime", **kwargs)

        system, rest = _split_system(messages)
        bedrock_messages = [
            {"role": m["role"], "content": [{"text": m["content"]}]} for m in rest
        ]
        return client.converse(
            modelId=cfg.model,
            messages=bedrock_messages,
            system=[{"text": system}] if system else [],
            inferenceConfig={"maxTokens": cfg.max_tokens, "temperature": cfg.temperature},
        )

    resp = await asyncio.to_thread(_invoke)
    blocks = resp.get("output", {}).get("message", {}).get("content", [])
    text = "".join(b.get("text", "") for b in blocks)
    return LLMResponse(text=text, provider="bedrock", model=cfg.model, raw=resp)


# ---------------------------------------------------------------------------
# Azure OpenAI
# ---------------------------------------------------------------------------


async def _call_azure_openai(
    messages: list[ChatMessage], cfg: ModelConfig, settings: Settings
) -> LLMResponse:
    if not (settings.azure_openai_api_key and settings.azure_openai_endpoint):
        raise RuntimeError("AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT must be set")
    from openai import AsyncAzureOpenAI

    client = AsyncAzureOpenAI(
        api_key=settings.azure_openai_api_key,
        azure_endpoint=settings.azure_openai_endpoint,
        api_version=settings.azure_openai_api_version,
    )
    resp = await client.chat.completions.create(
        model=cfg.model,  # Azure uses deployment name here
        messages=[{"role": m.role, "content": m.content} for m in messages],
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
    )
    text = resp.choices[0].message.content or ""
    return LLMResponse(text=text, provider="azure_openai", model=cfg.model, raw=resp)


# ---------------------------------------------------------------------------
# z.ai (Zhipu) — OpenAI-compatible HTTP API
# ---------------------------------------------------------------------------


async def _call_zai(
    messages: list[ChatMessage], cfg: ModelConfig, settings: Settings
) -> LLMResponse:
    if not settings.zai_api_key:
        raise RuntimeError("ZAI_API_KEY is not set")
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.zai_api_key, base_url=settings.zai_base_url)
    resp = await client.chat.completions.create(
        model=cfg.model,
        messages=[{"role": m.role, "content": m.content} for m in messages],
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
    )
    text = resp.choices[0].message.content or ""
    return LLMResponse(text=text, provider="zai", model=cfg.model, raw=resp)
