"""WikiCurator.lint(): orphan / stale / missing-coverage detection.

The lint pass is pure (no LLM calls), so we exercise it with a temp repo and
a freshly-seeded WikiStorage. The router is a stub since lint never calls it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from agentcore.wiki.curator import WikiCurator
from agentcore.wiki.index import WikiIndex
from agentcore.wiki.storage import WikiPage, WikiStorage


class _StubRouter:
    """Has the same shape as LLMRouter for our purposes; never invoked by lint."""

    async def complete(self, *_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("lint should never call the LLM")


def _curator(tmp_path: Path) -> tuple[WikiStorage, WikiCurator]:
    storage = WikiStorage(tmp_path / "wiki", "proj", "main")
    index = WikiIndex(storage, embedder=None, vector=None)
    cur = WikiCurator(_StubRouter(), storage, index)  # type: ignore[arg-type]
    return storage, cur


def _make_repo(repo: Path, files: list[str]) -> None:
    for rel in files:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# stub\n", encoding="utf-8")


def test_lint_flags_orphan_when_all_sources_deleted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _make_repo(repo, ["src/agentcore/__init__.py"])
    storage, cur = _curator(tmp_path)
    storage.write(
        WikiPage(
            rel="modules/dead.md",
            frontmatter={"sources": ["src/agentcore/gone.py", "src/agentcore/also_gone.py"]},
            body="describes deleted code",
        )
    )
    report = cur.lint(repo)
    assert "modules/dead.md" in report.orphans
    assert report.stale == []


def test_lint_flags_stale_when_source_newer_than_page(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    src = repo / "src/agentcore/foo.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# stub", encoding="utf-8")
    storage, cur = _curator(tmp_path)
    storage.write(
        WikiPage(
            rel="modules/foo.md",
            frontmatter={"sources": ["src/agentcore/foo.py"]},
            body="initial",
        )
    )
    # Make the source newer than the page's last_updated.
    time.sleep(0.05)
    new_mtime = time.time()
    os.utime(src, (new_mtime, new_mtime + 60))  # one minute in the future
    report = cur.lint(repo)
    assert "modules/foo.md" in report.stale


def test_lint_flags_missing_coverage_for_uncovered_packages(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _make_repo(
        repo,
        [
            "src/alpha/__init__.py",
            "src/alpha/x.py",
            "src/beta/__init__.py",
            "src/beta/y.py",
        ],
    )
    storage, cur = _curator(tmp_path)
    # Only alpha is documented.
    storage.write(
        WikiPage(
            rel="modules/alpha.md",
            frontmatter={"sources": ["src/alpha/x.py"]},
            body="alpha doc",
        )
    )
    report = cur.lint(repo)
    assert "beta" in report.missing_coverage
    assert "alpha" not in report.missing_coverage


def test_lint_skips_test_and_dunder_dirs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _make_repo(
        repo,
        [
            "src/_private/__init__.py",
            "src/tests/__init__.py",
            "src/.hidden/__init__.py",
            "src/realpkg/__init__.py",
            "src/realpkg/m.py",
        ],
    )
    _storage, cur = _curator(tmp_path)
    report = cur.lint(repo)
    assert report.missing_coverage == ["realpkg"]


def test_lint_appends_to_log_when_findings_exist(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _make_repo(repo, ["src/realpkg/__init__.py", "src/realpkg/m.py"])
    storage, cur = _curator(tmp_path)
    cur.lint(repo)  # should produce a missing-coverage finding
    log_text = storage.page_path("log.md").read_text(encoding="utf-8")
    assert "missing coverage" in log_text


def test_lint_is_quiet_when_clean(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _make_repo(repo, ["src/alpha/__init__.py", "src/alpha/x.py"])
    storage, cur = _curator(tmp_path)
    storage.write(
        WikiPage(
            rel="modules/alpha.md",
            frontmatter={"sources": ["src/alpha/x.py"]},
            body="alpha doc",
        )
    )
    report = cur.lint(repo)
    assert report.is_empty()
    # And no log.md was created.
    assert not storage.page_path("log.md").exists()
