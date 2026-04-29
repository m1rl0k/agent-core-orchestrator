"""Polyglot validation runner.

The QA agent declares `executors: [tests]` (or `[validate]`) and the
runtime calls `run_validation()` here. This isn't just tests — many
real codebases (Next.js, CLI tools, libraries) ship without a test
suite but DO ship with lint + typecheck. We run every signal we can
find, never install anything, and report all of them so the QA LLM
has ground truth from whatever the project actually exposes.

Per language we detect a menu of signals:

  - Python:    pytest, ruff, mypy
  - Node/TS:   package.json scripts (test|lint|typecheck|build),
               eslint, tsc --noEmit
  - Go:        go test, go vet, go build
  - Rust:      cargo test, cargo clippy, cargo check
  - Java:      mvn test / gradle test, mvn compile / gradle build
  - .NET:      dotnet test, dotnet build
  - C/C++:     ctest (when build dir present)
  - Ruby:      bundle exec rake test, rspec, rubocop
  - PHP:       phpunit, phpcs

Each signal runs only when (a) the marker file/dir exists in the repo
AND (b) the runner binary is already on PATH. Missing signals are
skipped silently — never installed.

Why a temp git worktree (not the live repo): half-applied QA runs
shouldn't dirty the user's tree, and we want clean reproducibility
across re-review loops. `git worktree add` shares the object DB so
this is cheap.

Why subprocess: in-process pytest pollutes the importer / event loop;
subprocess gives clean isolation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class Signal:
    kind: str  # "tests" | "lint" | "typecheck" | "build"
    lang: str
    command: list[str]
    parse: str = "exit_code"  # "pytest_json" | "go_json" | "cargo_json" | "exit_code"
    artifact: str = ""  # output file the parser reads (relative to wt)


async def run_validation(
    diffs: list[dict[str, Any]],
    *,
    repo_root: str | Path | None = None,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Apply `diffs` in a temp worktree, run every detected signal in
    parallel, return a combined result. Output shape stays consistent
    so the QA contract is stable across stacks.
    """
    repo = Path(repo_root or os.environ.get("AGENTCORE_REPO_ROOT", ".")).resolve()
    if not (repo / ".git").is_dir():
        return _result("no_repo", note=f"{repo} is not a git repo")

    sandbox = Path(tempfile.mkdtemp(prefix="agentcore-qa-"))
    try:
        wt = sandbox / "wt"
        rc, _, err = await _run(
            ["git", "-C", str(repo), "worktree", "add", "--detach", str(wt)],
            timeout=60,
        )
        if rc != 0:
            return _result("error", note=f"git worktree add failed: {err[:200]}")

        applied, apply_err = _apply_diffs(wt, diffs)
        if apply_err:
            log.info("validation.apply_warning", error=apply_err[:200])

        signals = detect_signals(wt)
        if not signals:
            return _result(
                "no_runner",
                note="no testable / lintable / typeable signals found on PATH",
                applied=applied,
            )

        # Run every signal concurrently — they're independent commands,
        # different processes. Per-signal timeout slice keeps a slow
        # build from starving a fast lint.
        per = max(60, timeout_seconds // max(1, len(signals)))
        outcomes = await asyncio.gather(
            *(_run_signal(s, wt, timeout=per) for s in signals),
            return_exceptions=True,
        )
        results: list[dict[str, Any]] = []
        for s, out in zip(signals, outcomes, strict=True):
            if isinstance(out, BaseException):
                results.append({
                    "kind": s.kind, "lang": s.lang,
                    "command": " ".join(s.command),
                    "executor_status": "error", "note": str(out),
                    "passed": [], "failed": [],
                })
            else:
                results.append(out)

        passed_all = [t for r in results for t in r.get("passed", [])]
        failed_all = [t for r in results for t in r.get("failed", [])]
        return {
            "executor_status": "ok",
            "signals": results,
            "passed": passed_all,
            "failed": failed_all,
            "all_passed": all(r.get("exit_code", 1) == 0 for r in results),
            "applied_files": applied,
            "coverage_pct": _first_coverage(results),
        }
    except Exception as exc:
        return _result("error", note=str(exc))
    finally:
        with contextlib.suppress(Exception):
            await _run(
                ["git", "-C", str(repo), "worktree", "remove", "--force",
                 str(sandbox / "wt")],
                timeout=30,
            )
        shutil.rmtree(sandbox, ignore_errors=True)


# Backwards-compat aliases.
run_tests = run_validation
run_pytest = run_validation


async def _run_signal(
    signal: Signal, wt: Path, *, timeout: int
) -> dict[str, Any]:
    rc, out, err = await _run(signal.command, cwd=wt, timeout=timeout)
    parsed = _parse_output(signal, wt, rc, out, err)
    parsed.update({
        "kind": signal.kind,
        "lang": signal.lang,
        "command": " ".join(signal.command),
    })
    return parsed


# ---------------------------------------------------------------------------
# Detection — collect every signal whose binary is already on PATH.
# ---------------------------------------------------------------------------


def detect_signals(repo: Path) -> list[Signal]:
    """Return every Signal whose marker exists in the repo AND whose
    binary is on PATH. Empty list means no validation possible — caller
    reports `no_runner` and the QA LLM proceeds without ground truth.
    """
    out: list[Signal] = []
    has = lambda b: shutil.which(b) is not None  # noqa: E731

    # ---- Python -----------------------------------------------------
    py_marker = (
        (repo / "pyproject.toml").exists()
        or (repo / "pytest.ini").exists()
        or (repo / "setup.py").exists()
        or any(repo.glob("test_*.py"))
        or (repo / "tests").is_dir()
    )
    if py_marker:
        if has("uv"):
            out.append(Signal(
                "tests", "python",
                ["uv", "run", "--quiet", "pytest", "-q",
                 "-p", "no:cacheprovider",
                 "--json-report", "--json-report-file=.agentcore-pytest.json"],
                parse="pytest_json", artifact=".agentcore-pytest.json",
            ))
        elif has("pytest"):
            out.append(Signal(
                "tests", "python",
                ["pytest", "-q", "-p", "no:cacheprovider",
                 "--json-report", "--json-report-file=.agentcore-pytest.json"],
                parse="pytest_json", artifact=".agentcore-pytest.json",
            ))
        if has("ruff"):
            out.append(Signal(
                "lint", "python", ["ruff", "check", "--output-format=concise", "."],
            ))
        if has("mypy"):
            out.append(Signal(
                "typecheck", "python", ["mypy", "--no-color-output", "."],
            ))

    # ---- Node / JS / TS --------------------------------------------
    pkg_path = repo / "package.json"
    if pkg_path.exists():
        pkg = _read_json(pkg_path) or {}
        scripts = pkg.get("scripts") or {}
        runner = next(
            (r for r in ("pnpm", "yarn", "npm") if has(r)), None,
        )
        if runner:
            # Map common script names to signal kinds. We honour whatever
            # the project ACTUALLY defines — if there's no `test` script
            # we don't error, we just don't add the signal.
            for script_name, kind in (
                ("test", "tests"),
                ("lint", "lint"),
                ("typecheck", "typecheck"),
                ("type-check", "typecheck"),
                ("build", "build"),
            ):
                if script_name in scripts:
                    out.append(Signal(
                        kind, "node",
                        [runner, "run", script_name]
                        if runner != "npm"
                        else [runner, "run", script_name, "--silent"],
                    ))
        # Direct fallbacks if no scripts defined: bare eslint / tsc on PATH.
        if not any(s.kind == "lint" for s in out) and has("eslint"):
            out.append(Signal("lint", "node", ["eslint", "."]))
        if not any(s.kind == "typecheck" for s in out) and has("tsc") \
                and (repo / "tsconfig.json").exists():
            out.append(Signal("typecheck", "node", ["tsc", "--noEmit"]))

    # ---- Go ---------------------------------------------------------
    if (repo / "go.mod").exists() and has("go"):
        out.append(Signal("tests", "go", ["go", "test", "-json", "./..."],
                          parse="go_json"))
        out.append(Signal("lint", "go", ["go", "vet", "./..."]))
        out.append(Signal("build", "go", ["go", "build", "./..."]))

    # ---- Rust -------------------------------------------------------
    if (repo / "Cargo.toml").exists() and has("cargo"):
        out.append(Signal(
            "tests", "rust",
            ["cargo", "test", "--message-format=json", "--quiet"],
            parse="cargo_json",
        ))
        out.append(Signal("lint", "rust",
                          ["cargo", "clippy", "--quiet", "--",
                           "-D", "warnings"]))
        out.append(Signal("build", "rust", ["cargo", "check", "--quiet"]))

    # ---- Java / Kotlin ---------------------------------------------
    if (repo / "pom.xml").exists() and has("mvn"):
        out.append(Signal("tests", "java", ["mvn", "-q", "test"]))
        out.append(Signal("build", "java", ["mvn", "-q", "compile"]))
    elif (repo / "build.gradle").exists() or (repo / "build.gradle.kts").exists():
        gradle = (
            ["./gradlew"] if (repo / "gradlew").exists()
            else (["gradle"] if has("gradle") else None)
        )
        if gradle is not None:
            out.append(Signal("tests", "java", [*gradle, "test", "--quiet"]))
            out.append(Signal("build", "java", [*gradle, "build", "--quiet"]))

    # ---- .NET -------------------------------------------------------
    if (any(repo.glob("*.csproj")) or any(repo.glob("*.sln"))) and has("dotnet"):
        out.append(Signal(
            "tests", "dotnet",
            ["dotnet", "test", "--nologo", "--verbosity", "quiet"],
        ))
        out.append(Signal(
            "build", "dotnet",
            ["dotnet", "build", "--nologo", "--verbosity", "quiet"],
        ))

    # ---- C/C++ via CMake -------------------------------------------
    if (repo / "CMakeLists.txt").exists() and has("ctest"):
        for build_dir in ("build", "out/build", "cmake-build-debug"):
            if (repo / build_dir).is_dir():
                out.append(Signal(
                    "tests", "cpp",
                    ["ctest", "--test-dir", build_dir, "--output-on-failure"],
                ))
                break

    # ---- Ruby -------------------------------------------------------
    if (repo / "Gemfile").exists() and has("bundle"):
        if (repo / "Rakefile").exists():
            out.append(Signal(
                "tests", "ruby", ["bundle", "exec", "rake", "test"],
            ))
        else:
            out.append(Signal(
                "tests", "ruby",
                ["bundle", "exec", "rspec", "--format", "doc"],
            ))
        if (repo / ".rubocop.yml").exists():
            out.append(Signal("lint", "ruby", ["bundle", "exec", "rubocop"]))

    # ---- PHP --------------------------------------------------------
    if (repo / "composer.json").exists():
        if (repo / "vendor/bin/phpunit").exists():
            out.append(Signal(
                "tests", "php",
                ["vendor/bin/phpunit", "--no-coverage"],
            ))
        if (repo / "vendor/bin/phpcs").exists():
            out.append(Signal("lint", "php", ["vendor/bin/phpcs"]))

    return out


# ---------------------------------------------------------------------------
# Output parsers
# ---------------------------------------------------------------------------


def _parse_output(
    signal: Signal, wt: Path, rc: int, stdout: str, stderr: str
) -> dict[str, Any]:
    if signal.parse == "pytest_json":
        report_path = wt / signal.artifact
        if report_path.exists():
            try:
                data = json.loads(report_path.read_text(encoding="utf-8"))
                tests = data.get("tests") or []
                passed = [t["nodeid"] for t in tests if t.get("outcome") == "passed"]
                failed = [
                    {
                        "name": t.get("nodeid", "?"),
                        "error": (t.get("call", {}) or {})
                                  .get("longrepr", "")[:1000],
                        "suggestion": "",
                    }
                    for t in tests if t.get("outcome") == "failed"
                ]
                return _ok(
                    passed, failed, rc, stdout, stderr,
                    coverage=(data.get("coverage", {}) or {}).get("total"),
                )
            except Exception as exc:
                log.warning("validation.pytest_parse_failed", error=str(exc))
        return _exit_code(rc, stdout, stderr,
                          note="pytest-json-report missing")

    if signal.parse == "go_json":
        passed: list[str] = []
        failed: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("Action") == "pass" and evt.get("Test"):
                passed.append(f"{evt.get('Package','')}::{evt['Test']}")
            elif evt.get("Action") == "fail" and evt.get("Test"):
                failed.append({
                    "name": f"{evt.get('Package','')}::{evt['Test']}",
                    "error": (evt.get("Output", "") or "")[:1000],
                    "suggestion": "",
                })
        return _ok(passed, failed, rc, stdout, stderr)

    if signal.parse == "cargo_json":
        passed = []
        failed = []
        for line in stdout.splitlines():
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") != "test":
                continue
            name = evt.get("name", "?")
            if evt.get("event") == "ok":
                passed.append(name)
            elif evt.get("event") == "failed":
                failed.append({
                    "name": name,
                    "error": (evt.get("stdout") or "")[:1000],
                    "suggestion": "",
                })
        return _ok(passed, failed, rc, stdout, stderr)

    return _exit_code(rc, stdout, stderr)


def _first_coverage(results: list[dict[str, Any]]) -> float | None:
    for r in results:
        cov = r.get("coverage_pct")
        if isinstance(cov, (int, float)):
            return float(cov)
    return None


def _ok(
    passed: list[str], failed: list[dict[str, Any]], rc: int,
    stdout: str, stderr: str, *, coverage: float | None = None,
) -> dict[str, Any]:
    return {
        "executor_status": "ok",
        "passed": passed,
        "failed": failed,
        "coverage_pct": (
            float(coverage) if isinstance(coverage, (int, float)) else None
        ),
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
        "exit_code": rc,
    }


def _exit_code(
    rc: int, stdout: str, stderr: str, *, note: str = "",
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "executor_status": "ok",
        "passed": [],
        "failed": [] if rc == 0 else [
            {
                "name": "<unknown>",
                "error": (stderr or stdout)[-500:],
                "suggestion": (
                    "Runner did not emit structured output; "
                    "see stdout_tail / stderr_tail."
                ),
            }
        ],
        "coverage_pct": None,
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
        "exit_code": rc,
    }
    if note:
        out["note"] = note
    return out


def _result(
    status: str, *, note: str = "", applied: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "executor_status": status,
        "signals": [],
        "passed": [],
        "failed": [],
        "all_passed": False,
        "coverage_pct": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "applied_files": applied or [],
        "note": note,
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Diff application + subprocess shims
# ---------------------------------------------------------------------------


def _apply_diffs(
    wt: Path, diffs: list[dict[str, Any]]
) -> tuple[list[str], str]:
    applied: list[str] = []
    last_err = ""
    for d in diffs or []:
        if not isinstance(d, dict):
            continue
        path = d.get("path")
        diff_text = d.get("unified_diff")
        if not path or not diff_text:
            continue
        patch = wt / ".agentcore.patch"
        patch.write_text(diff_text, encoding="utf-8")
        rc, _, err = _run_sync(
            ["git", "-C", str(wt), "apply", "--whitespace=nowarn", str(patch)]
        )
        if rc == 0:
            applied.append(path)
        else:
            last_err = err
            new_lines = [
                ln[1:] for ln in diff_text.splitlines()
                if ln.startswith("+") and not ln.startswith("+++")
            ]
            if new_lines:
                target = wt / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                applied.append(path + " (overwrite-fallback)")
        with contextlib.suppress(OSError):
            patch.unlink()
    return applied, last_err


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
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return (
        proc.returncode or 0,
        (stdout or b"").decode("utf-8", errors="replace"),
        (stderr or b"").decode("utf-8", errors="replace"),
    )


def _run_sync(cmd: list[str]) -> tuple[int, str, str]:
    import subprocess

    p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return p.returncode, p.stdout, p.stderr
