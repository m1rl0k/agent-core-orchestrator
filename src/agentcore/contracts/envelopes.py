"""Handoff envelope and contract validation.

The orchestrator never invokes an agent without first running `validate_payload`
against the receiver's `Contract.inputs`. Symmetrically, an agent's output is
validated against `Contract.outputs` before being placed into a downstream
handoff.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentcore.contracts.domain import DOMAIN_TYPES, PRIMITIVE_TYPES
from agentcore.spec.models import IOField

_LIST_TYPE_RE = re.compile(r"^list\[(\w+)\]$")
_DICT_TYPE_RE = re.compile(r"^dict\[str,\s*(\w+)\]$")
_PRIMITIVE_PY: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "dict": dict,
    "list": list,
}


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
    ) -> Handoff:
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


def _check_inner(inner: str, value: Any) -> str | None:
    """Validate `value` against a primitive or domain type name."""
    if inner in PRIMITIVE_TYPES:
        py_type = _PRIMITIVE_PY[inner]
        if not isinstance(value, py_type):
            return f"expected {inner}, got {type(value).__name__}"
        return None
    model = DOMAIN_TYPES.get(inner)
    if model is None:
        return f"unknown type {inner!r}"
    try:
        model.model_validate(value)
    except ValidationError as exc:
        return f"{exc.errors(include_url=False)}"
    return None


def _check_field(field: IOField, value: Any) -> str | None:
    """Return None on success, error string on failure."""
    # Parametric list[<inner>] — validate every element.
    list_match = _LIST_TYPE_RE.match(field.type)
    if list_match:
        if not isinstance(value, list):
            return (
                f"field {field.name!r}: expected list, got {type(value).__name__}"
            )
        inner = list_match.group(1)
        for i, item in enumerate(value):
            err = _check_inner(inner, item)
            if err:
                return f"field {field.name!r}[{i}]: {err}"
        return None

    # Parametric dict[str, <inner>] — validate every value (keys must be str).
    # TODO: extend to non-str keys when a real use case emerges.
    dict_match = _DICT_TYPE_RE.match(field.type)
    if dict_match:
        if not isinstance(value, dict):
            return (
                f"field {field.name!r}: expected dict, got {type(value).__name__}"
            )
        inner = dict_match.group(1)
        for key, item in value.items():
            if not isinstance(key, str):
                return f"field {field.name!r}: dict keys must be str, got {type(key).__name__}"
            err = _check_inner(inner, item)
            if err:
                return f"field {field.name!r}[{key!r}]: {err}"
        return None

    if field.type in PRIMITIVE_TYPES:
        py_type = _PRIMITIVE_PY[field.type]
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
