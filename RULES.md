# Agentcore Rules

> Project-wide rules every agent (Architect / Developer / QA / Ops / Wikist /
> custom) reads on every hop. The runtime prepends this file to each agent's
> system prompt. Edit freely; agents pick up changes on the next handoff
> without restarting the orchestrator.
>
> Keep this file general. Per-role rules belong in `agents/<role>.agent.md`.

## A. Behavior

- Be concise. Do not restate the prompt.
- Default to action-oriented answers. Provide rationale only when it changes
  the decision.
- Prefer single-shot edits. Multi-file rework is fine when the task genuinely
  requires it; do not pad scope.
- If a request is ambiguous and a wrong guess would be costly, ask exactly one
  scope-tightening question. Otherwise pick the minimal viable interpretation
  and proceed.

## B. Context discipline

- Pull from the real codebase: target file(s), exact symbols, 1-hop
  callers/callees, failing tests + their last error tail.
- Skip third-party deps, generated files, large fixtures, vendored code,
  snapshots, build artifacts.
- Prefer symbol/AST slices over whole files when reasoning about a change.
- Do not re-quote unchanged context across hops. Reference it: "see prior
  plan / patch / report."

## C. Output format

- For code changes: emit unified diffs against the current tree. One diff
  block per file. Production and test diffs go in separate blocks.
- For structured outputs (plans, reports, verdicts): emit a single JSON
  object matching the contract schema. No prose outside the JSON, no
  `<think>` tags, no trailing commentary, no markdown fences around the
  outermost object.
- Print full files only when >90% of the file changes, or when creating it.

## D. Edit policy

- Preserve public APIs and observable behavior unless the brief explicitly
  changes them.
- Match existing code style, imports, error handling, and lint rules. The
  surrounding file is the source of truth for conventions.
- No incidental refactors, formatting passes, or "while I'm here" cleanup
  unless the brief asks for it.
- Do not modify lockfiles, CI config, or migration history without explicit
  approval in the brief.

## E. Planning

- For each hop, plan in ≤3 bullets:
  1. Task type (bugfix / feature / refactor / test / investigation).
  2. Target symbols/files.
  3. The smallest 2-step path to a passing change.
- Prefer single-file fixes first; widen scope only if narrower paths fail
  the contract.

## F. Testing

- Bugfixes: change only what's needed to make the failing test(s) pass. Add
  a regression test that fails before your fix and passes after.
- New features: at least one positive case and one negative case. Avoid
  large fixtures; prefer small inline data.
- Do not weaken existing tests. If a test is wrong, fix the test in a
  separate diff block and explain why in `notes`.
- Coverage is a guide, not a target. Don't add ceremonial tests.

## G. Tool use

- Invoke tools (build, lint, typecheck, search) only when they unblock the
  next concrete action.
- Summarize tool output briefly. Quote the relevant failing lines, not the
  whole log.

## H. Failure modes

- If you cannot satisfy the contract with the information at hand, return
  the partial result and list specifics in `risks` / `notes` / `blockers`.
  Do not invent file paths, symbols, or test names.
- If a previous attempt was rejected in review, address the listed blockers
  directly. Do not relitigate the verdict.

## I. Quality bar

- The change should compile / typecheck in principle.
- No secrets, credentials, or test fixtures with real PII.
- No commented-out code, no dead branches, no `TODO` markers added.
- Imports are necessary and ordered consistently with the file.

## J. House style (customize per project)

These keys are intentionally short. Edit to match your stack.

- **Languages**: <!-- e.g. TypeScript / Go / Python / Rust -->
- **Error handling**: <!-- e.g. typed errors at boundaries, `Result<T, E>` -->
- **Logging**: <!-- e.g. structlog, no `print()`; reserve `error` for paging -->
- **Concurrency**: <!-- e.g. asyncio only; no thread pools in request path -->
- **Performance**: <!-- e.g. O(1) extra alloc on hot paths -->
- **Naming**: <!-- e.g. snake_case modules, PascalCase classes -->

## K. Self-check (before emitting output)

- [ ] Output matches the declared contract schema (or the diff is well-formed).
- [ ] Paths are real and exist (or the diff creates them).
- [ ] Tests referenced in the output exist or are included in the diff.
- [ ] No unrelated edits.
- [ ] No secrets, no PII.
