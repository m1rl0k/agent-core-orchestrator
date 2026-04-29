"""Pydantic models for the unified agent.md schema.

Foreign tools (Claude Code, IDEs that read AGENTS.md) ignore unknown frontmatter
keys, so a single AgentSpec serializes back to a file every consumer can use.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ProviderName = Literal["anthropic", "bedrock", "azure_openai", "zai"]


class IOField(BaseModel):
    """Single input or output slot on an agent contract."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str = Field(
        description="Either a primitive (`string`, `int`, `bool`, `dict`, `list`) "
        "or the name of a class registered in `agentcore.contracts.domain`.",
    )
    required: bool = True
    description: str = ""


class Contract(BaseModel):
    """Bounded contract: what the agent accepts, emits, and who it can talk to."""

    model_config = ConfigDict(extra="forbid")

    inputs: list[IOField] = Field(default_factory=list)
    outputs: list[IOField] = Field(default_factory=list)
    accepts_handoff_from: list[str] = Field(default_factory=lambda: ["user"])
    delegates_to: list[str] = Field(default_factory=list)
    sla_seconds: int | None = None


class Soul(BaseModel):
    """Persona / identity layer (SOUL.md-inspired)."""

    model_config = ConfigDict(extra="forbid")

    role: str
    voice: str = ""
    values: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)


class KnowledgeBinding(BaseModel):
    """Which slices of the memory layer an agent has access to."""

    model_config = ConfigDict(extra="forbid")

    rag_collections: list[str] = Field(default_factory=list)
    graph_communities: list[str] = Field(default_factory=list)
    code_scopes: list[str] = Field(default_factory=list)


class ModelConfig(BaseModel):
    """Provider routing for this agent's LLM calls."""

    model_config = ConfigDict(extra="forbid")

    provider: ProviderName = "anthropic"
    model: str = "claude-sonnet-4-6"
    temperature: float = 0.2
    max_tokens: int = 4096

    @field_validator("temperature")
    @classmethod
    def _temp_range(cls, v: float) -> float:
        if not 0.0 <= v <= 2.0:
            raise ValueError("temperature must be in [0, 2]")
        return v


class AgentSpec(BaseModel):
    """Top-level agent definition. One per `*.agent.md` file."""

    model_config = ConfigDict(extra="ignore")

    # Claude Code / AGENTS.md compatibility
    name: str
    description: str
    tools: list[str] = Field(default_factory=list)

    # Extensions
    llm: ModelConfig = Field(default_factory=ModelConfig)
    soul: Soul
    contract: Contract = Field(default_factory=Contract)
    knowledge: KnowledgeBinding = Field(default_factory=KnowledgeBinding)
    # Pre-LLM executors. Names registered in `agentcore.runtime.executors`
    # (e.g. "pytest"). The runtime runs each before the LLM call and
    # merges its structured result into the handoff payload, grounding
    # the LLM in real tool output instead of inviting it to invent.
    executors: list[str] = Field(default_factory=list)

    # The body of the markdown file becomes the system prompt.
    system_prompt: str = ""

    # Provenance (filled by parser, not by user)
    source_path: str | None = None
    checksum: str | None = None

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, v: str) -> str:
        if not v.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"agent name must be a slug, got {v!r}")
        return v
