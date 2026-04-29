"""Real token counting with graceful fallback.

Order of preference:
  1. **tiktoken** (`o200k_base`) — bundled with the package, fully offline,
     accurate for GPT-4o + close enough for Claude 4.x / Kimi K2 / GLM-4.6
     for budgeting purposes.
  2. **HuggingFace `tokenizers`** loaded from a local file under
     `vendor/tokenizers/<name>.json`. Drop in per-provider tokenizer JSONs
     to get exact counts; this never touches the HF Hub at runtime so an
     HF outage cannot break agent execution.
  3. **Char-based estimate** (`chars / 3.0`). Last-resort floor so the
     budget split decision still works when both libraries are missing
     or fail to load. Returns at least 1 for any non-empty string.

The `count_tokens()` API is intentionally side-effect-free: every call is
cheap (encoder cached on first hit), no network, no HF login required.

Caching: encoders are loaded once per process. Set
`AGENTCORE_TOKEN_BACKEND={tiktoken|hf|estimate}` to pin a backend
explicitly (useful for tests).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

_TOKENIZER_DIR = Path(__file__).resolve().parent.parent.parent.parent / "vendor" / "tokenizers"

_BACKEND_PIN = os.environ.get("AGENTCORE_TOKEN_BACKEND", "").lower().strip()


@lru_cache(maxsize=1)
def _tiktoken_enc():  # type: ignore[no-untyped-def]
    """Lazy-load tiktoken's o200k_base encoding. Returns None if the
    library or the bundled encoding is unavailable."""
    if _BACKEND_PIN and _BACKEND_PIN != "tiktoken":
        return None
    try:
        import tiktoken  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        # Some installs ship without bundled cache; cl100k is the older,
        # widely-cached fallback.
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None


@lru_cache(maxsize=8)
def _hf_tokenizer(name: str):  # type: ignore[no-untyped-def]
    """Load a HF tokenizer JSON from `vendor/tokenizers/<name>.json`.

    Returns None if the library is missing or the file isn't present.
    Never reaches the network — drop the JSON into the repo to use it.
    """
    if _BACKEND_PIN and _BACKEND_PIN not in ("hf", ""):
        return None
    try:
        from tokenizers import Tokenizer  # type: ignore[import-not-found]
    except ImportError:
        return None
    path = _TOKENIZER_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        return Tokenizer.from_file(str(path))
    except Exception:
        return None


def count_tokens(text: str, *, model_hint: str | None = None) -> int:
    """Best-effort token count for `text`.

    `model_hint` (e.g. "kimi-k2", "claude-4", "glm-4.6") tries a
    matching local tokenizer JSON first; falls back to tiktoken's
    o200k_base, then a 3-chars-per-token estimate.
    """
    if not text:
        return 0

    if _BACKEND_PIN == "estimate":
        return max(1, len(text) // 3)

    if model_hint:
        tok = _hf_tokenizer(_normalise_hint(model_hint))
        if tok is not None:
            try:
                return len(tok.encode(text).ids)
            except Exception:
                pass

    enc = _tiktoken_enc()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            pass

    return max(1, len(text) // 3)


def _normalise_hint(hint: str) -> str:
    """Map provider model strings to local tokenizer file names.

    `kimi-k2-thinking`, `moonshot.kimi-k2-thinking` → `kimi-k2`.
    `claude-4-sonnet-20260101`                       → `claude-4`.
    """
    h = hint.lower()
    if "kimi" in h:
        return "kimi-k2"
    if "claude" in h:
        return "claude-4"
    if "glm" in h:
        return "glm-4"
    if "gpt-4o" in h or "o200k" in h:
        return "gpt-4o"
    return h.split("/")[-1].split(".")[-1]


def active_backend() -> str:
    """Diagnostic: which tokenizer is currently active for `count_tokens()`."""
    if _BACKEND_PIN == "estimate":
        return "estimate"
    if _tiktoken_enc() is not None:
        return "tiktoken"
    if _BACKEND_PIN == "hf":
        return "hf" if _TOKENIZER_DIR.exists() else "estimate"
    return "estimate"
