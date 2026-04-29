"""Claude Code adapter.

Two integration points:

1. **Subagent compatibility.** Mirror `agents/*.agent.md` into `.claude/agents/`
   so Claude Code can invoke our roles directly via `/agents` or its
   subagent dispatcher. The same file is the source of truth — the mirror
   is a copy on `agentcore link claude`.

2. **Hooks.** Generate a `.claude/settings.json` snippet that wires
   PreToolUse / PostToolUse / SessionStart hooks to the orchestrator's
   `/signal` endpoint. Off by default; opt in via `agentcore link claude
   --with-hooks`.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

CLAUDE_AGENTS_DIR = ".claude/agents"
CLAUDE_SETTINGS_FILE = ".claude/settings.json"


@dataclass(slots=True)
class LinkResult:
    mirrored: list[str]
    skipped: list[str]
    settings_written: bool


def link(
    project_root: Path | str,
    agents_dir: Path | str,
    *,
    with_hooks: bool = False,
    orchestrator_url: str = "http://localhost:8088",
) -> LinkResult:
    root = Path(project_root).resolve()
    src = Path(agents_dir).resolve()
    dst = root / CLAUDE_AGENTS_DIR
    dst.mkdir(parents=True, exist_ok=True)

    mirrored: list[str] = []
    skipped: list[str] = []
    for path in sorted(src.glob("*.agent.md")):
        target = dst / path.name.replace(".agent.md", ".md")
        if target.exists() and target.read_text(encoding="utf-8") == path.read_text(encoding="utf-8"):
            skipped.append(target.name)
            continue
        shutil.copy2(path, target)
        mirrored.append(target.name)

    settings_written = False
    if with_hooks:
        settings_written = _write_hook_settings(root, orchestrator_url)

    return LinkResult(mirrored=mirrored, skipped=skipped, settings_written=settings_written)


def _write_hook_settings(root: Path, orchestrator_url: str) -> bool:
    settings_path = root / CLAUDE_SETTINGS_FILE
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "curl -sS -X POST "
                                f"{orchestrator_url}/signal "
                                "-H 'content-type: application/json' "
                                "-d '{\"source\":\"manual\","
                                "\"kind\":\"bash_post\","
                                "\"target\":\"$CLAUDE_PROJECT_DIR\","
                                "\"payload\":{}}' >/dev/null || true"
                            ),
                        }
                    ],
                }
            ]
        }
    }
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        existing.setdefault("hooks", {}).update(payload["hooks"])
        settings_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    else:
        settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Wiki projections — same source of truth, different per-tool surfaces.
# These all read from `WikiStorage` and write into the project root.
# ---------------------------------------------------------------------------

CLAUDE_SKILLS_DIR = ".claude/skills"
COPILOT_PROMPTS_DIR = ".github/prompts"
COPILOT_INSTRUCTIONS = ".github/copilot-instructions.md"
CURSOR_RULES_FILE = ".cursor/rules/wiki.md"
AGENTS_MD = "AGENTS.md"


def _slugify(rel: str) -> str:
    """`subsystems/retrieval pipeline.md` -> `retrieval-pipeline`."""
    stem = rel.rsplit("/", 1)[-1].removesuffix(".md")
    return "".join(c if c.isalnum() else "-" for c in stem.lower()).strip("-") or "page"


def link_wiki(project_root: Path, storage) -> int:  # type: ignore[no-untyped-def]
    """Mirror wiki subsystems into `.claude/skills/<slug>/SKILL.md` and append
    a module catalogue to `AGENTS.md`. Returns the number of skills written.
    """
    root = Path(project_root)
    skills_dir = root / CLAUDE_SKILLS_DIR
    skills_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    catalogue_lines: list[str] = []
    for page in storage.walk():
        if page.rel.startswith("subsystems/"):
            slug = _slugify(page.rel)
            skill_dir = skills_dir / slug
            skill_dir.mkdir(parents=True, exist_ok=True)
            content = (
                f"---\nname: {slug}\ndescription: {page.title}\n---\n\n{page.body}\n"
            )
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
            written += 1
        if page.rel.startswith("modules/"):
            catalogue_lines.append(f"- **{page.title}** — see `{page.rel}` in the wiki")
    if catalogue_lines:
        agents_md = root / AGENTS_MD
        header = "<!-- agentcore-wiki:start -->"
        footer = "<!-- agentcore-wiki:end -->"
        block = f"\n{header}\n## Codebase wiki (auto-generated)\n\n" + "\n".join(catalogue_lines) + f"\n{footer}\n"
        existing = agents_md.read_text(encoding="utf-8") if agents_md.exists() else ""
        if header in existing and footer in existing:
            pre = existing.split(header)[0]
            post = existing.split(footer, 1)[1]
            agents_md.write_text(pre + block + post, encoding="utf-8")
        else:
            agents_md.write_text((existing.rstrip() + "\n" if existing else "") + block, encoding="utf-8")
    return written


def link_copilot_wiki(project_root: Path, storage) -> int:  # type: ignore[no-untyped-def]
    """Mirror wiki pages to `.github/prompts/*.prompt.md` and prepend a
    catalogue to `.github/copilot-instructions.md`. Returns prompt count.
    """
    root = Path(project_root)
    prompts_dir = root / COPILOT_PROMPTS_DIR
    prompts_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    catalogue: list[str] = []
    for page in storage.walk():
        if page.rel in {"index.md", "log.md"}:
            continue
        slug = _slugify(page.rel)
        (prompts_dir / f"{slug}.prompt.md").write_text(
            f"---\ntitle: {page.title}\n---\n\n{page.body}\n", encoding="utf-8"
        )
        catalogue.append(f"- **{page.title}** — `.github/prompts/{slug}.prompt.md`")
        n += 1
    instr = root / COPILOT_INSTRUCTIONS
    instr.parent.mkdir(parents=True, exist_ok=True)
    header = "<!-- agentcore-wiki:start -->"
    footer = "<!-- agentcore-wiki:end -->"
    block = f"{header}\n## Codebase wiki (auto-generated)\n\n" + "\n".join(catalogue) + f"\n{footer}\n"
    existing = instr.read_text(encoding="utf-8") if instr.exists() else ""
    if header in existing and footer in existing:
        pre = existing.split(header)[0]
        post = existing.split(footer, 1)[1]
        instr.write_text(pre + block + post, encoding="utf-8")
    else:
        instr.write_text(block + ("\n" + existing if existing else ""), encoding="utf-8")
    return n


def link_cursor_wiki(project_root: Path, storage) -> bool:  # type: ignore[no-untyped-def]
    """Write a single Cursor rule that points into the wiki tree."""
    root = Path(project_root)
    target = root / CURSOR_RULES_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Codebase wiki", "", "Curated reference for this codebase. Pages:", ""]
    any_pages = False
    for page in storage.walk():
        if page.rel in {"index.md", "log.md"}:
            continue
        any_pages = True
        lines.append(f"- `{page.rel}` — {page.title}")
    if not any_pages:
        return False
    lines.append("")
    lines.append("Resolve these via the orchestrator's `/wiki/{path}` endpoint or in the local `.agentcore/wiki/` tree.")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True
