"""Minimal code indexer.

Intentionally lightweight. Agents can offload deeper code traversal to MCP
servers (e.g. gitnexus, code-graph) — this module's only job is to seed the
vector store with enough chunks for "show me roughly relevant code".

Scope of v0: Python via AST; everything else as 80-line text chunks. Each
chunk gets a stable `ref` of the form `code:<relative_path>:<start>-<end>`.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

EXCLUDE_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
EXCLUDE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".tar", ".gz", ".lock"}
TEXT_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt",
             ".rb", ".php", ".cs", ".swift", ".md", ".rst", ".txt", ".toml",
             ".yaml", ".yml", ".json", ".sql", ".sh", ".ps1"}

CHUNK_LINES = 80


@dataclass(slots=True)
class CodeSymbol:
    ref: str
    path: str
    start_line: int
    end_line: int
    kind: str          # "function" | "class" | "method" | "chunk"
    name: str
    text: str


class CodeIndex:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def walk(self) -> Iterable[Path]:
        for p in self.root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in EXCLUDE_DIRS for part in p.parts):
                continue
            if p.suffix.lower() in EXCLUDE_EXTS:
                continue
            if p.suffix.lower() not in TEXT_EXTS:
                continue
            yield p

    def index(self) -> list[CodeSymbol]:
        symbols: list[CodeSymbol] = []
        for path in self.walk():
            rel = path.relative_to(self.root).as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if path.suffix == ".py":
                symbols.extend(self._index_python(rel, text))
            else:
                symbols.extend(self._index_chunks(rel, text))
        return symbols

    # ------------------------------------------------------------------

    def _index_python(self, rel: str, text: str) -> list[CodeSymbol]:
        out: list[CodeSymbol] = []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return self._index_chunks(rel, text)

        lines = text.splitlines()

        def _grab(node: ast.AST, kind: str, name: str) -> CodeSymbol:
            start = getattr(node, "lineno", 1)
            end = getattr(node, "end_lineno", start) or start
            body = "\n".join(lines[start - 1 : end])
            return CodeSymbol(
                ref=f"code:{rel}:{start}-{end}",
                path=rel,
                start_line=start,
                end_line=end,
                kind=kind,
                name=name,
                text=body,
            )

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                out.append(_grab(node, "function", node.name))
            elif isinstance(node, ast.ClassDef):
                out.append(_grab(node, "class", node.name))
                for child in node.body:
                    if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                        out.append(_grab(child, "method", f"{node.name}.{child.name}"))
        if not out:
            return self._index_chunks(rel, text)
        return out

    def _index_chunks(self, rel: str, text: str) -> list[CodeSymbol]:
        lines = text.splitlines()
        out: list[CodeSymbol] = []
        for i in range(0, len(lines), CHUNK_LINES):
            start = i + 1
            end = min(i + CHUNK_LINES, len(lines))
            body = "\n".join(lines[i:end])
            out.append(CodeSymbol(
                ref=f"code:{rel}:{start}-{end}",
                path=rel,
                start_line=start,
                end_line=end,
                kind="chunk",
                name=f"{rel}#{start}",
                text=body,
            ))
        return out
