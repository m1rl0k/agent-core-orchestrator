"""PRF: snippet capture, label accumulation, change-kind classification."""

from __future__ import annotations

from agentcore.memory import prf
from agentcore.memory.graph import KnowledgeGraph


def test_record_snippet_and_tag_relevance(tmp_path) -> None:
    g = KnowledgeGraph(snapshot_path=tmp_path / "graph.json")
    g.record_snippet(
        "T1", "src/foo.py",
        start=10, end=14, content="def foo(): pass",
        intent="add foo", role="developer",
    )
    n = g.tag_relevance("T1", prf.QA_PASSED, score=1.0, reason="all green")
    assert n == 1

    snippet = g.snippets_for("T1")[0]
    assert snippet["intent"] == "add foo"
    assert snippet["role"] == "developer"
    assert prf.QA_PASSED in snippet["labels"]
    assert snippet["labels"][prf.QA_PASSED]["reason"] == "all green"


def test_tag_relevance_accumulates() -> None:
    g = KnowledgeGraph(snapshot_path="/tmp/agentcore-prf-acc.json")
    g.record_snippet(
        "T2", "src/bar.py",
        start=1, end=2, content="x = 1",
        intent="bugfix", role="architect",
    )
    g.tag_relevance("T2", prf.QA_FAILED, reason="3 cases failed")
    g.tag_relevance("T2", prf.DEV_REVISED)
    g.tag_relevance("T2", prf.QA_PASSED, reason="passed on retry")

    snippet = g.snippets_for("T2")[0]
    assert set(snippet["labels"].keys()) == {prf.QA_FAILED, prf.DEV_REVISED, prf.QA_PASSED}


def test_classify_change_kinds_picks_keywords() -> None:
    assert prf.KIND_BUGFIX in prf.classify_change_kinds("Fix off-by-one bug in parser")
    assert prf.KIND_REFACTOR in prf.classify_change_kinds(
        "Refactor the auth module: extract helpers"
    )
    assert prf.KIND_INFRA in prf.classify_change_kinds("Update docker-compose.yml")
    assert prf.classify_change_kinds("") == []


def test_tag_task_distinct_from_snippets() -> None:
    g = KnowledgeGraph(snapshot_path="/tmp/agentcore-prf-task.json")
    g.record_handoff("T3", "user", "ops")
    g.tag_task("T3", prf.SHIPPED, reason="merged + deployed")
    attrs = g.g.nodes["task:T3"]
    assert prf.SHIPPED in attrs.get("labels", {})
