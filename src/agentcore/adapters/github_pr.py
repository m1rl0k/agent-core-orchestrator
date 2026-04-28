"""GitHub adapter — wraps the host's `gh` CLI.

Capabilities:
  - poll a repo for PRs needing review (Signal: github_pr / pr_opened)
  - poll for failing workflow runs (Signal: github_pipeline / workflow_failed)
  - read a PR (diff, comments)
  - post review comments
  - open new PRs (used by Ops to ship remediations)

All operations require AGENTCORE_ENABLE_GITHUB=true AND `gh auth status` ok.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from agentcore.adapters.base import Adapter
from agentcore.contracts.domain import Signal


class GithubAdapter(Adapter):
    name = "github"
    cli = "gh"

    def __init__(self, capability, repo: str | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(capability)
        self.repo = repo  # "owner/name" or None to use cwd's default

    def short_status(self) -> str:
        if not self.capability.enabled:
            return "github: disabled"
        if not self.capability.installed:
            return f"github: missing CLI — {self.capability.install_hint}"
        if not self.capability.authenticated:
            return f"github: not authenticated — {self.capability.auth_hint}"
        target = self.repo or "(default repo)"
        return f"github: ready · {target}"

    # ---- reads ----------------------------------------------------------

    def list_open_prs(self) -> list[dict]:
        if not self.is_ready:
            return []
        args = ["gh", "pr", "list", "--state", "open", "--json", "number,title,author,headRefName,updatedAt"]
        if self.repo:
            args += ["--repo", self.repo]
        rc, out, _ = self._shell(args)
        if rc != 0:
            return []
        return json.loads(out or "[]")

    def list_failed_workflow_runs(self, limit: int = 10) -> list[dict]:
        if not self.is_ready:
            return []
        args = [
            "gh", "run", "list", "--status", "failure", "--limit", str(limit),
            "--json", "databaseId,name,conclusion,headBranch,createdAt,workflowName",
        ]
        if self.repo:
            args += ["--repo", self.repo]
        rc, out, _ = self._shell(args)
        if rc != 0:
            return []
        return json.loads(out or "[]")

    def read_pr(self, pr_number: int) -> dict:
        args = ["gh", "pr", "view", str(pr_number), "--json",
                "number,title,body,author,headRefName,baseRefName,additions,deletions,files"]
        if self.repo:
            args += ["--repo", self.repo]
        rc, out, _ = self._shell(args)
        if rc != 0:
            return {}
        return json.loads(out or "{}")

    # ---- writes ---------------------------------------------------------

    def comment_on_pr(self, pr_number: int, body: str) -> bool:
        if not self.is_ready:
            return False
        args = ["gh", "pr", "comment", str(pr_number), "--body", body]
        if self.repo:
            args += ["--repo", self.repo]
        rc, _, _ = self._shell(args, timeout=60.0)
        return rc == 0

    def open_pr(self, *, title: str, body: str, base: str = "main", head: str) -> str | None:
        """Returns the PR URL on success."""
        if not self.is_ready:
            return None
        args = ["gh", "pr", "create", "--title", title, "--body", body, "--base", base, "--head", head]
        if self.repo:
            args += ["--repo", self.repo]
        rc, out, _ = self._shell(args, timeout=60.0)
        return out.strip() if rc == 0 else None

    # ---- triggers -------------------------------------------------------

    async def scan(self) -> AsyncIterator[Signal]:
        """Yield one Signal per open-PR-needing-review and per failed workflow."""
        if not self.is_ready:
            return
        for pr in self.list_open_prs():
            yield Signal(
                id=f"github_pr:{pr.get('number')}",
                source="github_pr",
                kind="pr_open",
                severity="info",
                target=f"{self.repo or ''}#{pr.get('number')}",
                payload=pr,
            )
        for run in self.list_failed_workflow_runs():
            yield Signal(
                id=f"github_pipeline:{run.get('databaseId')}",
                source="github_pipeline",
                kind="workflow_failed",
                severity="error",
                target=f"{self.repo or ''}/runs/{run.get('databaseId')}",
                payload=run,
            )
