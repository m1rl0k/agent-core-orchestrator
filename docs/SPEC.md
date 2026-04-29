# `agent.md` — unified role specification

A single markdown file is the only source of truth for a role. It is consumable by:

1. **agent-core-orchestrator** — parses the full frontmatter (persona + contract + knowledge bindings).
2. **Claude Code** (and any tool that reads `AGENTS.md` / `.claude/agents/*.md`) — uses only the
   compatibility fields (`name`, `description`, `tools`, `model`) and the body as the system prompt.
3. **Humans** — reads as plain markdown.

Foreign tools ignore unknown frontmatter keys, so a single file works everywhere.

## Layout

```markdown
---
# ─── Claude Code / AGENTS.md compatibility ────────────────────────────────
name: architect
description: Plans technical design from a brief or failing acceptance criteria.
tools: [Read, Grep, Glob, WebSearch]
model: claude-opus-4-7

# ─── Provider routing (agentcore extension) ───────────────────────────────
llm:
  provider: anthropic        # anthropic | bedrock | azure_openai | zai
  model: claude-opus-4-7
  temperature: 0.2
  max_tokens: 4096

# ─── SOUL: persona / identity (agentcore extension) ───────────────────────
soul:
  role: architect
  voice: precise, terse, plan-oriented
  values: [correctness, simplicity, reversibility]
  forbidden:
    - writing implementation code
    - merging without QA sign-off

# ─── Bounded contract (agentcore extension) ───────────────────────────────
contract:
  inputs:
    - { name: brief,   type: string,         required: true }
    - { name: context, type: ContextBundle,  required: false }
  outputs:
    - { name: summary,         type: string,           required: true }
    - { name: files_to_change, type: list[FileChange], required: true }
    - { name: risks,           type: list[string],     required: false }
  accepts_handoff_from: [user, ops, qa]
  delegates_to: [developer]
  sla_seconds: 120

# ─── Knowledge bindings (agentcore extension) ─────────────────────────────
knowledge:
  rag_collections: [code, docs, decisions]
  graph_communities: [architecture, dependencies]
  code_scopes: ["src/**", "docs/**"]
---

You are the Architect. Read the brief, study the relevant code via the
provided context bundle, and produce a `TechnicalPlan` …
```

## Contract semantics

- `accepts_handoff_from` — a whitelist; any inbound handoff from a role not in this list is rejected by the orchestrator before the agent is invoked.
- `delegates_to` — symmetric; the agent may only emit a handoff whose `to_agent` is in this list.
- `inputs` / `outputs` — referenced types must exist in `agentcore.contracts.domain` (or be primitive). Payloads are validated against these schemas, both inbound and outbound. Parametric containers are supported: `list[<inner>]` and `dict[str, <inner>]` where `<inner>` is a primitive (`string`, `int`, `float`, `bool`) or a domain type — every element is validated, not just the container.
- `sla_seconds` — soft deadline; runtime emits a warning trace but does not hard-kill.

## Handoff envelope

Every transfer between agents is an immutable `Handoff` envelope:

```json
{
  "task_id": "01HX…",
  "from_agent": "architect",
  "to_agent": "developer",
  "payload": { "plan": { … } },
  "context_refs": ["rag:doc/auth-overview", "code:src/auth/oauth.py:42"],
  "parent_trace": [ … ]
}
```

The receiver's contract validates `payload` before its system prompt is even loaded.

## Hot reload

The loader watches `AGENTCORE_AGENTS_DIR` (default `./agents`). On change:

1. Re-parse the file.
2. If parse + contract validation succeeds → atomically swap into the registry.
3. If it fails → keep the previous spec, surface the error in `/agents`.

In-flight tasks are pinned to the spec version they started with.

## Mesh diagram

```
                           ┌──────────┐
            user ────▶     │ Architect │
                           └────┬──────┘
                       plan ▼   ▲ revision
                           ┌────┴──────┐
                           │ Developer │◀──── failing tests ────┐
                           └────┬──────┘                        │
                          patch ▼                               │
                           ┌──────────┐         ┌──────────┐    │
                           │    QA    │────────▶│   Ops    │    │
                           └────┬─────┘ report  └────┬─────┘    │
                                └──────────────────┐ │          │
                                              fail │ │ pipeline │
                                                   ▼ ▼          │
                                                  back to dev ──┘
```
