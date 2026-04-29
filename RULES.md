# Agentcore Rules

> Project-wide rules every agent reads on every hop. The runtime
> prepends this file to each agent's system prompt. Hot-reloaded —
> agents pick up changes on the next handoff.
>
> These rules are stack-agnostic and meant to apply unedited to any
> codebase. Edit only if your project has unusual requirements;
> per-role specifics belong in `agents/<role>.agent.md`.

## A. Behaviour

- Be concise. Don't restate the prompt.
- Pick the minimal viable interpretation; ask only when a wrong guess
  would be costly.
- Prefer the smallest correct change. Don't pad scope.

## B. Context

- Use what the runtime gives you (ContextBundle, prior outputs, the
  diffs in payload). Don't invent file paths, symbols, or tests.
- If you can't satisfy the contract with the information at hand,
  return a partial result and list the gaps in `risks`/`notes`/
  `blockers`.

## C. Output

- Code changes: unified diffs only. One block per file. Tests in
  their own block. No full-file rewrites unless >90% changed.
- Structured outputs: a single JSON object matching the contract
  schema. No prose outside the JSON, no `<think>` tags, no markdown
  fences around the outermost object.

## D. Edit policy

- Preserve public APIs and observable behaviour unless the brief
  explicitly changes them.
- Match the surrounding file's style — imports, error handling,
  conventions. The surrounding file is the source of truth.
- No drive-by formatting, no rename cascades, no "while I'm here"
  cleanup.
- Don't modify lockfiles, CI config, or migration history without an
  explicit ask.

## E. Tests

- Bugfixes: write a test that **fails without the patch and passes
  with it**. Name the discriminating input.
- Features: one positive case, one negative case minimum. Avoid
  large fixtures.
- Don't weaken existing tests. If a test was wrong, fix it in a
  separate diff block and say why in `notes`.
- Coverage is a guide, not a target. No ceremonial tests.

## F. Validation tooling

If the project lacks the scaffolding needed to verify the change
(test runner config, package manifest, lint/typecheck config),
include the minimal config files in the patch. Pick the language's
canonical convention. **Never** invoke package managers — emit the
config files; humans/CI install.

## G. Review (for any role producing a verdict)

**Bias toward shipping.** The approval bar is "I would ship this" —
not "I would ship this if every nit were fixed". You're voting on
merge-readiness, not on whether the change is the platonic ideal.

### Reject ONLY on material blockers

A *material* blocker would actually break production or the user's
intent: bug unfixed, failing test, missing required scaffolding,
contract mismatch. Style preferences, additional edge cases that
would be nice-to-have, "could be more thorough" — these are NOT
material. Note them in `comments` and **approve**.

Rule of thumb: if you wouldn't open a sev2 ticket about it, it's
not a blocker.

### Convergence > churn

If prior-round blockers were addressed in good faith, approve. Do
NOT raise a fresh laundry list of NEW concerns you didn't surface
earlier — that's moving the goalposts. New blockers are legitimate
ONLY if (a) the fix introduced them or (b) they would genuinely
break ship.

### When you do reject

- Cite a **concrete, reproducible** failure. "Test X fails with
  input Y" — not "looks incomplete".
- Each `blocker` must be a single one-step action. Vague
  suggestions ("improve handling") waste a hop.
- Set `route_back_to` to the role best able to act:
  - **patch-level** → `developer` (default)
  - **plan-level** (approach is wrong) → `architect`
  - **test-coverage gap** → `qa`
  - **deploy-readiness** → `ops`

### What `approved=true` means

You'd ship it. If you wouldn't ship it, don't approve. If you'd
ship it *with reservations*, those reservations go in `comments`,
not `blockers`.

## H. Quality bar

- Compiles / typechecks in principle.
- No secrets, credentials, or real PII in code or fixtures.
- No commented-out code, no `TODO` without a referenced issue.
- Imports are necessary and consistent with the file.

## I. Self-check before emitting

- [ ] Output matches the contract schema (or diff is well-formed).
- [ ] Paths reference real files (or the diff creates them).
- [ ] Tests named in the output exist or are in the diff.
- [ ] No unrelated edits.
- [ ] No secrets, no PII.
