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
