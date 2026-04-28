"""Handoff envelope and contract validation.

The orchestrator never invokes an agent without first running `validate_payload`
against the receiver's `Contract.inputs`. Symmetrically, an agent's output is
validated against `Contract.outputs` before being placed into a downstream
handoff.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentcore.contracts.domain import DOMAIN_TYPES, PRIMITIVE_TYPES
from agentcore.spec.models import IOField


def new_task_id() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Trace + envelopes
# ---------------------------------------------------------------------------


class Trace(BaseModel):
    """Single hop record, accumulated on every handoff."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    step: int
    from_agent: str
    to_agent: str
    timestamp: datetime = Field(default_factory=_utcnow)
    notes: str = ""


class Handoff(BaseModel):
    """Immutable transfer envelope between roles."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(default_factory=new_task_id)
    step: int = 0
    from_agent: str
    to_agent: str
    payload: dict[str, Any] = Field(default_factory=dict)
    context_refs: list[str] = Field(default_factory=list)
    parent_trace: list[Trace] = Field(default_factory=list)
    deadline: datetime | None = None

    def successor(
        self,
        *,
        to_agent: str,
        from_agent: str,
        payload: dict[str, Any],
        notes: str = "",
    ) -> "Handoff":
        """Build the next-hop handoff carrying forward task_id and trace."""
        new_step = self.step + 1
        trace = Trace(
            task_id=self.task_id,
            step=new_step,
            from_agent=from_agent,
            to_agent=to_agent,
            notes=notes,
        )
        return Handoff(
            task_id=self.task_id,
            step=new_step,
            from_agent=from_agent,
            to_agent=to_agent,
            payload=payload,
            parent_trace=[*self.parent_trace, trace],
        )


class Outcome(BaseModel):
    """What an agent returns after a single invocation."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    agent: str
    status: Literal["ok", "needs_more_context", "delegated", "failed"]
    output: dict[str, Any] = Field(default_factory=dict)
    delegate_to: str | None = None
    notes: str = ""


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------


class ContractViolation(ValueError):
    """Raised when a payload doesn't satisfy a Contract's inputs/outputs."""

    def __init__(self, agent: str, direction: str, errors: list[str]) -> None:
        self.agent = agent
        self.direction = direction
        self.errors = errors
        super().__init__(
            f"contract violation on {direction} for agent {agent!r}: " + "; ".join(errors)
        )


def _check_field(field: IOField, value: Any) -> str | None:
    """Return None on success, error string on failure."""
    if field.type in PRIMITIVE_TYPES:
        py_type = {
            "string": str,
            "int": int,
            "float": (int, float),
            "bool": bool,
            "dict": dict,
            "list": list,
        }[field.type]
        if not isinstance(value, py_type):
            return f"field {field.name!r}: expected {field.type}, got {type(value).__name__}"
        return None

    model = DOMAIN_TYPES.get(field.type)
    if model is None:
        return f"field {field.name!r}: unknown type {field.type!r}"
    try:
        model.model_validate(value)
    except ValidationError as exc:
        return f"field {field.name!r}: {exc.errors(include_url=False)}"
    return None


def validate_payload(
    fields: list[IOField],
    payload: dict[str, Any],
    *,
    agent: str,
    direction: str,
) -> None:
    """Validate a payload dict against an ordered list of IOFields."""
    errors: list[str] = []
    for field in fields:
        if field.name not in payload:
            if field.required:
                errors.append(f"missing required field {field.name!r}")
            continue
        err = _check_field(field, payload[field.name])
        if err:
            errors.append(err)
    if errors:
        raise ContractViolation(agent, direction, errors)
