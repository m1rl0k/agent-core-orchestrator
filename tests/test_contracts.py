"""Contract validation: payloads in/out of the handoff envelope."""

from __future__ import annotations

import pytest

from agentcore.contracts.envelopes import ContractViolation, Handoff, validate_payload
from agentcore.spec.models import IOField


def test_validate_primitive_ok() -> None:
    fields = [IOField(name="brief", type="string", required=True)]
    validate_payload(fields, {"brief": "do the thing"}, agent="architect", direction="input")


def test_validate_missing_required_raises() -> None:
    fields = [IOField(name="brief", type="string", required=True)]
    with pytest.raises(ContractViolation) as exc:
        validate_payload(fields, {}, agent="architect", direction="input")
    assert "missing required" in str(exc.value)


def test_validate_wrong_type_raises() -> None:
    fields = [IOField(name="count", type="int", required=True)]
    with pytest.raises(ContractViolation):
        validate_payload(fields, {"count": "not-an-int"}, agent="x", direction="input")


def test_validate_domain_type() -> None:
    fields = [IOField(name="context", type="ContextBundle", required=False)]
    validate_payload(
        fields,
        {"context": {"refs": [], "summary": "none"}},
        agent="architect",
        direction="input",
    )


def test_validate_unknown_type_raises() -> None:
    fields = [IOField(name="x", type="MysteryType", required=True)]
    with pytest.raises(ContractViolation):
        validate_payload(fields, {"x": {}}, agent="x", direction="input")


def test_handoff_successor_increments_step() -> None:
    h = Handoff(from_agent="user", to_agent="architect", payload={"brief": "x"})
    nxt = h.successor(from_agent="architect", to_agent="developer", payload={"summary": "s"})
    assert nxt.step == h.step + 1
    assert nxt.task_id == h.task_id
    assert nxt.parent_trace[-1].from_agent == "architect"
