"""Memory layer.

Submodules carry heavy native deps (psycopg, fastembed, networkx). They are
imported on demand to keep light commands like `agentcore doctor` fast and
free of postgres / vector / model deps.
"""

from __future__ import annotations

__all__ = ["CodeIndex", "CodeSymbol", "Embedder", "KnowledgeGraph", "Reranker", "VectorStore"]


def __getattr__(name: str):
    if name in {"Embedder"}:
        from agentcore.memory.embed import Embedder

        return Embedder
    if name in {"Reranker"}:
        from agentcore.memory.rerank import Reranker

        return Reranker
    if name in {"VectorStore"}:
        from agentcore.memory.vector import VectorStore

        return VectorStore
    if name in {"KnowledgeGraph"}:
        from agentcore.memory.graph import KnowledgeGraph

        return KnowledgeGraph
    if name in {"CodeIndex", "CodeSymbol"}:
        from agentcore.memory.code_index import CodeIndex, CodeSymbol

        return {"CodeIndex": CodeIndex, "CodeSymbol": CodeSymbol}[name]
    raise AttributeError(f"module 'agentcore.memory' has no attribute {name!r}")
