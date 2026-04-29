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

import asyncio
import json
import platform
import re
from pathlib import Path
from typing import Any

import structlog

from agentcore.adapters.graphify import GraphifyAdapter
from agentcore.contracts.envelopes import (
    ContractViolation,
    Handoff,
    Outcome,
    validate_payload,
)
from agentcore.llm.router import ChatMessage, LLMRouter
from agentcore.llm.tokens import count_tokens
from agentcore.memory.graph import KnowledgeGraph
from agentcore.orchestrator.traces import TraceEvent, TraceLog
from agentcore.retrieval.hybrid import HybridRetriever
from agentcore.settings import get_settings
from agentcore.spec.loader import AgentRegistry
from agentcore.spec.models import AgentSpec, Contract

# Headroom we reserve (in TOKENS) for system prompt overhead, JSON schema
# hint, retrieval block, and reply space inside the budget. Conservative
# enough that agents with 4-10k system prompts still fit comfortably.
_NON_PAYLOAD_TOKENS = 8_000


# Mtime-cached read of the project RULES.md so live edits land on the
# next hop without restarting the orchestrator. Empty / missing file is
# fine — agents fall back to their own system prompts.
_RULES_CACHE: tuple[str, float, str] | None = None


def _load_rules(path: Path) -> str:
    global _RULES_CACHE
    try:
        st = path.stat()
    except OSError:
        return ""
    cache_key = (str(path), st.st_mtime)
    if _RULES_CACHE is not None and (_RULES_CACHE[0], _RULES_CACHE[1]) == cache_key:
        return _RULES_CACHE[2]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    _RULES_CACHE = (str(path), st.st_mtime, text)
    return text

log = structlog.get_logger(__name__)


class HandoffRejected(RuntimeError):
    pass


class SLAExceeded(RuntimeError):
    """Raised when a hop blows past its `sla_seconds` budget.

    Throughput note: SLA is enforced via `asyncio.wait_for`, which cancels
    the in-flight LLM call cleanly. We deliberately do NOT clamp tokens or
    model size — the budget is wall-clock only — so high-throughput agent
    loops can run as fast as the upstream LLM can emit.
    """

    def __init__(self, agent: str, sla_seconds: int | None) -> None:
        self.agent = agent
        self.sla_seconds = sla_seconds
        super().__init__(
            f"agent {agent!r} exceeded sla of {sla_seconds}s during LLM call"
        )


