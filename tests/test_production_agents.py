"""Smoke test: the four shipped agent specs parse cleanly.

`test_loader.py` only exercises a synthetic fixture, so YAML mistakes in the
real `agents/*.agent.md` files (e.g. unquoted `list[Type]` inside a flow
mapping) used to slip through every other check. This module loads the
actual files in the repo and asserts there are zero parse errors.
"""

from __future__ import annotations

from pathlib import Path

from agentcore.spec.loader import AgentRegistry

EXPECTED_NAMES = {"architect", "developer", "qa", "ops"}


def test_all_production_agent_specs_load() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    agents_dir = repo_root / "agents"

    reg = AgentRegistry()
    reg.load_dir(agents_dir)

    assert not reg.errors(), f"agent.md parse errors: {reg.errors()}"

    loaded = {s.name for s in reg.all()}
    missing = EXPECTED_NAMES - loaded
    assert not missing, f"missing agents: {missing}; loaded: {loaded}"


def test_all_specs_have_resolvable_llm_config() -> None:
    """Every spec declares a provider+model the router knows how to dispatch."""
    repo_root = Path(__file__).resolve().parent.parent
    reg = AgentRegistry()
    reg.load_dir(repo_root / "agents")

    valid_providers = {"anthropic", "bedrock", "azure_openai", "zai"}
    for spec in reg.all():
        assert spec.llm.provider in valid_providers, (
            f"{spec.name}: unknown provider {spec.llm.provider!r}"
        )
        assert spec.llm.model, f"{spec.name}: empty model"
