"""Round-trip + validation tests for the agent.md parser."""

from __future__ import annotations

import textwrap

import pytest

from agentcore.spec.parser import SpecParseError, parse_agent_text


VALID = textwrap.dedent(
    """\
    ---
    name: architect
    description: Plans things.
    tools: [Read]
    model: claude-opus-4-7
    llm:
      provider: anthropic
      model: claude-opus-4-7
    soul:
      role: architect
      voice: terse
      values: [correctness]
    contract:
      inputs:
        - { name: brief, type: string, required: true }
      outputs:
        - { name: summary, type: string, required: true }
      accepts_handoff_from: [user]
      delegates_to: [developer]
    ---

    You are the Architect.
    """
)


def test_parses_valid_spec() -> None:
    spec = parse_agent_text(VALID, source="architect.agent.md")
    assert spec.name == "architect"
    assert spec.llm.provider == "anthropic"
    assert spec.contract.inputs[0].name == "brief"
    assert spec.contract.delegates_to == ["developer"]
    assert "Architect" in spec.system_prompt
    assert spec.checksum is not None


def test_unknown_frontmatter_is_ignored() -> None:
    text = VALID.replace(
        "model: claude-opus-4-7\n",
        "model: claude-opus-4-7\nrandom_key_from_other_tool: hello\n",
        1,
    )
    spec = parse_agent_text(text)
    assert spec.name == "architect"


def test_invalid_temperature_raises() -> None:
    bad = VALID.replace("provider: anthropic", "provider: anthropic\n  temperature: 9.9")
    with pytest.raises(SpecParseError):
        parse_agent_text(bad)


def test_name_must_be_slug() -> None:
    bad = VALID.replace("name: architect", "name: 'has spaces'")
    with pytest.raises(SpecParseError):
        parse_agent_text(bad)
