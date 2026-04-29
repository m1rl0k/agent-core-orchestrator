"""Shared constructor for the optional `HybridRetriever`.

Both the CLI (`agentcore plan`) and the FastAPI orchestrator need to wire up
the retriever stack: VectorStore + Embedder + (optional) Reranker. The
factory is intentionally tolerant — missing pgvector or fastembed shouldn't
take the orchestrator down; agents simply get prompts without the
semantic-retrieval section.

Returns `None` (with a structured-log warning) on any failure path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from agentcore.memory.graph import KnowledgeGraph
    from agentcore.retrieval.hybrid import HybridRetriever
    from agentcore.settings import Settings

log = structlog.get_logger(__name__)


def try_build_retriever(
    settings: Settings, graph: KnowledgeGraph
) -> HybridRetriever | None:
    """Construct a HybridRetriever only if its deps are usable on this host."""
    try:
        from agentcore.memory.embed import Embedder
        from agentcore.memory.vector import VectorStore
        from agentcore.retrieval.hybrid import HybridRetriever

        store = VectorStore(settings)
        try:
            store.init_schema()
        except Exception as exc:
            log.warning("retriever.offline", reason="pgvector_unavailable", error=str(exc))
            return None

        try:
            embedder = Embedder(settings)
        except Exception as exc:
            log.warning("retriever.offline", reason="embedder_unavailable", error=str(exc))
            return None

        reranker = None
        if settings.enable_rerank:
            try:
                from agentcore.memory.rerank import Reranker

                reranker = Reranker(settings)
            except Exception as exc:
                log.info("reranker.unavailable", error=str(exc))
                reranker = None  # nice-to-have

        return HybridRetriever(embedder, store, graph=graph, reranker=reranker)
    except Exception as exc:
        log.warning("retriever.offline", reason="import_failure", error=str(exc))
        return None
