"""Host-capability detection for opt-in integrations.

The orchestrator never assumes credentials. Each optional adapter (GitHub,
AWS, Azure) declares (a) whether the operator has opted in via settings and
(b) whether the host has the underlying CLI installed and authenticated.

`agentcore doctor` reads from this module to render a status table.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal

from agentcore.settings import Settings, get_settings


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    enabled: bool                 # opted in via settings
    installed: bool               # CLI present on PATH
    authenticated: bool           # CLI reports a usable session
    cli: str                      # the binary the adapter shells out to
    install_hint: str             # one-line install hint
    auth_hint: str                # how to authenticate
    detail: str = ""              # last error, account id, etc.

    @property
    def status(self) -> Literal["off", "missing", "unauthed", "ready"]:
        if not self.enabled:
            return "off"
        if not self.installed:
            return "missing"
        if not self.authenticated:
            return "unauthed"
        return "ready"


def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run(cmd: list[str], timeout: float = 5.0) -> tuple[bool, str]:
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return out.returncode == 0, (out.stdout or out.stderr).strip()


_GH_HINT = (
    "macOS: brew install gh  ·  "
    "Linux: see https://github.com/cli/cli/blob/trunk/docs/install_linux.md  ·  "
    "Windows: winget install GitHub.cli"
)
_AWS_HINT = (
    "macOS: brew install awscli  ·  "
    "Linux: see https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html  ·  "
    "Windows: winget install Amazon.AWSCLI"
)
_AZ_HINT = (
    "macOS: brew install azure-cli  ·  "
    "Linux: see https://learn.microsoft.com/cli/azure/install-azure-cli-linux  ·  "
    "Windows: winget install Microsoft.AzureCLI"
)


def _probe_github(enabled: bool) -> Capability:
    installed = _which("gh")
    auth_ok, detail = (False, "")
    if installed:
        auth_ok, detail = _run(["gh", "auth", "status"])
    return Capability(
        name="github",
        enabled=enabled,
        installed=installed,
        authenticated=auth_ok,
        cli="gh",
        install_hint=_GH_HINT,
        auth_hint="gh auth login",
        detail=detail,
    )


def _probe_aws(enabled: bool) -> Capability:
    installed = _which("aws")
    auth_ok, detail = (False, "")
    if installed:
        auth_ok, detail = _run(["aws", "sts", "get-caller-identity"])
    return Capability(
        name="aws",
        enabled=enabled,
        installed=installed,
        authenticated=auth_ok,
        cli="aws",
        install_hint=_AWS_HINT,
        auth_hint="aws configure   (or AWS_PROFILE / SSO)",
        detail=detail,
    )


def _probe_azure(enabled: bool) -> Capability:
    installed = _which("az")
    auth_ok, detail = (False, "")
    if installed:
        auth_ok, detail = _run(["az", "account", "show"])
    return Capability(
        name="azure",
        enabled=enabled,
        installed=installed,
        authenticated=auth_ok,
        cli="az",
        install_hint=_AZ_HINT,
        auth_hint="az login",
        detail=detail,
    )


def detect_capabilities(settings: Settings | None = None) -> dict[str, Capability]:
    s = settings or get_settings()
    return {
        "github": _probe_github(s.enable_github),
        "aws": _probe_aws(s.enable_aws),
        "azure": _probe_azure(s.enable_azure),
    }
