"""Sandboxed worktree execution for pre-LLM executors.

The QA executor (and any other inline command declared in an agent.md)
runs here. Responsibilities:

  1. Create a temp git worktree off the project root so the live tree
     stays clean. Cheap — `git worktree add` shares the object DB with
     the source repo.
  2. Apply the developer's `FileDiff[]` (unified diffs) to the worktree.
     Falls back to raw post-image overwrite when the LLM's context
     anchors don't match.
  3. Run the requested command inside the worktree, capturing exit
     code + stdout + stderr tails (subprocess, never in-process).
  4. Optionally read a JSON artifact (e.g. pytest's `--json-report-file`)
     and merge it into the result.
  5. Always remove the worktree, even on exceptions.

No language detection, no install, no "smart" fallbacks. The agent.md
declares exactly what to run — runtime is generic.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


async def run_in_worktree(
    command: list[str],
    *,
    diffs: list[dict[str, Any]],
    repo_root: str | Path | None = None,
    timeout_seconds: int = 600,
    artifact: str | None = None,
) -> dict[str, Any]:
    """Apply `diffs` in a fresh worktree, run `command` inside it,
    return structured output.

    Result shape:
        {
          "executor_status": "ok" | "no_repo" | "timeout" | "error",
          "exit_code": int,
          "stdout_tail": str,        # last 2 KiB
          "stderr_tail": str,        # last 2 KiB
          "applied_files": list[str],
          "artifact": dict | None,   # parsed JSON if `artifact` set
          "command": str,
        }
    """
    repo = Path(
        repo_root or os.environ.get("AGENTCORE_REPO_ROOT", ".")
    ).resolve()
    if not (repo / ".git").is_dir():
        return _result(
            "no_repo",
            command=command,
            note=f"{repo} is not a git repo",
        )

    sandbox = Path(tempfile.mkdtemp(prefix="agentcore-exec-"))
    try:
        wt = sandbox / "wt"
        rc, _, err = await _run(
            ["git", "-C", str(repo), "worktree", "add", "--detach", str(wt)],
            timeout=60,
        )
        if rc != 0:
            return _result(
                "error",
                command=command,
                note=f"git worktree add failed: {err[:200]}",
            )

        applied = apply_in_worktree(wt, diffs)

        rc, out, err = await _run(command, cwd=wt, timeout=timeout_seconds)
        artifact_data: dict[str, Any] | None = None
        if artifact:
            apath = wt / artifact
            if apath.exists():
                try:
                    artifact_data = json.loads(apath.read_text(encoding="utf-8"))
                except Exception as exc:
                    log.warning(
                        "sandbox.artifact_parse_failed",
                        path=str(apath),
                        error=str(exc),
                    )

        return {
            "executor_status": "ok",
            "exit_code": rc,
            "stdout_tail": out[-2000:],
            "stderr_tail": err[-2000:],
            "applied_files": applied,
            "artifact": artifact_data,
            "command": " ".join(command),
        }
    except TimeoutError:
        return _result(
            "timeout",
            command=command,
            note=f"command exceeded {timeout_seconds}s",
        )
    except Exception as exc:
        return _result("error", command=command, note=str(exc))
    finally:
        with contextlib.suppress(Exception):
            await _run(
                ["git", "-C", str(repo), "worktree", "remove", "--force",
                 str(sandbox / "wt")],
                timeout=30,
            )
        shutil.rmtree(sandbox, ignore_errors=True)


def apply_in_worktree(
    wt: Path, diffs: list[dict[str, Any]]
) -> list[str]:
    """Apply each FileDiff via `git apply` inside `wt`. On context
    mismatch (LLMs sometimes invent context lines), fall back to
    raw post-image overwrite. Returns the list of paths actually
    written.
    """
    import subprocess

    applied: list[str] = []
    for d in diffs or []:
        if not isinstance(d, dict):
            continue
        path = d.get("path")
        diff_text = d.get("unified_diff")
        if not path or not diff_text:
            continue
        patch = wt / ".agentcore.patch"
        patch.write_text(diff_text, encoding="utf-8")
        proc = subprocess.run(
            ["git", "-C", str(wt), "apply", "--whitespace=nowarn", str(patch)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            applied.append(path)
        else:
            new_lines = [
                ln[1:] for ln in diff_text.splitlines()
                if ln.startswith("+") and not ln.startswith("+++")
            ]
            if new_lines:
                target = wt / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    "\n".join(new_lines) + "\n", encoding="utf-8"
                )
                applied.append(path + " (overwrite-fallback)")
        with contextlib.suppress(OSError):
            patch.unlink()
    return applied


def _result(
    status: str, *, command: list[str], note: str = "",
) -> dict[str, Any]:
    return {
        "executor_status": status,
        "exit_code": -1,
        "stdout_tail": "",
        "stderr_tail": "",
        "applied_files": [],
        "artifact": None,
        "command": " ".join(command),
        "note": note,
    }


async def _run(
    cmd: list[str], *, cwd: Path | None = None, timeout: int = 60
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return (
        proc.returncode or 0,
        (stdout or b"").decode("utf-8", errors="replace"),
        (stderr or b"").decode("utf-8", errors="replace"),
    )
