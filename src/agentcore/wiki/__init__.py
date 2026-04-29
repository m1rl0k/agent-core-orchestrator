"""Living wiki: a curated, retrieval-first markdown projection of the codebase.

The wiki is the LLM's running summary of the repo. It's:
  - persisted as plain markdown under `<wiki_root>/<project>/<branch>/...`
  - indexed in pgvector under collection `wiki:<project>:<branch>` so every
    agent loop can retrieve from it via the existing HybridRetriever
  - kept current by a curator agent (`agents/wikist.agent.md`) that runs in
    bulk-seed, incremental, or lint mode
  - tool-agnostic: a single `agentcore wiki link` command mirrors the same
    content into Claude Code skills, Copilot prompts, Cursor rules, AGENTS.md
"""

from agentcore.wiki.storage import WikiPage, WikiStorage

__all__ = ["WikiPage", "WikiStorage"]
