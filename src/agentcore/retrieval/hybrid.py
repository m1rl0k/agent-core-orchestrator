"""Hybrid retrieval: vector similarity ∪ graph proximity, then rerank.

Algorithm:
  1. Embed the query.
  2. Top-k from VectorStore in each requested collection.
  3. For each vector hit, expand neighbors in KnowledgeGraph (1 hop).
  4. Score = α * vector_score + β * graph_co_membership.
  5. Return ContextRefs sorted by score.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentcore.contracts.domain import ContextBundle, ContextRef
from agentcore.memory.embed import Embedder
from agentcore.memory.graph import KnowledgeGraph
from agentcore.memory.vector import Hit, VectorStore


@dataclass(slots=True)
class RetrievalResult:
    bundle: ContextBundle
    raw_hits: list[Hit]


class HybridRetriever:
    def __init__(
        self,
        embedder: Embedder,
        vector: VectorStore,
        graph: KnowledgeGraph | None = None,
        *,
        alpha: float = 0.7,
        beta: float = 0.3,
    ) -> None:
        self.embedder = embedder
        self.vector = vector
        self.graph = graph
        self.alpha = alpha
        self.beta = beta

    async def retrieve(
        self, query: str, collections: list[str], k: int = 8
    ) -> RetrievalResult:
        [emb] = await self.embedder.embed([query])
        all_hits: list[Hit] = []
        for col in collections:
            all_hits.extend(self.vector.search(col, emb, k=k))

        scored: list[tuple[Hit, float]] = []
        for hit in all_hits:
            graph_bonus = 0.0
            if self.graph and self.graph.g.has_node(hit.ref):
                neighbors = set(self.graph.neighbors(hit.ref, hops=1))
                if neighbors & {h.ref for h in all_hits}:
                    graph_bonus = 1.0
            score = self.alpha * hit.score + self.beta * graph_bonus
            scored.append((hit, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:k]
        refs = [
            ContextRef(
                kind="rag" if hit.ref.startswith(("rag", "doc")) else "code",
                id=hit.ref,
                score=score,
                excerpt=hit.content[:400],
            )
            for hit, score in top
        ]
        summary = (
            f"{len(top)} relevant chunks across {len(collections)} collection(s)."
            if top else "No relevant context found."
        )
        return RetrievalResult(
            bundle=ContextBundle(refs=refs, summary=summary),
            raw_hits=[h for h, _ in top],
        )
