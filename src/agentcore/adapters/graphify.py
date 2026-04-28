"""Graphify adapter — first-class, in-process code knowledge graph.

Why in-process: graphify is a native Python package (NetworkX + Leiden +
tree-sitter) and we already use NetworkX/Louvain for our operational graph.
We can compose subgraphs without serialization, IPC, or a Node runtime.

The adapter is defensively imported: if `graphifyy` isn't installed we still
load (capability reports unavailable). The runtime's enrichment hook
becomes a no-op in that case.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import networkx as nx

from agentcore.adapters.base import Adapter
from agentcore.capabilities import Capability


def _try_import() -> Any | None:
    try:
        import graphifyy  # type: ignore[import-not-found]

        return graphifyy
    except Exception:
        return None


@dataclass(slots=True)
class SymbolImpact:
    """Subset of graphify's `impact` return shaped for our enrichment loop."""

    symbol: str
    file: str
    downstream: list[str]      # symbols that would be affected
    confidence: float = 1.0


class GraphifyAdapter(Adapter):
    name = "graphify"
    cli = "—"  # in-process

    def __init__(self, repo_root: Path | str = ".", *, enabled: bool = True) -> None:
        self._mod = _try_import() if enabled else None
        installed = self._mod is not None
        cap = Capability(
            name="graphify",
            enabled=enabled,
            installed=installed,
            authenticated=installed,
            cli="graphifyy (Python)",
            install_hint="pip install graphifyy   (or: uv add graphifyy)",
            auth_hint="—",
        )
        super().__init__(cap)
        self.repo_root = Path(repo_root).resolve()
        self._engine: Any | None = None

    def short_status(self) -> str:
        if not self.capability.enabled:
            return "graphify: disabled"
        if not self.capability.installed:
            return f"graphify: missing — {self.capability.install_hint}"
        return f"graphify: ready · {self.repo_root}"

    # ---- lifecycle ------------------------------------------------------

    def _ensure_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        if self._mod is None:
            raise RuntimeError("graphifyy is not installed")
        # graphify exposes a top-level builder; we tolerate naming variations
        # across versions to keep this adapter version-light.
        for builder_name in ("Graph", "Graphify", "build", "from_directory"):
            ctor = getattr(self._mod, builder_name, None)
            if ctor is None:
                continue
            try:
                self._engine = ctor(str(self.repo_root))
                return self._engine
            except Exception:
                continue
        raise RuntimeError("could not initialize graphify engine; check graphifyy version")

    def analyze(self) -> bool:
        """Index the repo. Idempotent across repeat calls."""
        try:
            engine = self._ensure_engine()
        except Exception:
            return False
        for method in ("analyze", "index", "build"):
            fn = getattr(engine, method, None)
            if callable(fn):
                try:
                    fn()
                    return True
                except Exception:
                    continue
        return True  # already indexed / nothing to do

    # ---- read API the runtime/agents call ------------------------------

    def context(self, symbol_or_file: str) -> dict[str, Any]:
        try:
            engine = self._ensure_engine()
        except Exception:
            return {}
        fn = getattr(engine, "context", None)
        if not callable(fn):
            return {}
        try:
            result = fn(symbol_or_file)
            return result if isinstance(result, dict) else {"raw": result}
        except Exception:
            return {}

    def impact(self, symbol_or_file: str) -> SymbolImpact | None:
        try:
            engine = self._ensure_engine()
        except Exception:
            return None
        fn = getattr(engine, "impact", None)
        if not callable(fn):
            return None
        try:
            raw = fn(symbol_or_file)
        except Exception:
            return None
        if raw is None:
            return None
        # Normalize a few likely shapes (dict / namedtuple / object).
        if isinstance(raw, dict):
            return SymbolImpact(
                symbol=raw.get("symbol", symbol_or_file),
                file=raw.get("file", ""),
                downstream=list(raw.get("downstream", []) or raw.get("affected", [])),
                confidence=float(raw.get("confidence", 1.0)),
            )
        return SymbolImpact(
            symbol=getattr(raw, "symbol", symbol_or_file),
            file=getattr(raw, "file", ""),
            downstream=list(getattr(raw, "downstream", []) or getattr(raw, "affected", [])),
            confidence=float(getattr(raw, "confidence", 1.0)),
        )

    def query(self, q: str) -> Any:
        try:
            engine = self._ensure_engine()
        except Exception:
            return None
        fn = getattr(engine, "query", None) or getattr(engine, "cypher", None)
        if not callable(fn):
            return None
        try:
            return fn(q)
        except Exception:
            return None

    # ---- raw graph access for the enrichment hook ----------------------

    def to_networkx(self) -> nx.Graph | None:
        try:
            engine = self._ensure_engine()
        except Exception:
            return None
        for attr in ("graph", "nx", "to_networkx"):
            obj = getattr(engine, attr, None)
            if isinstance(obj, nx.Graph):
                return obj
            if callable(obj):
                try:
                    g = obj()
                    if isinstance(g, nx.Graph):
                        return g
                except Exception:
                    continue
        return None

    def subgraph_for(self, refs: Iterable[str]) -> nx.Graph | None:
        """Return the subgraph induced by the given symbols/files."""
        full = self.to_networkx()
        if full is None:
            return None
        present = [r for r in refs if r in full]
        if not present:
            return None
        seeds = set(present)
        for r in present:
            seeds.update(full.neighbors(r))
        return full.subgraph(seeds).copy()
