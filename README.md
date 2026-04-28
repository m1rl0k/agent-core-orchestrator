# agent-core-orchestrator

A hot-loadable, markdown-first, codebase-aware **role mesh** for SDLC agents.
Drop a folder of `*.agent.md` files into any project, hit one CLI command, and
get a contract-bound team of Architect / Developer / QA / Ops working together
through a thin FastAPI orchestrator — universally, on any codebase, on any OS.

> The wedge: every other framework either monoliths the team (MetaGPT) or
> ships a runtime without a role library (LangGraph, OpenAI Agents SDK).
> `agentcore` is the smallest thing that makes role definitions a first-class
> file format, with bounded contracts that compose with Copilot, Claude Code,
> Cursor, or nothing at all.

---

## Highlights

- **One file per role.** YAML frontmatter + system-prompt body. The same file
  works as a Claude Code subagent (`/agents`), an `AGENTS.md` entry for any
  IDE that reads it, and a fully-typed agentcore spec.
- **Bounded contracts.** Every handoff between roles is validated against the
  receiver's `inputs` / `outputs` schema before the LLM is ever called. No
  silent shape drift.
- **Hot-reload.** Edit an `agent.md`; the registry swaps it in atomically. In-
  flight tasks are pinned to the version they started with.
- **Multi-provider LLM router.** Anthropic, AWS Bedrock, Azure OpenAI, z.ai.
  Per-agent provider/model in frontmatter.
- **Hybrid RAG.** pgvector + Nomic-embed-text v1.5 + a NetworkX/Louvain
  *team & task* graph (who handed off to whom, which tasks touched which
  files, what the outcome was). Code traversal itself is offloaded to MCPs
  like gitnexus — agentcore's index is intentionally minimal.
- **Host-credentialed integrations.** Optional GitHub / AWS / Azure adapters
  ride on the host's existing `gh` / `aws` / `az` CLIs — agentcore never
  asks for credentials.
- **Cross-platform.** Linux, macOS, Windows. PowerShell-aware. Python 3.11–3.13.
- **Run anywhere.** Orchestrator on host or in Docker. Postgres+pgvector
  always in a container. Sandbox agent shell-outs in `host` or `docker` mode.
- **Self-maintaining.** Optional triggers (webhooks, scheduled scans) feed
  Signals to Ops, which can escalate to Architect → Dev → QA autonomously.

---

## Quickstart

```bash
# 1. Bring up postgres+pgvector
docker compose up -d postgres

# 2. Install (with uv, recommended)
uv sync
cp .env.example .env
# fill in at least one provider key (e.g. ANTHROPIC_API_KEY)

# 3. Sanity check the host
uv run agentcore doctor

# 4. Index the current repo
uv run agentcore index .

# 5. Run the role mesh
uv run agentcore plan "Add a /metrics endpoint that exposes Prometheus counters"
```

Without `uv`:

```bash
python -m venv .venv && source .venv/bin/activate     # macOS/Linux
# .venv\Scripts\Activate.ps1                          # Windows PowerShell
pip install -e .
agentcore doctor
```

---

## What an `agent.md` looks like

```markdown
---
name: architect
description: Plans technical design from a brief.
tools: [Read, Grep, Glob, WebSearch]
model: claude-opus-4-7

llm:
  provider: anthropic
  model: claude-opus-4-7
  temperature: 0.2

soul:
  role: architect
  voice: precise, terse, plan-oriented
  values: [correctness, simplicity, reversibility]

contract:
  inputs:
    - { name: brief, type: string, required: true }
  outputs:
    - { name: summary,         type: string, required: true }
    - { name: files_to_change, type: list,   required: true }
  accepts_handoff_from: [user, ops, qa]
  delegates_to: [developer]

knowledge:
  rag_collections: [code, docs]
---

You are the Architect. Read the brief, study the relevant code via the
provided context bundle, and produce a TechnicalPlan…
```

The same file is consumable by Claude Code (it ignores the unknown frontmatter
keys and treats the body as the system prompt). Run `agentcore link claude` to
mirror it into `.claude/agents/`.

