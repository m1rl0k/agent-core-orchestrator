"""Focused regressions for orchestrator HTTP helpers."""

from __future__ import annotations

import importlib
from typing import Any

from agentcore.contracts.envelopes import Handoff, validate_payload
from agentcore.spec.models import IOField


def _load_app_module(monkeypatch: Any) -> Any:
    monkeypatch.setenv("AGENTCORE_HOST", "127.0.0.1")
    monkeypatch.delenv("AGENTCORE_API_TOKEN", raising=False)
    monkeypatch.setenv("AGENTCORE_ENABLE_GRAPHIFY", "false")

    from agentcore.settings import get_settings

    get_settings.cache_clear()
    return importlib.import_module("agentcore.orchestrator.app")


class _Settings:
    project_name = "default"


class _Idem:
    def __init__(self) -> None:
        self.values: dict[tuple[str | None, str, str], dict[str, Any]] = {}

    def get(
        self, scope: str, key: str, *, project_id: str | None = None
    ) -> dict[str, Any] | None:
        return self.values.get((project_id, scope, key))

    def put(
        self,
        scope: str,
        key: str,
        payload: dict[str, Any],
        *,
        project_id: str | None = None,
        ttl_seconds: float | None = None,
    ) -> None:
        self.values[(project_id, scope, key)] = payload


class _Jobs:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, Any]]] = []

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        **_kwargs: Any,
    ) -> int:
        self.enqueued.append((kind, payload))
        return len(self.enqueued)


def test_signal_payload_adds_id_that_satisfies_ops_contract(monkeypatch: Any) -> None:
    app_module = _load_app_module(monkeypatch)

    sig = app_module.SignalIn(
        source="manual",
        kind="bash_post",
        target="repo",
        severity="info",
        payload={},
    )
    payload = app_module._signal_payload(sig)

    assert payload["id"]
    validate_payload(
        [IOField(name="signal", type="Signal", required=True)],
        {"signal": payload},
        agent="ops",
        direction="input",
    )


async def test_runtime_chain_advance_marks_unhandled_exception_failed(
    monkeypatch: Any,
) -> None:
    app_module = _load_app_module(monkeypatch)
    idem = _Idem()
    jobs = _Jobs()

    async def fail_execute(
        _handoff: Handoff, _project_id: str
    ) -> tuple[Any, Handoff | None]:
        raise ValueError("bad json")

    handoff = Handoff(from_agent="user", to_agent="architect", payload={"brief": "x"})
    await app_module._runtime_chain_advance(
        {
            "chain_id": "chain-1",
            "project_id": "tenant-a",
            "max_hops": 6,
            "chain": True,
            "step": 0,
            "hops": [{"agent": "architect", "status": "delegated"}],
            "handoff": handoff.model_dump(mode="json"),
        },
        settings=_Settings(),
        idem_cache=idem,
        job_queue=jobs,
        execute_for_project=fail_execute,
    )

    cached = idem.get("chain", "chain-1", project_id="tenant-a")
    assert cached is not None
    assert cached["status"] == "failed"
    assert cached["error"] == "bad json"
    assert cached["error_type"] == "ValueError"
    assert cached["hops"] == [{"agent": "architect", "status": "delegated"}]
    assert jobs.enqueued == []
