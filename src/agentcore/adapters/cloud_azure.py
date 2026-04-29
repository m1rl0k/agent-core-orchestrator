"""Azure adapter — wraps the host's `az` CLI.

Initial v0 surface:
  - Azure Monitor alerts in 'fired' state -> Signal(azure / alert_fired)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from agentcore.adapters.base import Adapter
from agentcore.contracts.domain import Signal


class AzureAdapter(Adapter):
    name = "azure"
    cli = "az"

    def short_status(self) -> str:
        if not self.capability.enabled:
            return "azure: disabled"
        if not self.capability.installed:
            return f"azure: missing CLI — {self.capability.install_hint}"
        if not self.capability.authenticated:
            return f"azure: not authenticated — {self.capability.auth_hint}"
        return "azure: ready"

    def list_fired_alerts(self) -> list[dict]:
        if not self.is_ready:
            return []
        rc, out, _ = self._shell(
            ["az", "monitor", "alert", "list", "--output", "json"],
            timeout=20.0,
        )
        if rc != 0:
            return []
        try:
            data = json.loads(out or "[]")
        except json.JSONDecodeError:
            return []
        return [a for a in data if str(a.get("alertState", "")).lower() == "fired"]

    async def scan(self) -> AsyncIterator[Signal]:
        if not self.is_ready:
            return
        for alert in self.list_fired_alerts():
            name = alert.get("name", "unknown")
            yield Signal(
                id=f"azure:{name}",
                source="azure",
                kind="alert_fired",
                severity="error",
                target=name,
                payload=alert,
            )
