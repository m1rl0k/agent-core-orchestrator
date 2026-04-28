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
    host: str = Field("0.0.0.0", alias="AGENTCORE_HOST")
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

    # Embeddings
    embed_provider: Literal["ollama", "nomic"] = Field("ollama", alias="AGENTCORE_EMBED_PROVIDER")
    embed_model: str = Field("nomic-embed-text:v1.5", alias="AGENTCORE_EMBED_MODEL")
    ollama_host: str = Field("http://localhost:11434", alias="OLLAMA_HOST")
    nomic_api_key: str | None = Field(None, alias="NOMIC_API_KEY")

    # LLM providers
    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY")
    aws_region: str = Field("us-east-1", alias="AWS_REGION")
    aws_profile: str | None = Field(None, alias="AWS_PROFILE")
    azure_openai_api_key: str | None = Field(None, alias="AZURE_OPENAI_API_KEY")
    azure_openai_endpoint: str | None = Field(None, alias="AZURE_OPENAI_ENDPOINT")
    azure_openai_api_version: str = Field(
        "2024-08-01-preview", alias="AZURE_OPENAI_API_VERSION"
    )
    zai_api_key: str | None = Field(None, alias="ZAI_API_KEY")
    zai_base_url: str = Field("https://api.z.ai/api/paas/v4", alias="ZAI_BASE_URL")

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
