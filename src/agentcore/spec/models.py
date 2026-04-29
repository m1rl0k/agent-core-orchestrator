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


class ExecutorSpec(BaseModel):
    """A pre-LLM command to run in a sandboxed worktree.

    The runtime applies the developer's diffs to a temp git worktree,
    then runs each declared executor inside it. Output (exit_code,
    stdout/stderr tails) is merged into the handoff payload under
    `<name>` so the agent's LLM call sees real tool output instead of
    inventing pass/fail.

    Declared in `<role>.agent.md` so each project owns its validation
    pipeline — runtime stays generic, no per-language heuristics.

    Example (in qa.agent.md):
      executors:
        - name: pytest
          command: [pytest, -q]
        - name: ruff
          command: [ruff, check, .]
        - name: typecheck
          command: [mypy, .]
    """

    model_config = ConfigDict(extra="forbid")

    name: str  # merged-payload key + a label for traces
    command: list[str]  # exact argv; runs in worktree cwd, no shell
    timeout_seconds: int = 600  # per-executor cap
    optional: bool = True  # non-zero exit doesn't fail the hop
    # If set, the runtime reads this file from the worktree after the
    # command exits and parses it as JSON into the merged payload.
    # Useful for `pytest --json-report-file=...` and similar.
    artifact: str | None = None


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
    # Pre-LLM executors. Each entry is either:
    #   - a string naming a registered executor in
    #     `agentcore.runtime.executors.EXECUTORS` (legacy form), or
    #   - an inline `ExecutorSpec` declaring the exact command to run.
    # Each runs in a temp git worktree with the developer's diffs
    # already applied; structured output is merged into the handoff
    # payload before the LLM call.
    executors: list[ExecutorSpec | str] = Field(default_factory=list)

    # When True, the runtime does a small "command discovery" LLM call
    # BEFORE the main hop: it shows the agent the developer's diffs and
    # asks for a single shell command that runs the relevant tests.
    # The command must be on PATH (never installs); the runtime executes
    # it in the sandbox, captures real pass/fail, and merges the result
    # into the handoff payload as `test_run` for the main call. This
    # lets agents (typically QA) choose the right runner by inspecting
    # what was just written — no per-project config, no heuristics.
    discovers_commands: bool = False

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
