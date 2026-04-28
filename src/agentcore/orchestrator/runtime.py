"""Runtime: execute one Handoff against the registry, with contract checks.

Flow per hop:
  1. Resolve `to_agent` from registry.
  2. Reject if `from_agent` is not in receiver's `accepts_handoff_from`.
  3. Validate payload against receiver's `Contract.inputs`.
  4. Render the prompt: system prompt + JSON schema instructions + payload.
  5. Call the LLM router.
  6. Parse model output as JSON; validate against `Contract.outputs`.
  7. Emit Outcome; if Outcome.delegate_to is set, build the successor Handoff.

The runtime does NOT loop — chaining is decided by the caller (CLI / HTTP).
This keeps every step inspectable and human-cancellable.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

from agentcore.contracts.envelopes import (
    ContractViolation,
    Handoff,
    Outcome,
    validate_payload,
)
from agentcore.llm.router import ChatMessage, LLMRouter
from agentcore.orchestrator.traces import TraceEvent, TraceLog
from agentcore.spec.loader import AgentRegistry
from agentcore.spec.models import AgentSpec, Contract

log = structlog.get_logger(__name__)


class HandoffRejected(RuntimeError):
    pass


class Runtime:
    def __init__(
        self,
        registry: AgentRegistry,
        router: LLMRouter,
        traces: TraceLog | None = None,
    ) -> None:
        self.registry = registry
        self.router = router
        self.traces = traces or TraceLog()

    async def execute(self, handoff: Handoff) -> tuple[Outcome, Handoff | None]:
        spec = self.registry.get(handoff.to_agent)
        if spec is None:
            raise HandoffRejected(f"unknown agent: {handoff.to_agent!r}")

        self._record(handoff.task_id, handoff.step, "handoff_in", spec.name,
                     {"from": handoff.from_agent})

        # 1. Authorization
        if handoff.from_agent not in spec.contract.accepts_handoff_from:
            raise HandoffRejected(
                f"agent {spec.name!r} does not accept handoffs from {handoff.from_agent!r}"
            )

        # 2. Payload validation
        try:
            validate_payload(
                spec.contract.inputs, handoff.payload, agent=spec.name, direction="input"
            )
        except ContractViolation as exc:
            self._record(handoff.task_id, handoff.step, "error", spec.name,
                         {"contract": exc.errors})
            raise

        # 3. LLM call
        messages = self._render_messages(spec, handoff)
        self._record(handoff.task_id, handoff.step, "llm_call", spec.name,
                     {"provider": spec.llm.provider, "model": spec.llm.model})
        resp = await self.router.complete(messages, spec.llm)

        # 4. Parse + validate output
        output = self._parse_json_block(resp.text)
        try:
            validate_payload(
                spec.contract.outputs, output, agent=spec.name, direction="output"
            )
        except ContractViolation as exc:
            self._record(handoff.task_id, handoff.step, "error", spec.name,
                         {"contract": exc.errors, "raw": resp.text[:1000]})
            raise

        # 5. Outcome
        delegate_to = self._infer_delegation(spec, output)
        outcome = Outcome(
            task_id=handoff.task_id,
            agent=spec.name,
            status="delegated" if delegate_to else "ok",
            output=output,
            delegate_to=delegate_to,
        )
        self._record(handoff.task_id, handoff.step, "outcome", spec.name,
                     {"status": outcome.status, "delegate_to": delegate_to})

        next_handoff = None
        if delegate_to:
            next_handoff = handoff.successor(
                from_agent=spec.name,
                to_agent=delegate_to,
                payload=output,
                notes=f"emitted by {spec.name}",
            )
        return outcome, next_handoff

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _render_messages(self, spec: AgentSpec, handoff: Handoff) -> list[ChatMessage]:
        schema = self._json_schema_hint(spec.contract)
        soul = (
            f"You are the {spec.soul.role}. Voice: {spec.soul.voice}. "
            f"Values: {', '.join(spec.soul.values) or 'n/a'}. "
            f"Forbidden: {', '.join(spec.soul.forbidden) or 'n/a'}."
        )
        system = f"{spec.system_prompt}\n\n{soul}\n\n{schema}".strip()
        user = (
            f"Inbound handoff from `{handoff.from_agent}` (task {handoff.task_id}, "
            f"step {handoff.step}).\n\n"
            f"Payload (validated against your inputs):\n```json\n"
            f"{json.dumps(handoff.payload, indent=2)}\n```\n\n"
            "Respond with a single JSON object matching the OUTPUT schema. "
            "Do not include any prose outside the JSON."
        )
        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user),
        ]

    @staticmethod
    def _json_schema_hint(contract: Contract) -> str:
        out_lines = []
        for f in contract.outputs:
            req = "required" if f.required else "optional"
            out_lines.append(f"  - {f.name}: {f.type} ({req}) — {f.description}")
        if not out_lines:
            return ""
        return "OUTPUT schema:\n" + "\n".join(out_lines)

    @staticmethod
    def _parse_json_block(text: str) -> dict[str, Any]:
        """Models occasionally wrap JSON in ```json fences. Be lenient."""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError(f"no JSON object found in model output: {text[:200]!r}")
        return json.loads(match.group(0))

    @staticmethod
    def _infer_delegation(spec: AgentSpec, output: dict[str, Any]) -> str | None:
        """If the agent placed `_delegate_to: <name>` in its output, honor it
        when that target is in `delegates_to`. Otherwise auto-pick the unique
        successor if there's exactly one."""
        explicit = output.pop("_delegate_to", None)
        if explicit and explicit in spec.contract.delegates_to:
            return explicit
        if len(spec.contract.delegates_to) == 1:
            return spec.contract.delegates_to[0]
        return None

    def _record(
        self, task_id: str, step: int, kind: str, actor: str, detail: dict[str, Any]
    ) -> None:
        self.traces.record(TraceEvent(
            task_id=task_id, step=step, kind=kind, actor=actor, detail=detail  # type: ignore[arg-type]
        ))
        log.info("trace", task_id=task_id, step=step, kind=kind, actor=actor, **detail)
