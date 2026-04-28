"""Host detection sanity (cross-platform)."""

from __future__ import annotations

from agentcore.host import detect_host, render_install_hint


def test_detect_host_returns_known_os() -> None:
    h = detect_host()
    assert h.os in {"windows", "macos", "linux", "unknown"}
    assert h.python_version
    assert isinstance(h.is_posix, bool)


def test_render_install_hint_picks_platform() -> None:
    hint = (
        "macOS: brew install gh  ·  "
        "Linux: apt install gh  ·  "
        "Windows: winget install GitHub.cli"
    )
    h = detect_host()
    rendered = render_install_hint(hint, h)
    if h.os == "macos":
        assert rendered.startswith("brew")
    elif h.os == "linux":
        assert rendered.startswith("apt")
    elif h.os == "windows":
        assert rendered.startswith("winget")
