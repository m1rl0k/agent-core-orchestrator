"""Domain types passed between roles.

The `DOMAIN_TYPES` registry is what `IOField.type` resolves against. Adding a
new role-specific payload is as simple as defining a Pydantic model here and
adding it to the registry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


class ContextRef(BaseModel):
    """Pointer into the memory layer (RAG hit, code symbol, graph node)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["rag", "code", "graph", "doc"]
    id: str
    score: float | None = None
    excerpt: str = ""


class ContextBundle(BaseModel):
    """Aggregated retrieval result handed to an agent at invocation time."""

    model_config = ConfigDict(extra="forbid")

    refs: list[ContextRef] = Field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# Architect
# ---------------------------------------------------------------------------


class FileChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    action: Literal["create", "modify", "delete"]
    rationale: str

    @field_validator("action", mode="before")
    @classmethod
    def _normalise_action(cls, v: object) -> object:
        """LLMs reach for `update`/`edit`/`change`/`add`/`remove`
        instead of the canonical literals. Map them rather than
        reject — the action's intent is unambiguous."""
        if isinstance(v, str):
            alias = {
                "update": "modify",
                "edit": "modify",
                "change": "modify",
                "patch": "modify",
                "add": "create",
                "new": "create",
                "remove": "delete",
                "rm": "delete",
                "drop": "delete",
            }
            return alias.get(v.strip().lower(), v.strip().lower())
        return v


class TechnicalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    files_to_change: list[FileChange] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    test_strategy: str = ""
    open_questions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Developer
# ---------------------------------------------------------------------------


class FileDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    unified_diff: str


class ImplementationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_summary: str
    diffs: list[FileDiff] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# QA
# ---------------------------------------------------------------------------


class TestCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    file: str
    body: str  # source of the test, e.g. a pytest function


class TestSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_summary: str
    cases: list[TestCase] = Field(default_factory=list)


class FailedCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    error: str
    suggestion: str = ""


class QAReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite_summary: str
    passed: list[str] = Field(default_factory=list)
    failed: list[FailedCase] = Field(default_factory=list)
    coverage_pct: float | None = None


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------


class OpsReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_branch: str
    commit_sha: str | None = None
    pipeline_status: Literal["pending", "running", "passed", "failed", "skipped"] = "pending"
    artifacts: list[str] = Field(default_factory=list)
    notes: str = ""


# ---------------------------------------------------------------------------
# Autonomous mode: external signals + remediation proposals
# ---------------------------------------------------------------------------

SignalSource = Literal[
    "github_pr",
    "github_pipeline",
    "cloudwatch",
    "azure",
    "sentry",
    "datadog",
    "scheduled_scan",
    "manual",
]


class Signal(BaseModel):
    """An external event the orchestrator should evaluate.

    Sources include incoming PR webhooks, failing CI pipelines, cloud alarms,
    and periodic repo scans. Signals are routed to Ops first; Ops decides
    whether the situation needs a code change (in which case it emits a
    RemediationProposal to Architect) or just observation/ack.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    source: SignalSource
    kind: str  # e.g. "pr_opened", "alarm_triggered", "workflow_failed", "weekly_scan"
    severity: Literal["info", "warning", "error", "critical"] = "info"
    target: str  # e.g. "owner/repo#123", "service-name", "pipeline:42"
    payload: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RemediationProposal(BaseModel):
    """Ops's structured response to a Signal that needs a code change."""

    model_config = ConfigDict(extra="forbid")

    signal_id: str
    summary: str
    suggested_role: Literal["architect", "developer", "qa", "ops"] = "architect"
    urgency: Literal["low", "normal", "high"] = "normal"
    evidence: list[ContextRef] = Field(default_factory=list)
    notes: str = ""


class ReviewVerdict(BaseModel):
    """One agent's verdict on a proposed patch + test suite.

    The chain's review round collects one verdict per role; if any role
    rejects, blockers are aggregated and the chain re-runs starting at
    the agent best suited to address them (architect for plan-level
    issues, developer for patch-level, qa for test gaps). Loops cap at
    `max_review_loops` so we never spin forever.
    """

    model_config = ConfigDict(extra="forbid")

    agent: str
    approved: bool
    blockers: list[str] = Field(default_factory=list)
    comments: str = ""
    # Where the chain should restart if `approved=False`. Defaults to
    # 'architect' for plan-level concerns; reviewer can route to
    # 'developer' (patch-level) or 'qa' (test-gap) instead.
    route_back_to: Literal["architect", "developer", "qa", "ops"] = "architect"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

DOMAIN_TYPES: dict[str, type[BaseModel]] = {
    "ContextBundle": ContextBundle,
    "ContextRef": ContextRef,
    "FileChange": FileChange,
    "TechnicalPlan": TechnicalPlan,
    "FileDiff": FileDiff,
    "ImplementationPatch": ImplementationPatch,
    "TestCase": TestCase,
    "TestSuite": TestSuite,
    "FailedCase": FailedCase,
    "QAReport": QAReport,
    "OpsReport": OpsReport,
    "Signal": Signal,
    "RemediationProposal": RemediationProposal,
    "ReviewVerdict": ReviewVerdict,
}

PRIMITIVE_TYPES: set[str] = {"string", "int", "float", "bool", "dict", "list"}
