"""Registry behaviour: load + per-file errors don't poison siblings."""

from __future__ import annotations

from pathlib import Path

from agentcore.spec.loader import AgentRegistry

GOOD = """\
---
name: dev
description: A developer.
soul:
  role: developer
contract:
  inputs:
    - { name: x, type: string, required: true }
  outputs:
    - { name: y, type: string, required: true }
  accepts_handoff_from: [user]
  delegates_to: []
---

Body.
"""

BROKEN = """\
---
name: 'invalid name'
soul:
  role: ops
---
"""


def test_load_dir_skips_broken_but_keeps_good(tmp_path: Path) -> None:
    (tmp_path / "dev.agent.md").write_text(GOOD)
    (tmp_path / "broken.agent.md").write_text(BROKEN)

    reg = AgentRegistry()
    reg.load_dir(tmp_path)

    assert reg.get("dev") is not None
    assert "invalid" in next(iter(reg.errors().values())).lower()
    assert len(reg.all()) == 1
