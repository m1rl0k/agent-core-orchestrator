"""AWS adapter — wraps the host's `aws` CLI.

Initial v0 surfaces:
  - CloudWatch alarms in ALARM state -> Signal(cloudwatch / alarm_triggered)

Adding a new surface (ECS health, Lambda errors, RDS slow query) means adding
another `_list_*` and yielding more Signals from `scan()`. All credentials
come from the host's existing `aws` profile / SSO chain.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from agentcore.adapters.base import Adapter
from agentcore.contracts.domain import Signal


class AwsAdapter(Adapter):
    name = "aws"
    cli = "aws"

    def short_status(self) -> str:
        if not self.capability.enabled:
            return "aws: disabled"
        if not self.capability.installed:
            return f"aws: missing CLI — {self.capability.install_hint}"
        if not self.capability.authenticated:
            return f"aws: not authenticated — {self.capability.auth_hint}"
        return f"aws: ready · {self.capability.detail.splitlines()[0] if self.capability.detail else ''}"

    def list_alarms_in_alarm(self) -> list[dict]:
        if not self.is_ready:
            return []
        rc, out, _ = self._shell(
            [
                "aws", "cloudwatch", "describe-alarms",
                "--state-value", "ALARM",
                "--output", "json",
            ],
            timeout=20.0,
        )
        if rc != 0:
            return []
        data = json.loads(out or "{}")
        return data.get("MetricAlarms", []) + data.get("CompositeAlarms", [])

    async def scan(self) -> AsyncIterator[Signal]:
        if not self.is_ready:
            return
        for alarm in self.list_alarms_in_alarm():
            name = alarm.get("AlarmName", "unknown")
            yield Signal(
                id=f"cloudwatch:{name}",
                source="cloudwatch",
                kind="alarm_triggered",
                severity="error",
                target=name,
                payload=alarm,
            )
