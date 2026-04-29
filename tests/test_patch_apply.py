"""Patch application must never corrupt files via lossy fallbacks."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agentcore.cli import _apply_diffs
from agentcore.runtime.sandbox import PatchApplyError, apply_in_worktree


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)


def test_cli_apply_diffs_does_not_overwrite_on_bad_context(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    target = repo / "calculator.py"
    target.write_text("def multiply(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "calculator.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True)

    bad_diff = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
-def missing(a, b):
-    return a + b
+def multiply(a, b):
+    return a * b
"""

    written = _apply_diffs(repo, [{"path": "calculator.py", "unified_diff": bad_diff}])

    assert written == []
    assert target.read_text(encoding="utf-8") == "def multiply(a, b):\n    return a + b\n"


def test_sandbox_apply_in_worktree_raises_on_bad_context(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    target = repo / "calculator.py"
    target.write_text("def multiply(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "calculator.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True)

    bad_diff = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1,2 +1,2 @@
-def missing(a, b):
-    return a + b
+def multiply(a, b):
+    return a * b
"""

    try:
        apply_in_worktree(repo, [{"path": "calculator.py", "unified_diff": bad_diff}])
    except PatchApplyError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("expected bad patch context to fail explicitly")

    assert target.read_text(encoding="utf-8") == "def multiply(a, b):\n    return a + b\n"