class Runtime:
    def __init__(
        self,
        registry: AgentRegistry,
        router: LLMRouter,
        traces: TraceLog | None = None,
        *,
        graph: KnowledgeGraph | None = None,
        graphify: GraphifyAdapter | None = None,
        retriever: HybridRetriever | None = None,
    ) -> None:
        self.registry = registry
        self.router = router
        self.traces = traces or TraceLog()
        self.graph = graph
        self.graphify = graphify
        self.retriever = retriever

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

        # 2. Pre-LLM phase. Two complementary mechanisms:
        #
        #    a) `discovers_commands: true` — the runtime does a small
        #       LLM call asking the agent (given the diffs in payload)
        #       what shell command runs the relevant tests. The chosen
        #       command runs in a temp git worktree against the dev's
        #       diffs; the structured result is merged as `test_run`.
        #       This is how QA grounds itself without static config.
        #    b) Static `executors:` — declared inline in <role>.agent.md
        #       as `ExecutorSpec`s (or registered string names). Used
        #       for fixed pipelines like "always run lint + typecheck"
        #       regardless of what the dev produced.
        new_payload = dict(handoff.payload)
        if spec.discovers_commands:
            disc = await self._discover_and_run(spec, handoff)
            if disc:
                new_payload.update(disc)
                self._record(
                    handoff.task_id, handoff.step, "discovery",
                    spec.name,
                    {"fields": list(disc.keys())},
                )
        if spec.executors:
            from agentcore.runtime.executors import run_executor

            for entry in spec.executors:
                merged = await run_executor(entry, new_payload)
                if merged:
                    new_payload.update(merged)
                    label = (
                        entry.name if hasattr(entry, "name") else str(entry)
                    )
                    self._record(
                        handoff.task_id, handoff.step, "executor",
                        spec.name,
                        {"name": label, "fields": list(merged.keys())},
                    )
        if new_payload != handoff.payload:
            handoff = handoff.model_copy(update={"payload": new_payload})

        # 3. Payload validation
        try:
            validate_payload(
                spec.contract.inputs, handoff.payload, agent=spec.name, direction="input"
            )
        except ContractViolation as exc:
            self._record(handoff.task_id, handoff.step, "error", spec.name,
                         {"contract": exc.errors})
            raise

        # 3. LLM call(s). When the rendered context would blow the budget
        #    we split the largest list-typed input field into chunks, run
        #    the LLM per chunk, and merge the structured outputs. One
        #    chunk = the original behaviour. Every agent gets batching for
        #    free; nothing in the agent.md needs to opt in.
        chunks = self._split_payload(spec, handoff)
        if len(chunks) == 1:
            output = await self._one_shot(spec, handoff)
        else:
            self._record(
                handoff.task_id, handoff.step, "batch_split", spec.name,
                {"chunks": len(chunks)},
            )
            partials: list[dict[str, Any]] = []
            for i, chunk_payload in enumerate(chunks):
                sub = handoff.model_copy(update={"payload": chunk_payload})
                self._record(
                    handoff.task_id, handoff.step, "batch_chunk", spec.name,
                    {"chunk": i + 1, "of": len(chunks)},
                )
                partials.append(await self._one_shot(spec, sub))
            output = self._merge_outputs(spec, partials)

        # 4. Validate the (merged) output. If shape doesn't match the
        #    contract (LLMs occasionally rename fields — `summary`
        #    instead of `plan_summary`, `changes` instead of `diffs`),
        #    re-ask with the missing-field list before failing the hop.
        try:
            validate_payload(
                spec.contract.outputs, output, agent=spec.name, direction="output"
            )
        except ContractViolation as first_err:
            self._record(
                handoff.task_id, handoff.step, "llm_retry", spec.name,
                {"reason": "contract_violation", "errors": first_err.errors[:3]},
            )
            output = await self._reask_with_contract(spec, handoff, first_err)
            try:
                validate_payload(
                    spec.contract.outputs, output,
                    agent=spec.name, direction="output",
                )
            except ContractViolation as exc:
                self._record(
                    handoff.task_id, handoff.step, "error", spec.name,
                    {"contract": exc.errors, "raw": str(output)[:1000]},
                )
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

        # 6. Enrichment hook: write to operational graph + pull symbol-level
        #    impact from graphify and merge it in.
        self._enrich_graph(handoff, spec, output)

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
    # LLM call + batching
    # ------------------------------------------------------------------

    async def _discover_and_run(
        self, spec: AgentSpec, handoff: Handoff
    ) -> dict[str, Any]:
        """Self-discovery: ask the agent to propose 1-N candidate test
        commands for the developer's diffs given the host OS, then try
        each in turn until one runs cleanly (rc=0) or all exhaust.

        The agent is told the host OS so it can pick sh / pwsh / native
        tooling appropriately. Multiple candidates lets the agent hedge
        when it isn't sure which runner the project wires up.

        Returns `{"test_command": [...], "test_run": {...},
        "test_attempts": [...]}`. Empty dict if no diffs or LLM bails.
        """
        from agentcore.runtime.sandbox import run_in_worktree

        diffs = handoff.payload.get("diffs") or []
        if not diffs:
            return {}

        # Compact the diffs into a sketch the LLM can scan quickly.
        sketch: list[dict[str, Any]] = []
        for d in diffs[:20]:
            if not isinstance(d, dict):
                continue
            text = d.get("unified_diff") or ""
            sketch.append({
                "path": d.get("path"),
                "preview": "\n".join(text.splitlines()[:40]),
            })

        host_label = f"{platform.system()} ({platform.machine()})"
        sys_prompt = (
            "Choose 1-3 candidate shell commands that run the tests "
            "covering the developer's proposed diffs. The runtime will "
            "try them in order until one exits cleanly (rc=0).\n\n"
            f"Host OS: {host_label}. Pick commands appropriate for the "
            "host — bash/sh on Linux/macOS, pwsh/cmd on Windows. Use "
            "language-native tooling where applicable (pytest, go test, "
            "cargo test, npm/pnpm/yarn test, mvn/gradle test, dotnet "
            "test, ctest, etc.).\n\n"
            "Constraints:\n"
            "  - Each command's first arg MUST be on PATH already; the "
            "runtime never installs anything.\n"
            "  - If no test command is appropriate (e.g. docs-only "
            'diff), respond with {"candidates": []}.\n'
            "  - You may include a fallback or two — e.g. if pytest "
            "isn't installed, try `python -m unittest discover`.\n\n"
            "Reply with ONLY a JSON object:\n"
            '{"candidates": [["arg0","arg1",...], ...], '
            '"rationale": "<one sentence>"}. '
            "No prose, no markdown fences, no <think> tags."
        )
        user_prompt = (
            "Repository diffs (paths + previews):\n"
            f"```json\n{json.dumps(sketch, indent=2)}\n```"
        )
        try:
            resp = await asyncio.wait_for(
                self.router.complete(
                    [
                        ChatMessage(role="system", content=sys_prompt),
                        ChatMessage(role="user", content=user_prompt),
                    ],
                    spec.llm,
                ),
                timeout=180.0,
            )
        except Exception as exc:
            log.warning("discovery.llm_failed", error=str(exc))
            return {}

        match = re.search(r"\{.*\}", resp.text, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
        candidates = data.get("candidates") or data.get("commands") or []
        # Tolerant: also accept `command: [...]` (single) for back-compat.
        if not candidates and isinstance(data.get("command"), list):
            candidates = [data["command"]]
        if not isinstance(candidates, list) or not candidates:
            return {}
        # Filter to well-formed argv lists.
        candidates = [
            list(c) for c in candidates
            if isinstance(c, list) and c and all(isinstance(x, str) for x in c)
        ]
        if not candidates:
            return {}

        attempts: list[dict[str, Any]] = []
        last_run: dict[str, Any] | None = None
        winning_cmd: list[str] | None = None
        for cmd in candidates:
            try:
                run = await run_in_worktree(
                    list(cmd),
                    diffs=diffs,
                    repo_root=handoff.payload.get("repo_root"),
                    timeout_seconds=600,
                )
            except Exception as exc:
                run = {
                    "executor_status": "error",
                    "exit_code": -1,
                    "stdout_tail": "",
                    "stderr_tail": str(exc)[:500],
                    "applied_files": [],
                    "command": " ".join(cmd),
                }
            attempts.append({
                "command": cmd,
                "exit_code": run.get("exit_code"),
                "executor_status": run.get("executor_status"),
            })
            last_run = run
            if run.get("exit_code") == 0:
                winning_cmd = cmd
                break

        return {
            "test_command": winning_cmd or candidates[-1],
            "test_run": last_run or {},
            "test_attempts": attempts,
        }

    async def _reask_with_contract(
        self, spec: AgentSpec, handoff: Handoff, err: ContractViolation,
    ) -> dict[str, Any]:
        """Re-prompt the agent after a contract-shape mismatch.

        We don't blindly retry the same prompt — we tell the model
        exactly which required fields it dropped and re-emit the
        schema hint so it can correct course on a single second pass.
        """
        messages = await self._render_messages(spec, handoff)
        schema_hint = self._json_schema_hint(spec.contract)
        nudge = (
            "Your previous response failed contract validation:\n"
            f"  {'; '.join(err.errors[:5])}\n\n"
            "Re-emit the JSON object using the EXACT field names from "
            "the OUTPUT schema below. Include every required field. "
            "No extra fields, no `<think>` tags, no markdown fences.\n\n"
            f"{schema_hint}"
        )
        messages = [*messages, ChatMessage(role="user", content=nudge)]
        try:
            if spec.contract.sla_seconds:
                resp = await asyncio.wait_for(
                    self.router.complete(messages, spec.llm),
                    timeout=float(spec.contract.sla_seconds),
                )
            else:
                resp = await self.router.complete(messages, spec.llm)
        except TimeoutError as exc:
            raise SLAExceeded(spec.name, spec.contract.sla_seconds) from exc
        return self._parse_json_block(resp.text)

    async def _one_shot(self, spec: AgentSpec, handoff: Handoff) -> dict[str, Any]:
        """Render messages, call the LLM honouring SLA, parse JSON.

        Used once for normal hops and once per chunk for batched hops.
        Retries once on parse failure with a tightened reminder appended —
        thinking models occasionally emit `<think>...</think>` followed by
        a bare code fence and no JSON body, and a single re-ask usually
        succeeds where the first call truncated.
        """
        messages = await self._render_messages(spec, handoff)
        self._record(
            handoff.task_id, handoff.step, "llm_call", spec.name,
            {
                "provider": spec.llm.provider,
                "model": spec.llm.model,
                "sla_seconds": spec.contract.sla_seconds,
            },
        )
        for attempt in (1, 2):
            try:
                if spec.contract.sla_seconds:
                    import asyncio as _aio

                    resp = await _aio.wait_for(
                        self.router.complete(messages, spec.llm),
                        timeout=float(spec.contract.sla_seconds),
                    )
                else:
                    resp = await self.router.complete(messages, spec.llm)
            except TimeoutError as exc:
                self._record(
                    handoff.task_id, handoff.step, "error", spec.name,
                    {"timeout": spec.contract.sla_seconds, "phase": "llm_call"},
                )
                raise SLAExceeded(spec.name, spec.contract.sla_seconds) from exc
            try:
                return self._parse_json_block(resp.text)
            except ValueError:
                if attempt >= 2:
                    raise
                self._record(
                    handoff.task_id, handoff.step, "llm_retry", spec.name,
                    {"reason": "unparseable_output", "snippet": resp.text[:120]},
                )
                # Tighten the reminder for the retry — no thinking tokens,
                # JSON object only.
                messages = [
                    *messages,
                    ChatMessage(
                        role="user",
                        content=(
                            "Your previous response was unparseable. "
                            "Reply with ONLY the JSON object — no <think> "
                            "tags, no commentary, no markdown fences."
                        ),
                    ),
                ]
        raise RuntimeError("unreachable")  # pragma: no cover

    def _split_payload(
        self, spec: AgentSpec, handoff: Handoff
    ) -> list[dict[str, Any]]:
        """Decide whether the rendered hop fits the budget. If not, split
        the largest list-valued input field into chunks; non-list fields
        are duplicated across chunks so each batch sees full context.

        Uses `count_tokens` (tiktoken o200k_base by default; HF tokenizer
        if a matching JSON sits in `vendor/tokenizers/`; char-estimate
        as last-resort floor) so the decision is grounded in real token
        counts, not a chars/3 heuristic.
        """
        budget = int(get_settings().llm_context_budget_tokens)
        hint = spec.llm.model
        payload = handoff.payload
        sys_tokens = count_tokens(spec.system_prompt or "", model_hint=hint)
        payload_tokens = count_tokens(
            json.dumps(payload, default=str), model_hint=hint
        )
        if sys_tokens + payload_tokens + _NON_PAYLOAD_TOKENS <= budget:
            return [payload]
        list_fields = [(k, v) for k, v in payload.items() if isinstance(v, list)]
        if not list_fields:
            return [payload]
        largest_key, largest_val = max(
            list_fields,
            key=lambda kv: count_tokens(
                json.dumps(kv[1], default=str), model_hint=hint
            ),
        )
        if len(largest_val) < 2:
            return [payload]
        base = {k: v for k, v in payload.items() if k != largest_key}
        base_tokens = count_tokens(json.dumps(base, default=str), model_hint=hint)
        available = budget - sys_tokens - base_tokens - _NON_PAYLOAD_TOKENS
        if available < 500:
            # Base alone is over budget; we can't split usefully. Let the
            # LLM see what it sees and fail loud.
            return [payload]
        largest_tokens = count_tokens(
            json.dumps(largest_val, default=str), model_hint=hint
        )
        tokens_per_item = max(1, largest_tokens // len(largest_val))
        items_per_chunk = max(1, available // tokens_per_item)
        return [
            {**base, largest_key: largest_val[i : i + items_per_chunk]}
            for i in range(0, len(largest_val), items_per_chunk)
        ]

    def _merge_outputs(
        self, spec: AgentSpec, partials: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Combine per-chunk outputs into a single contract-shaped dict.

        Lists concatenate (the natural shape for batched results); strings
        take the first non-empty value (avoids a 5x-long summary); booleans
        AND together (conservative — every chunk must approve); numerics
        keep the first non-None.
        """
        merged: dict[str, Any] = {}
        for out in partials:
            if not isinstance(out, dict):
                continue
            for k, v in out.items():
                if k not in merged or merged[k] is None:
                    merged[k] = v
                    continue
                cur = merged[k]
                if isinstance(cur, list) and isinstance(v, list):
                    merged[k] = cur + v
                elif isinstance(cur, bool) and isinstance(v, bool):
                    merged[k] = cur and v
                elif isinstance(cur, (int, float)) and isinstance(v, (int, float)):
                    # Keep first; merging numerics is too domain-specific.
                    pass
                elif isinstance(cur, str) and isinstance(v, str) and not cur and v:
                    merged[k] = v
                # Anything else: keep first.
        return merged

    # ------------------------------------------------------------------
    # Enrichment
    # ------------------------------------------------------------------

    def _enrich_graph(
        self, handoff: Handoff, spec: AgentSpec, output: dict[str, Any]
    ) -> None:
        if self.graph is None:
            return
        from agentcore.memory.prf import (
            HIGH_BLAST_RADIUS,
            LOW_BLAST_RADIUS,
            classify_change_kinds,
        )

        self.graph.record_handoff(
            handoff.task_id, handoff.from_agent, spec.name, created_by=spec.name
        )

        intent = (
            output.get("plan_summary")
            or output.get("summary")
            or output.get("notes")
            or ""
        )

        # ---- Snippets from architect plans (file-level) -----------------
        for fc in output.get("files_to_change", []) or []:
            if not isinstance(fc, dict) or "path" not in fc:
                continue
            self.graph.record_change(handoff.task_id, fc["path"], created_by=spec.name)
            self.graph.record_snippet(
                handoff.task_id,
                fc["path"],
                start=0,
                end=0,
                content=str(fc.get("rationale", "")),
                intent=intent,
                role=spec.name,
                created_by=spec.name,
            )

        # ---- Snippets from developer/qa diffs (line-range level) --------
        for diff in output.get("diffs", []) or []:
            if not isinstance(diff, dict) or "path" not in diff:
                continue
            path = diff["path"]
            self.graph.record_change(handoff.task_id, path, created_by=spec.name)
            for start, end, hunk in self._parse_diff_hunks(str(diff.get("unified_diff", ""))):
                self.graph.record_snippet(
                    handoff.task_id, path,
                    start=start, end=end, content=hunk,
                    intent=intent, role=spec.name,
                )

        # ---- Agent-supplied snippet/feedback annotations ----------------
        # Agents may include `_snippets` or `_feedback` to enrich beyond what
        # the runtime can infer from diffs alone.
        for snip in output.pop("_snippets", []) or []:
            if not isinstance(snip, dict):
                continue
            self.graph.record_snippet(
                handoff.task_id,
                snip.get("path", ""),
                start=int(snip.get("start", 0)),
                end=int(snip.get("end", 0)),
                content=str(snip.get("content", "")),
                intent=str(snip.get("intent", intent)),
                role=spec.name,
                created_by=spec.name,
            )
        for fb in output.pop("_feedback", []) or []:
            if not isinstance(fb, dict) or "label" not in fb:
                continue
            self.graph.tag_relevance(
                handoff.task_id,
                str(fb["label"]),
                score=float(fb.get("score", 1.0)),
                reason=str(fb.get("reason", "")),
                created_by=spec.name,
            )

        # ---- Auto-classification (change-kind labels) -------------------
        for kind in classify_change_kinds(intent):
            self.graph.tag_relevance(
                handoff.task_id, kind, reason="auto-classified", created_by=spec.name
            )

        # ---- Graphify enrichment + blast-radius PRF ---------------------
        paths = self._extract_paths(output)
        if not paths or self.graphify is None or not self.graphify.is_ready:
            return

        total_downstream = 0
        for path in paths:
            impact = self.graphify.impact(path)
            if impact is None:
                continue
            self.graph.record_impact(
                handoff.task_id, path, impact.downstream, created_by=spec.name
            )
            total_downstream += len(impact.downstream)
            sub = self.graphify.subgraph_for([impact.symbol, *impact.downstream])
            if sub is not None:
                self.graph.merge_subgraph(sub, namespace="symbol")

        if total_downstream:
            label = HIGH_BLAST_RADIUS if total_downstream >= 10 else LOW_BLAST_RADIUS
            self.graph.tag_relevance(
                handoff.task_id, label,
                score=float(total_downstream),
                reason=f"{total_downstream} downstream symbols",
                created_by=spec.name,
            )

    @staticmethod
    def _parse_diff_hunks(unified_diff: str) -> list[tuple[int, int, str]]:
        """Extract `(new_start, new_end, hunk_text)` per @@ block.

        We use the *new file* coordinates because agents reason about the
        post-change layout. Returns one entry per hunk.
        """
        if not unified_diff:
            return []
        out: list[tuple[int, int, str]] = []
        current_start: int | None = None
        current_lines: list[str] = []
        current_added = 0
        for line in unified_diff.splitlines():
            if line.startswith("@@"):
                if current_start is not None:
                    out.append((
                        current_start,
                        current_start + max(current_added - 1, 0),
                        "\n".join(current_lines),
                    ))
                current_lines = []
                current_added = 0
                m = re.search(r"\+(\d+)(?:,(\d+))?", line)
                current_start = int(m.group(1)) if m else 0
                continue
            if current_start is None:
                continue
            if line.startswith(("+", " ")) and not line.startswith("+++"):
                current_lines.append(line[1:])
                current_added += 1
        if current_start is not None and current_lines:
            out.append((
                current_start,
                current_start + max(current_added - 1, 0),
                "\n".join(current_lines),
            ))
        return out

    @staticmethod
    def _extract_paths(output: dict[str, Any]) -> list[str]:
        """Pull file paths out of common output shapes (architect/dev/qa)."""
        paths: list[str] = []
        for key in ("files_to_change", "diffs"):
            entries = output.get(key)
            if not isinstance(entries, list):
                continue
            for item in entries:
                if isinstance(item, dict) and "path" in item:
                    paths.append(str(item["path"]))
                elif isinstance(item, str):
                    paths.append(item)
        # de-dupe, preserve order
        seen: set[str] = set()
        return [p for p in paths if not (p in seen or seen.add(p))]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _render_messages(
        self, spec: AgentSpec, handoff: Handoff
    ) -> list[ChatMessage]:
        schema = self._json_schema_hint(spec.contract)
        soul = (
            f"You are the {spec.soul.role}. Voice: {spec.soul.voice}. "
            f"Values: {', '.join(spec.soul.values) or 'n/a'}. "
            f"Forbidden: {', '.join(spec.soul.forbidden) or 'n/a'}."
        )
        rules = _load_rules(get_settings().rules_path)
        rules_block = f"# PROJECT RULES\n{rules}\n\n" if rules else ""
        # Hard output-format directive at the END of the system prompt
        # (recency wins in long contexts). Tells thinking-mode models to
        # skip <think>...</think> blocks because (a) we strip them
        # post-hoc anyway and (b) on heavy multi-file emissions the
        # thinking block eats the response budget and the JSON gets
        # truncated, dropping required fields.
        output_directive = (
            "\n\n# OUTPUT — STRICT\n"
            "Reply with EXACTLY ONE JSON object matching the schema "
            "above. Begin your reply with `{`. End with `}`. NO prose "
            "before or after. NO `<think>...</think>` blocks (we strip "
            "them post-hoc, and they eat your output budget — JSON gets "
            "truncated and required fields fall off). NO markdown "
            "fences around the outermost object. Prioritise emitting "
            "the COMPLETE JSON over verbose reasoning; if you sense "
            "you're approaching the token cap, cut everything except "
            "the JSON envelope."
        )
        system = (
            f"{rules_block}{spec.system_prompt}\n\n{soul}\n\n{schema}"
            f"{output_directive}"
        ).strip()
        context_block = await self._build_context_block(spec, handoff)
        # Mirror the directive at the START of the user message too —
        # so the very first thing the model reads is "JSON only", then
        # the payload, then a closing reminder.
        user = (
            "Output a single JSON object that matches the OUTPUT schema. "
            "JSON only. No thinking blocks, no markdown, no prose.\n\n"
            f"Inbound handoff from `{handoff.from_agent}` (task {handoff.task_id}, "
            f"step {handoff.step}).\n\n"
            f"Payload (validated against your inputs):\n```json\n"
            f"{json.dumps(handoff.payload, indent=2)}\n```\n"
            f"{context_block}\n"
            "Reply now with the JSON object — no preamble, no `<think>` "
            "tag, just `{...}`."
        )
        return [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user),
        ]

    async def _build_context_block(
        self, spec: AgentSpec, handoff: Handoff
    ) -> str:
        """Assemble the per-hop dynamic context block.

        Three sources, each independent (any can fail without aborting):
          1. Hybrid RAG (vector + graph + reranker) over the agent's
             declared `knowledge.rag_collections`.
          2. Operational memory: prior tasks that touched the same files,
             aggregated PRF labels, co-changed neighbours.
          3. graphify symbol context for files mentioned in the payload.
        """
        sections: list[str] = []
        query = self._compose_query(handoff)
        files = self._files_in_payload(handoff.payload)

        # ---- 1. Retrieval ----
        if self.retriever and spec.knowledge.rag_collections and query:
            try:
                result = await self.retriever.retrieve(
                    query, spec.knowledge.rag_collections, k=6
                )
                if result.bundle.refs:
                    lines = ["== Retrieved context (semantic) =="]
                    lines.append(result.bundle.summary)
                    for ref in result.bundle.refs:
                        lines.append(
                            f"- {ref.id}  (score {ref.score:.2f})\n"
                            f"  ```\n  {ref.excerpt}\n  ```"
                        )
                    sections.append("\n".join(lines))
            except Exception as exc:
                log.warning("retrieval_failed", agent=spec.name, error=str(exc))

        # ---- 2. Operational memory ----
        if self.graph is not None and files:
            mem = self.graph.operational_memory(files)
            if mem["tasks"] or mem["label_counts"] or mem["neighbors"]:
                lines = ["== Operational memory (team & task history) =="]
                if mem["tasks"]:
                    lines.append("Recent tasks touching these files:")
                    for t in mem["tasks"]:
                        labels = ", ".join(t["labels"]) or "no labels"
                        lines.append(f"- {t['id']}  [{labels}]")
                if mem["label_counts"]:
                    lines.append(
                        "Aggregate PRF labels in this area: "
                        + ", ".join(f"{k}×{v}" for k, v in mem["label_counts"].items())
                    )
                if mem["neighbors"]:
                    lines.append(
                        "Co-changed files (likely related): "
                        + ", ".join(mem["neighbors"])
                    )
                sections.append("\n".join(lines))

        # ---- 3. Graphify symbol context ----
        if self.graphify is not None and self.graphify.is_ready and files:
            lines = ["== Code-graph context (graphify) =="]
            for path in files[:5]:
                impact = self.graphify.impact(path)
                if impact is None:
                    continue
                lines.append(
                    f"- {path}: blast radius {len(impact.downstream)} "
                    f"(confidence {impact.confidence:.2f})"
                )
                if impact.downstream:
                    lines.append("    downstream: " + ", ".join(impact.downstream[:8]))
            if len(lines) > 1:
                sections.append("\n".join(lines))

        if not sections:
            return ""
        return "\n\n" + "\n\n".join(sections) + "\n"

    @staticmethod
    def _compose_query(handoff: Handoff) -> str:
        """Cheap natural-language query from common payload shapes."""
        p = handoff.payload
        for key in ("brief", "summary", "plan_summary", "notes", "suite_summary"):
            value = p.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    @staticmethod
    def _files_in_payload(payload: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for key in ("files_to_change", "diffs"):
            entries = payload.get(key)
            if not isinstance(entries, list):
                continue
            for item in entries:
                if isinstance(item, dict) and "path" in item:
                    out.append(str(item["path"]))
        seen: set[str] = set()
        return [p for p in out if not (p in seen or seen.add(p))]

    @staticmethod
    def _json_schema_hint(contract: Contract) -> str:
        """Render the output schema in a way the LLM can faithfully reproduce.

        Includes every domain type's field shape inline. Without this, models
        emit their own JSON shape (e.g. {"file_path", "changes": [...]}
        instead of FileChange's {path, action, rationale}) and contract
        validation rejects the response, wasting the LLM call.
        """
        from agentcore.contracts.domain import DOMAIN_TYPES

        out_lines: list[str] = []
        referenced: set[str] = set()
        for f in contract.outputs:
            req = "required" if f.required else "optional"
            out_lines.append(f"  - {f.name}: {f.type} ({req}) — {f.description}")
            # Capture every domain type referenced (including list[Type] and dict[str, Type]).
            inner = f.type
            for prefix in ("list[", "dict[str,"):
                if inner.startswith(prefix):
                    inner = inner[len(prefix):].rstrip("] ").strip()
                    break
            if inner in DOMAIN_TYPES:
                referenced.add(inner)

        type_blocks: list[str] = []
        for tname in sorted(referenced):
            model = DOMAIN_TYPES[tname]
            fields = model.model_fields
            field_lines = []
            for field_name, field_info in fields.items():
                ann = field_info.annotation
                ann_str = getattr(ann, "__name__", None) or str(ann).replace("typing.", "")
                req_str = "required" if field_info.is_required() else "optional"
                field_lines.append(f"    {field_name}: {ann_str} ({req_str})")
            type_blocks.append(f"  {tname}:\n" + "\n".join(field_lines))

        if not out_lines:
            return ""
        s = "OUTPUT schema:\n" + "\n".join(out_lines)
        if type_blocks:
            s += "\n\nReferenced types (use EXACTLY these field names):\n" + "\n".join(
                type_blocks
            )
        return s

    @staticmethod
    def _parse_json_block(text: str) -> dict[str, Any]:
        """Extract a valid JSON object from fenced or prose-wrapped output."""
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
        if fence:
            try:
                parsed = json.loads(fence.group(1))
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(parsed, dict):
                    return parsed

        candidates: list[str] = []
        starts = [i for i, ch in enumerate(text) if ch == "{"]
        for start in starts:
            depth = 0
            in_string = False
            escaped = False
            for end in range(start, len(text)):
                ch = text[end]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.append(text[start : end + 1])
                        break

        for candidate in sorted(candidates, key=len, reverse=True):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        raise ValueError(f"no JSON object found in model output: {text[:200]!r}")

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
