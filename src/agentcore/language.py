"""Repository language detection + LSP-server availability probing.

We don't run LSP servers ourselves; agents shell out to whichever LSP-aware
CLI is installed on the host. This module is the surface that tells the
operator (via `agentcore doctor`) which language servers are available for
the languages actually present in the repo, and points at install hints
when one is missing.
"""

from __future__ import annotations

import shutil
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# Extension → canonical language tag.
_EXT_TO_LANG: Final[dict[str, str]] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".m": "objc",
    ".scala": "scala",
    ".lua": "lua",
    ".dart": "dart",
    ".sh": "bash",
    ".ps1": "powershell",
    ".sql": "sql",
}

_EXCLUDE_DIRS: Final[set[str]] = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", "target", "out", ".next", ".idea", ".vscode",
}

# Canonical LSP recommendation per language. Each entry is a list of
# acceptable binaries (first one found wins).
_LSP_BY_LANG: Final[dict[str, list[tuple[str, str]]]] = {
    # (binary, install_hint)
    "python": [
        ("pyright", "pip install pyright   ·   npm i -g pyright"),
        ("pylsp", "pip install python-lsp-server"),
        ("ruff", "pip install ruff   (lints + many fixes)"),
    ],
    "typescript": [
        ("typescript-language-server", "npm i -g typescript-language-server typescript"),
        ("tsc", "npm i -g typescript"),
    ],
    "javascript": [
        ("typescript-language-server", "npm i -g typescript-language-server typescript"),
        ("eslint", "npm i -g eslint"),
    ],
    "go": [("gopls", "go install golang.org/x/tools/gopls@latest")],
    "rust": [("rust-analyzer", "rustup component add rust-analyzer")],
    "java": [("jdtls", "https://download.eclipse.org/jdtls/")],
    "kotlin": [("kotlin-language-server", "https://github.com/fwcd/kotlin-language-server")],
    "ruby": [("solargraph", "gem install solargraph")],
    "php": [("intelephense", "npm i -g intelephense")],
    "csharp": [("omnisharp", "https://github.com/OmniSharp/omnisharp-roslyn")],
    "swift": [("sourcekit-lsp", "ships with the Swift toolchain")],
    "c": [("clangd", "macOS: brew install llvm  ·  Linux: apt install clangd")],
    "cpp": [("clangd", "macOS: brew install llvm  ·  Linux: apt install clangd")],
    "scala": [("metals", "https://scalameta.org/metals/")],
    "lua": [("lua-language-server", "https://github.com/LuaLS/lua-language-server")],
    "dart": [("dart", "https://dart.dev/get-dart")],
    "bash": [("bash-language-server", "npm i -g bash-language-server")],
    "powershell": [("pwsh", "winget install Microsoft.PowerShell")],
    "sql": [("sqls", "go install github.com/sqls-server/sqls@latest")],
}


@dataclass(frozen=True, slots=True)
class LanguageProfile:
    primary: str | None
    counts: dict[str, int]
    files_scanned: int


@dataclass(frozen=True, slots=True)
class LspStatus:
    language: str
    binary: str | None        # first available binary, or None
    available: bool
    install_hint: str         # hint for the recommended option


def _walk(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _EXCLUDE_DIRS for part in p.parts):
            continue
        yield p


def detect_languages(root: Path | str = ".", *, file_cap: int = 2000) -> LanguageProfile:
    """Walk the repo and tally languages by file count. Stops at `file_cap`."""
    counts: Counter[str] = Counter()
    scanned = 0
    for path in _walk(Path(root).resolve()):
        scanned += 1
        if scanned > file_cap:
            break
        lang = _EXT_TO_LANG.get(path.suffix.lower())
        if lang:
            counts[lang] += 1
    primary = counts.most_common(1)[0][0] if counts else None
    return LanguageProfile(primary=primary, counts=dict(counts), files_scanned=scanned)


def probe_lsps(languages: Iterable[str]) -> list[LspStatus]:
    """For each language, report which LSP binaries are usable on the host."""
    out: list[LspStatus] = []
    for lang in languages:
        candidates = _LSP_BY_LANG.get(lang, [])
        chosen: tuple[str, str] | None = None
        for binary, hint in candidates:
            if shutil.which(binary):
                chosen = (binary, hint)
                break
        if chosen is None and candidates:
            # No installed binary — recommend the first option.
            chosen = candidates[0]
            out.append(LspStatus(
                language=lang, binary=None, available=False, install_hint=chosen[1]
            ))
        elif chosen is not None:
            out.append(LspStatus(
                language=lang, binary=chosen[0], available=True, install_hint=chosen[1]
            ))
    return out
