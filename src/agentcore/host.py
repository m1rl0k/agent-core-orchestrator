"""Host/OS/shell detection.

Used by:
  - `agentcore doctor` to render the install hint relevant to this host
  - the CLI to pick line-rendering defaults
  - adapters when they need to decide between e.g. `where` vs `which`
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass
from typing import Literal

OS = Literal["windows", "macos", "linux", "unknown"]
Shell = Literal["powershell", "pwsh", "cmd", "bash", "zsh", "fish", "sh", "unknown"]


@dataclass(frozen=True, slots=True)
class HostInfo:
    os: OS
    arch: str
    shell: Shell
    python_version: str
    is_windows: bool
    is_macos: bool
    is_linux: bool
    is_posix: bool

    @property
    def package_manager_hint(self) -> str:
        if self.is_macos:
            return "brew install <pkg>"
        if self.is_windows:
            return "winget install <pkg>   (or: choco install <pkg>)"
        # Linux
        if shutil.which("apt"):
            return "sudo apt install <pkg>"
        if shutil.which("dnf"):
            return "sudo dnf install <pkg>"
        if shutil.which("pacman"):
            return "sudo pacman -S <pkg>"
        if shutil.which("zypper"):
            return "sudo zypper install <pkg>"
        return "use your distro's package manager"


def _detect_os() -> OS:
    sysname = platform.system().lower()
    if sysname.startswith("win"):
        return "windows"
    if sysname == "darwin":
        return "macos"
    if sysname == "linux":
        return "linux"
    return "unknown"


def _detect_shell() -> Shell:
    # PowerShell sets PSModulePath and (often) PSExecutionPolicyPreference.
    if os.environ.get("PSModulePath") and (os.environ.get("PSEdition") or shutil.which("pwsh")):
        return "pwsh" if os.environ.get("PSEdition", "").lower() == "core" else "powershell"
    if os.environ.get("PROMPT") and platform.system().lower().startswith("win"):
        return "cmd"

    shell = os.environ.get("SHELL", "")
    base = os.path.basename(shell)
    if base in {"bash", "zsh", "fish", "sh"}:
        return base  # type: ignore[return-value]
    if shutil.which("powershell") and platform.system().lower().startswith("win"):
        return "powershell"
    return "unknown"


def detect_host() -> HostInfo:
    os_name = _detect_os()
    return HostInfo(
        os=os_name,
        arch=platform.machine(),
        shell=_detect_shell(),
        python_version=sys.version.split()[0],
        is_windows=os_name == "windows",
        is_macos=os_name == "macos",
        is_linux=os_name == "linux",
        is_posix=os.name == "posix",
    )


def render_install_hint(full_hint: str, host: HostInfo | None = None) -> str:
    """Pull the OS-specific slice out of a multi-platform `install_hint` string.

    Capability hints are formatted as `macOS: ...  ·  Linux: ...  ·  Windows: ...`.
    This returns just the slice for the current host (or the full string if it
    can't decide).
    """
    h = host or detect_host()
    parts = [p.strip() for p in full_hint.split("·") if p.strip()]
    needle = {"windows": "Windows:", "macos": "macOS:", "linux": "Linux:"}.get(h.os, "")
    if not needle:
        return full_hint
    for p in parts:
        if p.startswith(needle):
            return p[len(needle):].strip()
    return full_hint