See [`docs/SPEC.md`](docs/SPEC.md) for the full spec.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│ CLI / Skills      ─►  agentcore plan "<brief>"                       │
│ FastAPI surface   ─►  /run /handoff /signal /agents /capabilities    │
└──────────────────────────────────────────────────────────────────────┘
                              │
                ┌─────────────┼──────────────┐
                ▼             ▼              ▼
         ┌──────────┐  ┌────────────┐  ┌──────────────┐
         │ Registry │  │  Runtime   │  │  Adapters    │
         │ (hot     │  │ + Contract │  │ git · gh ·   │
         │  reload) │  │ validation │  │ aws · az     │
         └─────┬────┘  └─────┬──────┘  └──────┬───────┘
               │             │                 │
               ▼             ▼                 ▼
       ┌──────────────┐  ┌──────────┐  ┌──────────────┐
       │ agents/*.md  │  │ LLM      │  │ Capability   │
       │ (4 roles)    │  │ Router   │  │ preflight    │
       └──────────────┘  └────┬─────┘  └──────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
   Anthropic           AWS Bedrock           Azure OpenAI / z.ai

                    ┌────────────────────┐
                    │  Memory & RAG      │
                    │  pgvector +        │
                    │  Nomic-embed-1.5 + │
                    │  NetworkX/Louvain  │
                    └────────────────────┘
```

### Role mesh

```
  user ──▶ Architect ─plan─▶ Developer ─patch─▶ QA ──┬─ pass ─▶ Ops
                ▲                                      │
                │                                      └─ fail ─▶ Developer
                └────── revision request ◀─────────────────────── QA
                ▲
                └────── escalation ◀── Ops (from Signal)
```

---

## Multi-project & sandboxing

- **One orchestrator, many projects:** point each project's `.env` at a
  different `AGENTCORE_AGENTS_DIR` and `PGDATABASE`. Or run a separate
  orchestrator per project on different ports.
- **Sandbox modes:** `AGENTCORE_SANDBOX_MODE=host` (default) runs agent
  shell-outs on the orchestrator host. `=docker` wraps them in `docker exec`
  against `AGENTCORE_SANDBOX_IMAGE`. The orchestrator itself can run on host
  *or* in a container — independently.
- **Always-on infra:** `postgres` (with the pgvector volume) is the only
  service that *must* be containerised in dev.

---

## Optional integrations (host-credentialed)

agentcore never asks for cloud credentials. Each opt-in adapter rides on the
equivalent CLI already installed and authenticated on the host:

| Capability | CLI required        | Auth check                          | Enable                          |
| ---------- | ------------------- | ----------------------------------- | ------------------------------- |
| GitHub     | `gh`                | `gh auth status`                    | `AGENTCORE_ENABLE_GITHUB=true`  |
| AWS        | `aws`               | `aws sts get-caller-identity`       | `AGENTCORE_ENABLE_AWS=true`     |
| Azure      | `az`                | `az account show`                   | `AGENTCORE_ENABLE_AZURE=true`   |

`agentcore doctor` shows the live status matrix with OS-correct install hints.

When enabled, Ops can:

- triage open PRs, comment, and open remediation PRs (`gh`)
- listen for failing GitHub Actions runs
- read CloudWatch alarms / Azure Monitor alerts and escalate to Architect

---

## Composing with Copilot / Claude Code / Cursor

- `agentcore link claude` — mirrors the role library into `.claude/agents/`.
- `agentcore link claude --with-hooks` — also writes `.claude/settings.json`
  hooks that POST tool-use events to the orchestrator's `/signal`.
- For Copilot / Cursor: the same `agent.md` files are picked up if they read
  `AGENTS.md`-style roles. The unknown frontmatter is ignored.

---

## Roadmap

- [x] v0: core spec, runtime, four roles, multi-provider router, pgvector + graph, CLI
- [ ] v0.1: tree-sitter symbol graph (TS, Go, Rust)
- [ ] v0.2: webhook receiver for GitHub PR events
- [ ] v0.3: durable task store (postgres-backed checkpointing)
- [ ] v0.4: ACL on `accepts_handoff_from` (signed handoffs)

---

## License

Apache-2.0.
