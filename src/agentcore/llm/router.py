"""Provider-agnostic LLM router.

Each AgentSpec carries a `ModelConfig` declaring `provider` + `model`. The
router picks the matching client and issues a single chat completion. No
streaming, no tool-calling indirection — that's the agent runtime's job.

Providers are imported lazily so a missing optional SDK doesn't crash startup.
Clients are cached per-provider on the router instance to avoid the cost of
re-establishing connection pools on every call. Non-Bedrock providers use a
small inline retry policy (429 + 5xx) with exponential backoff; boto3 owns
its own retries for Bedrock.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Literal

import httpx

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
        self._clients: dict[str, Any] = {}

    # ---- shared knobs --------------------------------------------------

    @property
    def _httpx_timeout(self) -> httpx.Timeout:
        # connect/read/write all bounded; keepalive idle is short.
        t = float(self.settings.llm_timeout_seconds)
        return httpx.Timeout(t, connect=min(10.0, t))

    # ---- cached clients ------------------------------------------------

    def _anthropic_client(self) -> Any:
        if "anthropic" in self._clients:
            return self._clients["anthropic"]
        if not self.settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(
            api_key=self.settings.anthropic_api_key,
            timeout=self._httpx_timeout,
        )
        self._clients["anthropic"] = client
        return client

    def _azure_client(self) -> Any:
        if "azure_openai" in self._clients:
            return self._clients["azure_openai"]
        if not (self.settings.azure_openai_api_key and self.settings.azure_openai_endpoint):
            raise RuntimeError("AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT must be set")
        from openai import AsyncAzureOpenAI

        client = AsyncAzureOpenAI(
            api_key=self.settings.azure_openai_api_key,
            azure_endpoint=self.settings.azure_openai_endpoint,
            api_version=self.settings.azure_openai_api_version,
            timeout=self._httpx_timeout,
        )
        self._clients["azure_openai"] = client
        return client

    def _zai_client(self) -> Any:
        if "zai" in self._clients:
            return self._clients["zai"]
        if not self.settings.zai_api_key:
            raise RuntimeError("ZAI_API_KEY is not set")
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=self.settings.zai_api_key,
            base_url=self.settings.zai_base_url,
            timeout=self._httpx_timeout,
        )
        self._clients["zai"] = client
        return client

    # ---- resolution ----------------------------------------------------

    def resolve_config(self, cfg: ModelConfig) -> ModelConfig:
        """Pick the actual provider for this call.

        If the agent's declared provider has credentials, use it as-is. Otherwise
        fall back to the first configured provider from `Settings.provider_priority`
        (z.ai first by default), swapping in that provider's default model since
        model identifiers don't translate across vendors. Returns the original
        config when nothing is configured so the caller still sees a clear
        "key not set" error from the chosen provider.
        """
        if self.settings.provider_has_creds(cfg.provider):
            return cfg
        preferred = self.settings.preferred_provider()
        if preferred is None or preferred == cfg.provider:
            return cfg
        fallback_model = self.settings.default_model_for(preferred) or cfg.model
        return cfg.model_copy(update={"provider": preferred, "model": fallback_model})

    # ---- public --------------------------------------------------------

    async def complete(
        self,
        messages: list[ChatMessage],
        model_config: ModelConfig,
    ) -> LLMResponse:
        model_config = self.resolve_config(model_config)
        provider = model_config.provider
        if provider == "anthropic":
            return await _with_retry(
                self.settings,
                lambda: _call_anthropic(messages, model_config, self._anthropic_client()),
            )
        if provider == "bedrock":
            # boto3 has its own retry policy; don't double up.
            return await _call_bedrock(messages, model_config, self.settings)
        if provider == "azure_openai":
            return await _with_retry(
                self.settings,
                lambda: _call_azure_openai(messages, model_config, self._azure_client()),
            )
        if provider == "zai":
            return await _with_retry(
                self.settings,
                lambda: _call_zai(messages, model_config, self._zai_client()),
            )
        raise ValueError(f"unknown LLM provider: {provider!r}")


# ---------------------------------------------------------------------------
# Retry helper — bounded, exponential-with-jitter, only on 429 / 5xx.
# ---------------------------------------------------------------------------


def _is_retryable(exc: BaseException) -> bool:
    """Decide whether to retry. Conservative: only known-transient classes."""
    # Any httpx timeout / network blip.
    if isinstance(exc, httpx.TimeoutException | httpx.NetworkError | httpx.RemoteProtocolError):
        return True
    # SDK-level rate-limit / status-code errors. We dodge SDK imports here and
    # poke at attributes the openai/anthropic SDKs both expose.
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    return isinstance(status, int) and (status == 429 or 500 <= status < 600)


async def _with_retry(settings: Settings, call):  # type: ignore[no-untyped-def]
    """Call `call()` with bounded retries on transient errors."""
    attempts = max(1, int(settings.llm_max_retries))
    delay = 0.5
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await call()
        except BaseException as exc:
            last_exc = exc
            if not _is_retryable(exc) or attempt == attempts - 1:
                raise
            jitter = random.uniform(0, delay / 2)
            await asyncio.sleep(min(delay + jitter, 8.0))
            delay = min(delay * 2, 8.0)
    # Unreachable, but keeps type-checkers happy.
    assert last_exc is not None
    raise last_exc


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
    messages: list[ChatMessage], cfg: ModelConfig, client: Any
) -> LLMResponse:
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
    import os

    import boto3

    # If the operator set a Bedrock API key in agentcore's settings, export it
    # so boto3's default credential chain finds it. Idempotent.
    if settings.bedrock_api_key and not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        os.environ["AWS_BEARER_TOKEN_BEDROCK"] = settings.bedrock_api_key

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
    messages: list[ChatMessage], cfg: ModelConfig, client: Any
) -> LLMResponse:
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
    messages: list[ChatMessage], cfg: ModelConfig, client: Any
) -> LLMResponse:
    resp = await client.chat.completions.create(
        model=cfg.model,
        messages=[{"role": m.role, "content": m.content} for m in messages],
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
    )
    text = resp.choices[0].message.content or ""
    return LLMResponse(text=text, provider="zai", model=cfg.model, raw=resp)
