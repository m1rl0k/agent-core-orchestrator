"""Unified-diff hunk parser used by the runtime to capture snippet ranges."""

from __future__ import annotations

import textwrap

from agentcore.orchestrator.runtime import Runtime

DIFF = textwrap.dedent("""\
    --- a/src/app.py
    +++ b/src/app.py
    @@ -10,3 +10,5 @@ def foo():
         pass
    +    return 1
    +    # done
    @@ -50,2 +52,3 @@ def bar():
         x = 1
    +    y = 2
    """)


def test_parse_diff_hunks_extracts_new_file_ranges() -> None:
    hunks = Runtime._parse_diff_hunks(DIFF)
    assert len(hunks) == 2
    starts = [h[0] for h in hunks]
    assert starts == [10, 52]
    # the captured text should include both context and added lines
    assert "return 1" in hunks[0][2]
    assert "y = 2" in hunks[1][2]


def test_parse_diff_hunks_empty() -> None:
    assert Runtime._parse_diff_hunks("") == []
