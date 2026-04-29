"""CLI review convergence helpers."""

from __future__ import annotations

from agentcore.cli import _environment_only_review_blockers


def test_environment_only_review_blockers_converges_after_clean_qa() -> None:
    verdicts = [
        {
            "agent": "qa",
            "approved": False,
            "comments": "Test environment failure: pytest is not installed.",
            "blockers": ["No module named pytest; cannot execute test suite."],
        },
        {
            "agent": "ops",
            "approved": False,
            "comments": "Cannot verify because the test runner failed in the environment.",
            "blockers": ["zero tests executed due to missing pytest"],
        },
    ]
    state = {"qa_output": {"failed": [], "passed": ["test_add"]}}

    assert _environment_only_review_blockers(verdicts, state) is True


def test_environment_only_review_blockers_does_not_mask_real_qa_failures() -> None:
    verdicts = [
        {
            "agent": "qa",
            "approved": False,
            "comments": "pytest not installed",
            "blockers": ["cannot verify"],
        }
    ]
    state = {
        "qa_output": {
            "failed": [{"name": "test_multiply", "message": "expected 6 got 5"}],
            "passed": [],
        }
    }

    assert _environment_only_review_blockers(verdicts, state) is False


def test_environment_only_review_blockers_rejects_non_environment_feedback() -> None:
    verdicts = [
        {
            "agent": "developer",
            "approved": False,
            "comments": "multiply still returns the wrong result",
            "blockers": ["implementation bug not fixed"],
        }
    ]
    state = {"qa_output": {"failed": [], "passed": ["test_add"]}}

    assert _environment_only_review_blockers(verdicts, state) is False
