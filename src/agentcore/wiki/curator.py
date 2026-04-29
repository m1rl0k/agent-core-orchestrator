"""Wiki curator: ingest + incremental + lint.

Three modes — all driven by `LLMRouter` so they pick up provider auto-resolution
and the thinking-token strip for free.

Seed (`seed_from_repo`)
    Walk the repo, group files by top-level package, ask the LLM to produce
    one `modules/<pkg>.md` page per group. Cheap; runs once per major reset.

Incremental (`incremental`)
    Given a list of changed file paths (e.g. from a post-commit hook), find
    every wiki page whose `sources[]` overlaps that list. Ask the LLM to
    revise *only those* pages. Skips no-op rewrites via the storage layer's
    content-hash check.

Lint (`lint`)
    Walk the wiki and emit a structured report:
      - **orphans:** every `sources[]` entry has been deleted from the repo
      - **stale:** any source on disk is newer than `last_updated`
      - **missing_coverage:** top-level package with no `modules/<pkg>.md`
    Findings are appended to `log.md` and surface in the response payload.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from agentcore.llm.router import ChatMessage, LLMRouter
from agentcore.spec.models import ModelConfig
from agentcore.wiki.index import WikiIndex
from agentcore.wiki.storage import WikiPage, WikiStorage

log = structlog.get_logger(__name__)

_DEFAULT_GLOB = "**/*.py"
_MAX_BODY_PER_FILE = 18000  # chars piped into the prompt per file
_MAX_FILES_PER_MODULE = 100


@dataclass(slots=True)
class LintReport:
    orphans: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    missing_coverage: list[str] = field(default_factory=list)
    pages_updated: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.orphans or self.stale or self.missing_coverage)

    def to_log_lines(self) -> list[str]:
        lines: list[str] = []
        ts = datetime.now(UTC).isoformat(timespec="seconds")
        if self.orphans:
            lines.append(f"- {ts}  orphans: {', '.join(self.orphans)}")
        if self.stale:
            lines.append(f"- {ts}  stale: {', '.join(self.stale)}")
        if self.missing_coverage:
            lines.append(f"- {ts}  missing coverage: {', '.join(self.missing_coverage)}")
        return lines


class WikiCurator:
    """Owns the LLM-mediated lifecycle of one (project, branch) wiki."""

    def __init__(
        self,
        router: LLMRouter,
        storage: WikiStorage,
        index: WikiIndex,
        *,
        curator_model: str = "glm-4.6",
        curator_provider: str = "zai",
    ) -> None:
        self.router = router
        self.storage = storage
        self.index = index
        self.curator_model = curator_model
        self.curator_provider = curator_provider

    # ---- mode 1: seed -------------------------------------------------

    async def seed_from_repo(
        self,
        repo_root: Path | str,
        *,
        glob: str = _DEFAULT_GLOB,
        commit_sha: str | None = None,
        force: bool = False,
    ) -> list[str]:
        """Bulk-produce module pages for every top-level package under `repo_root`.

        Smart by default: when an on-disk page already exists for a module
        and none of its `sources[]` have been modified since the page was
        written, the LLM call is skipped entirely. Pass `force=True` to
        re-render every module unconditionally (useful after a prompt or
        contract change).
        """
        groups = self._group_files_by_module(Path(repo_root), glob)
        written: list[str] = []
        skipped_unchanged = 0
        for module, files in groups.items():
            rel = f"modules/{module}.md"
            existing = self.storage.read(rel)
            if not force and existing is not None and not self._needs_refresh(
                existing, files
            ):
                skipped_unchanged += 1
                continue
            page = await self._render_module_page(module, files, repo_root)
            if page is None:
                continue
            written_page = self.storage.write(page, commit_sha=commit_sha)
            if written_page is not None:
                await self.index.upsert_page(written_page)
                written.append(written_page.rel)
        if skipped_unchanged:
            log.info(
                "wiki.seed_skipped_unchanged",
                modules=skipped_unchanged,
                hint="re-run with `--force` to regenerate every page",
            )
        # Always (re)write the index page after a seed — it summarises what's there.
        idx = self._render_index_page()
        written_idx = self.storage.write(idx, commit_sha=commit_sha)
        if written_idx is not None:
            await self.index.upsert_page(written_idx)
            written.append(written_idx.rel)
        return written

    # ---- mode 2: incremental -----------------------------------------

    async def incremental(
        self,
        changed_paths: Iterable[str],
        repo_root: Path | str,
        *,
        commit_sha: str | None = None,
    ) -> list[str]:
        """Revise wiki pages whose `sources[]` overlaps `changed_paths`."""
        changed = {str(p) for p in changed_paths}
        if not changed:
            return []
        repo = Path(repo_root)
        affected: list[WikiPage] = []
        for page in self.storage.walk():
            if set(page.sources) & changed:
                affected.append(page)
        written: list[str] = []
        for page in affected:
            files = [(s, repo / s) for s in page.sources]
            files = [(s, p) for s, p in files if p.exists()]
            if not files:
                # All sources gone — let lint() handle it as an orphan.
                continue
            new_body = await self._revise_page_body(page, files)
            if new_body is None:
                continue
            new_page = WikiPage(
                rel=page.rel, frontmatter=dict(page.frontmatter), body=new_body
            )
            written_page = self.storage.write(new_page, commit_sha=commit_sha)
            if written_page is not None:
                await self.index.upsert_page(written_page)
                written.append(written_page.rel)
        return written

    # ---- mode 3: lint -------------------------------------------------

    def lint(self, repo_root: Path | str) -> LintReport:
        """Pure, deterministic lint pass — no LLM calls."""
        repo = Path(repo_root)
        report = LintReport()
        seen_modules: set[str] = set()

        for page in self.storage.walk():
            sources = page.sources
            if sources:
                missing = [s for s in sources if not (repo / s).exists()]
                if missing and len(missing) == len(sources):
                    report.orphans.append(page.rel)
                    continue
                if missing:
                    # Partial orphan: at least one source still exists,
                    # but others are gone. Surface as stale so the
                    # curator regenerates the page from the surviving
                    # subset instead of silently retaining content
                    # describing deleted files.
                    report.stale.append(page.rel)
                    continue
                last_updated = page.frontmatter.get("last_updated")
                if last_updated and self._sources_newer_than(repo, sources, last_updated):
                    report.stale.append(page.rel)
            if page.rel.startswith("modules/"):
                seen_modules.add(Path(page.rel).stem)

        # Missing coverage: top-level python package present in repo but no
        # corresponding modules/<pkg>.md.
        for pkg in self._discover_top_level_packages(repo):
            if pkg not in seen_modules:
                report.missing_coverage.append(pkg)

        if not report.is_empty():
            self._append_log(report.to_log_lines())
        return report

    # ---- LLM helpers -------------------------------------------------

    def _curator_cfg(self, max_tokens: int) -> ModelConfig:
        return ModelConfig(
            provider=self.curator_provider,  # type: ignore[arg-type]
            model=self.curator_model,
            temperature=0.2,
            max_tokens=max_tokens,
        )

    async def _render_module_page(
        self,
        module: str,
        files: list[Path],
        repo_root: Path | str,
    ) -> WikiPage | None:
        repo = Path(repo_root)
        rel_sources: list[str] = []
        bundles: list[str] = []
        for f in files[:_MAX_FILES_PER_MODULE]:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = f.relative_to(repo).as_posix()
            rel_sources.append(rel)
            bundles.append(f"### {rel}\n```python\n{text[:_MAX_BODY_PER_FILE]}\n```")
        if not bundles:
            return None
        prompt = (
            f"You are documenting the `{module}` module of a Python codebase. "
            "Read the source files below and produce a concise wiki page (markdown, "
            "≤500 words) describing: what this module is responsible for, its key "
            "public types/functions, how it fits with the rest of the system, and "
            "any non-obvious conventions. No code blocks. No 'introduction' fluff. "
            "Lead with a one-sentence summary.\n\n"
            + "\n\n".join(bundles)
        )
        try:
            resp = await self.router.complete(
                [ChatMessage(role="user", content=prompt)],
                self._curator_cfg(max_tokens=32000),
            )
        except Exception as exc:
            log.warning("wiki.module_render_failed", module=module, error=str(exc))
            return None
        body = resp.text.strip()
        if not body:
            log.warning(
                "wiki.module_render_empty",
                module=module,
                resp_len=len(resp.text or ""),
                resp_head=(resp.text or "")[:200],
            )
            return None
        return WikiPage(
            rel=f"modules/{module}.md",
            frontmatter={
                "title": f"{module} module",
                "sources": rel_sources,
                "status": "drafting",
            },
            body=body,
        )

    async def _revise_page_body(
        self, page: WikiPage, files: list[tuple[str, Path]]
    ) -> str | None:
        bundles: list[str] = []
        for rel, p in files[:_MAX_FILES_PER_MODULE]:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            bundles.append(f"### {rel}\n```python\n{text[:_MAX_BODY_PER_FILE]}\n```")
        if not bundles:
            return None
        prompt = (
            f"Revise this wiki page to reflect the latest source. Keep what's still "
            f"accurate, update what's changed, drop what's obsolete. Same rough "
            f"shape and length. Markdown, no code blocks, lead with the summary.\n\n"
            f"=== current page ({page.rel}) ===\n{page.body}\n\n"
            f"=== current sources ===\n" + "\n\n".join(bundles)
        )
        try:
            resp = await self.router.complete(
                [ChatMessage(role="user", content=prompt)],
                self._curator_cfg(max_tokens=32000),
            )
        except Exception as exc:
            log.warning("wiki.revise_failed", rel=page.rel, error=str(exc))
            return None
        return resp.text.strip() or None

    # ---- discovery helpers -------------------------------------------

    @staticmethod
    def _group_files_by_module(repo_root: Path, glob: str) -> dict[str, list[Path]]:
        groups: dict[str, list[Path]] = {}
        # Prefer src layout if present.
        roots = [repo_root / "src"] if (repo_root / "src").is_dir() else [repo_root]
        for root in roots:
            for p in root.glob(glob):
                if not p.is_file():
                    continue
                # First directory under root after `src/` is the package name.
                rel_parts = p.relative_to(root).parts
                if not rel_parts:
                    continue
                pkg = rel_parts[0]
                if pkg.startswith((".", "_")) or pkg in {"tests", "test"}:
                    continue
                groups.setdefault(pkg, []).append(p)
        # Drill in: if a package has subpackages with > 5 files, split them out.
        for pkg in list(groups.keys()):
            files = groups[pkg]
            if len(files) <= 8:
                continue
            sub_groups: dict[str, list[Path]] = {}
            for f in files:
                # Find the level beneath the top package
                parts = f.parts
                try:
                    idx = parts.index(pkg)
                    sub = parts[idx + 1] if idx + 1 < len(parts) - 1 else "_root"
                except ValueError:
                    sub = "_root"
                key = pkg if sub in ("_root", "") else f"{pkg}.{sub}"
                sub_groups.setdefault(key, []).append(f)
            del groups[pkg]
            groups.update(sub_groups)
        return groups

    @staticmethod
    def _discover_top_level_packages(repo_root: Path) -> set[str]:
        """Return top-level package names (used to find missing coverage)."""
        roots = [repo_root / "src"] if (repo_root / "src").is_dir() else [repo_root]
        out: set[str] = set()
        for root in roots:
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if not child.is_dir():
                    continue
                if child.name.startswith((".", "_")) or child.name in {"tests", "test"}:
                    continue
                # Treat as a package if it has any python file.
                if any(child.rglob("*.py")):
                    out.add(child.name)
        return out

    # Sub-second slack: `last_updated` is stored at second precision, but
    # filesystem mtimes are sub-second. Without slack, a source written
    # immediately before the page's audit timestamp tests as "newer" by
    # ~0.x seconds and gets falsely flagged as stale.
    _STALE_SLACK_SECONDS = 2.0

    @staticmethod
    def _needs_refresh(existing: WikiPage, candidate_files: list[Path]) -> bool:
        """Decide whether an existing module page should be re-rendered.

        True iff:
          - the page has no `last_updated` timestamp (legacy page), OR
          - the set of source files changed (added/removed module files), OR
          - any current source file's mtime is newer than `last_updated`
            beyond the slack window.
        """
        last_updated = existing.frontmatter.get("last_updated")
        if not last_updated:
            return True
        recorded = {Path(s).as_posix() for s in existing.sources}
        current = {p.as_posix() for p in candidate_files}
        if recorded and recorded != current:
            return True
        try:
            cutoff = datetime.fromisoformat(last_updated).timestamp()
        except (TypeError, ValueError):
            return True
        for p in candidate_files:
            try:
                if p.stat().st_mtime > cutoff + WikiCurator._STALE_SLACK_SECONDS:
                    return True
            except OSError:
                continue
        return False

    @staticmethod
    def _sources_newer_than(
        repo: Path, sources: list[str], last_updated_iso: str
    ) -> bool:
        try:
            cutoff = datetime.fromisoformat(last_updated_iso).timestamp()
        except (TypeError, ValueError):
            return False
        for s in sources:
            f = repo / s
            try:
                if f.stat().st_mtime > cutoff + WikiCurator._STALE_SLACK_SECONDS:
                    return True
            except OSError:
                continue
        return False

    def _render_index_page(self) -> WikiPage:
        """Auto-generate a tiny index.md catalogue from what's on disk."""
        entries: list[str] = []
        for page in self.storage.walk():
            if page.rel == "index.md":
                continue
            entries.append(f"- [[{page.rel}|{page.title}]]")
        body = (
            f"# {self.storage.project} · {self.storage.branch} wiki\n\n"
            + ("\n".join(entries) if entries else "_(empty — run `agentcore wiki rebuild` to seed)_")
        )
        return WikiPage(
            rel="index.md",
            frontmatter={"title": f"{self.storage.project} wiki", "status": "stable"},
            body=body,
        )

    def _append_log(self, lines: list[str]) -> None:
        """Append findings to log.md, creating it if needed.

        Append-mode write so concurrent lint passes (post-commit hook +
        scheduled scan) cannot overwrite each other's lines via
        read-modify-write.
        """
        log_path = self.storage.page_path("log.md")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not log_path.exists():
            log_path.write_text(
                "---\ntitle: changelog\nstatus: stable\n---\n\n# Wiki changelog\n\n",
                encoding="utf-8",
            )
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
