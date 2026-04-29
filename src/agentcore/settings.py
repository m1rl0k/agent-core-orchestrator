"""Centralised settings, loaded from environment via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # Server
    host: str = Field("127.0.0.1", alias="AGENTCORE_HOST")
    api_token: str | None = Field(None, alias="AGENTCORE_API_TOKEN")
    port: int = Field(8088, alias="AGENTCORE_PORT")
    log_level: str = Field("info", alias="AGENTCORE_LOG_LEVEL")

    # Agent library
    agents_dir: Path = Field(Path("./agents"), alias="AGENTCORE_AGENTS_DIR")

    # Postgres
    pg_host: str = Field("localhost", alias="PGHOST")
    pg_port: int = Field(5432, alias="PGPORT")
    pg_database: str = Field("agentcore", alias="PGDATABASE")
    pg_user: str = Field("agentcore", alias="PGUSER")
    pg_password: str = Field("agentcore", alias="PGPASSWORD")

    @property
    def pg_dsn(self) -> str:
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )

    # Embeddings (in-process via fastembed; Nomic-1.5 default).
    embed_model: str = Field(
        "nomic-ai/nomic-embed-text-v1.5", alias="AGENTCORE_EMBED_MODEL"
    )

    # Reranking (optional; tiny MixedBread cross-encoder via fastembed).
    enable_rerank: bool = Field(True, alias="AGENTCORE_ENABLE_RERANK")
    rerank_model: str = Field(
        "mixedbread-ai/mxbai-rerank-xsmall-v1", alias="AGENTCORE_RERANK_MODEL"
    )

    # LLM providers
    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY")
    aws_region: str = Field("us-east-1", alias="AWS_REGION")
    aws_profile: str | None = Field(None, alias="AWS_PROFILE")
    # Bedrock now supports long-lived API keys via the AWS_BEARER_TOKEN_BEDROCK
    # env var. boto3 picks this up automatically when present in the
    # environment, so all we do is read it here and ensure it's exported.
    bedrock_api_key: str | None = Field(None, alias="AWS_BEARER_TOKEN_BEDROCK")
    azure_openai_api_key: str | None = Field(None, alias="AZURE_OPENAI_API_KEY")
    azure_openai_endpoint: str | None = Field(None, alias="AZURE_OPENAI_ENDPOINT")
    azure_openai_api_version: str = Field(
        "2024-08-01-preview", alias="AZURE_OPENAI_API_VERSION"
    )
    zai_api_key: str | None = Field(None, alias="ZAI_API_KEY")
    zai_base_url: str = Field("https://api.z.ai/api/paas/v4", alias="ZAI_BASE_URL")

    # LLM call timeout (applies to Anthropic, Azure, z.ai; boto3 owns Bedrock).
    llm_timeout_seconds: float = Field(60.0, alias="AGENTCORE_LLM_TIMEOUT_SECONDS")
    llm_max_retries: int = Field(3, alias="AGENTCORE_LLM_MAX_RETRIES")

    # Provider priority — comma-separated list. The orchestrator picks the first
    # provider in this list whose credentials are populated when an agent's
    # declared provider is not configured. z.ai is preferred by default; flip
    # the list if you want a different fallback order.
    provider_priority: str = Field(
        "zai,anthropic,bedrock,azure_openai", alias="AGENTCORE_PROVIDER_PRIORITY"
    )

    # Per-provider fallback model (used only when we resolve to a different
    # provider than the agent declared, since model identifiers don't translate
    # across vendors). Override per-deployment as needed.
    default_model_anthropic: str = Field(
        "claude-sonnet-4-6", alias="AGENTCORE_DEFAULT_MODEL_ANTHROPIC"
    )
    default_model_bedrock: str = Field(
        # Kimi K2 Thinking — frontier reasoning + coding model. Tops SWE-Bench
        # Verified / LiveCodeBench v6 in the K2 family; chain-of-thought is
        # stripped at the router boundary so JSON-only contracts stay clean.
        # Per-agent overrides via `llm.model` in each agent.md still apply.
        "moonshot.kimi-k2-thinking",
        alias="AGENTCORE_DEFAULT_MODEL_BEDROCK",
    )
    default_model_azure_openai: str = Field(
        "gpt-4o", alias="AGENTCORE_DEFAULT_MODEL_AZURE_OPENAI"
    )
    default_model_zai: str = Field("glm-4.6", alias="AGENTCORE_DEFAULT_MODEL_ZAI")

    # ------------------------------------------------------------------
    # Provider-resolution helpers
    # ------------------------------------------------------------------

    def provider_has_creds(self, name: str) -> bool:
        """True iff this provider has the env vars it needs to make a call."""
        if name == "anthropic":
            return bool(self.anthropic_api_key)
        if name == "azure_openai":
            return bool(self.azure_openai_api_key and self.azure_openai_endpoint)
        if name == "zai":
            return bool(self.zai_api_key)
        if name == "bedrock":
            # Bedrock now supports long-lived API keys (Bearer token), and
            # boto3 also accepts the standard AWS chain (profile / IAM / SSO).
            # We treat any of these as a creds signal.
            import os

            return bool(
                self.bedrock_api_key
                or self.aws_profile
                or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
                or os.environ.get("AWS_ACCESS_KEY_ID")
                or os.environ.get("AWS_SESSION_TOKEN")
            )
        return False

    def active_providers(self) -> list[str]:
        """Configured providers, in declared priority order."""
        order = [p.strip() for p in self.provider_priority.split(",") if p.strip()]
        return [p for p in order if self.provider_has_creds(p)]

    def preferred_provider(self) -> str | None:
        """First configured provider in priority order, or None if none are set."""
        active = self.active_providers()
        return active[0] if active else None

    def default_model_for(self, provider: str) -> str:
        return {
            "anthropic": self.default_model_anthropic,
            "bedrock": self.default_model_bedrock,
            "azure_openai": self.default_model_azure_openai,
            "zai": self.default_model_zai,
        }.get(provider, "")

    # ------------------------------------------------------------------
    # Optional integrations (host-credentialed). Each adapter rides on
    # the equivalent CLI already installed and authenticated on the host:
    #   github -> `gh`     (gh auth status)
    #   aws    -> `aws`    (aws sts get-caller-identity)
    #   azure  -> `az`     (az account show)
    # If the flag is true but the CLI is missing/unauth'd, the adapter is
    # listed as unavailable in `agentcore doctor` and never fires.
    # ------------------------------------------------------------------
    enable_github: bool = Field(False, alias="AGENTCORE_ENABLE_GITHUB")
    enable_aws: bool = Field(False, alias="AGENTCORE_ENABLE_AWS")
    enable_azure: bool = Field(False, alias="AGENTCORE_ENABLE_AZURE")
    enable_scheduled_scans: bool = Field(False, alias="AGENTCORE_ENABLE_SCHEDULED_SCANS")
    scheduled_scan_interval_seconds: int = Field(
        900, alias="AGENTCORE_SCHEDULED_SCAN_INTERVAL_SECONDS"
    )

    # Graphify: in-process code knowledge graph. Defaults on because it's a
    # native Python dep; turn off for pure offline / minimal installs.
    enable_graphify: bool = Field(True, alias="AGENTCORE_ENABLE_GRAPHIFY")
    graphify_repo_root: Path = Field(Path("."), alias="AGENTCORE_GRAPHIFY_REPO_ROOT")

    # Where shell-outs from agents (Bash tool, code-runner) execute.
    #   "host"   -> run directly on the orchestrator's host
    #   "docker" -> wrap in `docker exec` against AGENTCORE_SANDBOX_IMAGE
    # The orchestrator itself can run on host or in a container regardless.
    sandbox_mode: Literal["host", "docker"] = Field("host", alias="AGENTCORE_SANDBOX_MODE")
    sandbox_image: str = Field("python:3.13-slim", alias="AGENTCORE_SANDBOX_IMAGE")

    # Multi-project: a single orchestrator can serve N projects by having
    # callers POST to /run with their own AGENTCORE_AGENTS_DIR-pointed
    # registry, or by running multiple orchestrator instances per project
    # (each with its own .env). Both modes work.
    project_name: str = Field("default", alias="AGENTCORE_PROJECT_NAME")


@lru_cache
def get_settings() -> Settings:
    return Settings()
