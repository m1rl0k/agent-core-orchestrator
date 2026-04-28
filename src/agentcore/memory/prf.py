"""Pseudo-relevance feedback (PRF) labels.

Labels are attached to snippets in the operational graph. Each snippet
accumulates a set of (label, score, reason, at) records over time so that
later events can refine the picture (a task may be `qa_passed` and then
`signal_recurring` two weeks later).

We intentionally keep this open: callers can pass labels not in the
registry, but `WELL_KNOWN` is the curated taxonomy that the runtime,
adapters, and dashboards understand by default.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

# ---- Canonical labels --------------------------------------------------

# Outcome / lifecycle
POSITIVE: Final = "positive"                    # change shipped + healthy
QA_PASSED: Final = "qa_passed"                  # QA suite green
QA_FAILED: Final = "qa_failed"                  # QA returned failures
ARCHITECT_REVISED: Final = "architect_revised"  # plan was rewritten
DEV_REVISED: Final = "dev_revised"              # patch was rewritten
OPS_BLOCKED: Final = "ops_blocked"              # ops could not ship
SHIPPED: Final = "shipped"                      # merged / deployed
ABANDONED: Final = "abandoned"                  # chain cancelled / timed out
SUPERSEDED: Final = "superseded"                # same area changed later

# Signal feedback
SIGNAL_RESOLVED: Final = "signal_resolved"      # originating signal cleared
SIGNAL_RECURRING: Final = "signal_recurring"    # same signal fired again

# Risk / impact
LOW_BLAST_RADIUS: Final = "low_blast_radius"
HIGH_BLAST_RADIUS: Final = "high_blast_radius"

# Change kind (from summary/notes keyword match — cheap heuristic)
KIND_BUGFIX: Final = "kind:bugfix"
KIND_FEATURE: Final = "kind:feature"
KIND_REFACTOR: Final = "kind:refactor"
KIND_HOTFIX: Final = "kind:hotfix"
KIND_MIGRATION: Final = "kind:migration"
KIND_CLEANUP: Final = "kind:cleanup"
KIND_DOCS: Final = "kind:docs"
KIND_INFRA: Final = "kind:infra"
KIND_TEST: Final = "kind:test"

WELL_KNOWN: Final[set[str]] = {
    POSITIVE, QA_PASSED, QA_FAILED, ARCHITECT_REVISED, DEV_REVISED,
    OPS_BLOCKED, SHIPPED, ABANDONED, SUPERSEDED,
    SIGNAL_RESOLVED, SIGNAL_RECURRING,
    LOW_BLAST_RADIUS, HIGH_BLAST_RADIUS,
    KIND_BUGFIX, KIND_FEATURE, KIND_REFACTOR, KIND_HOTFIX,
    KIND_MIGRATION, KIND_CLEANUP, KIND_DOCS, KIND_INFRA, KIND_TEST,
}

# Crude keyword classifier for change-kind labels. Order matters: more
# specific first.
_KIND_KEYWORDS: Final[list[tuple[str, list[str]]]] = [
    (KIND_HOTFIX, ["hotfix", "p0", "incident", "outage", "urgent"]),
    (KIND_MIGRATION, ["migration", "migrate", "schema", "alembic", "ddl"]),
    (KIND_BUGFIX, ["bug", "fix ", "fixes ", "regression", "patch "]),
    (KIND_REFACTOR, ["refactor", "extract", "rename", "tidy", "consolidate"]),
    (KIND_CLEANUP, ["cleanup", "remove dead", "dead code", "deprecat"]),
    (KIND_DOCS, ["docs", "documentation", "readme", "comment"]),
    (KIND_INFRA, ["docker", "compose", "ci/cd", "pipeline", "infra", "terraform", "k8s"]),
    (KIND_TEST, ["test", "spec ", "fixture", "coverage"]),
    (KIND_FEATURE, ["feature", "add ", "implement", "introduce", "new "]),
]


def classify_change_kinds(text: str) -> list[str]:
    """Apply the keyword classifier to a free-form summary/notes string."""
    if not text:
        return []
    lowered = text.lower()
    return [label for label, kws in _KIND_KEYWORDS if any(k in lowered for k in kws)]


def now() -> str:
    return datetime.now(UTC).isoformat()
