"""Python client for the agentcore HTTP surface.

Two flavors share one method set:
  - `AgentcoreClient` (sync, backed by `httpx.Client`)
  - `AsyncAgentcoreClient` (async, backed by `httpx.AsyncClient`)

Both auto-attach `Authorization: Bearer ...` when an `api_token` is
provided (or pulled from `AGENTCORE_API_TOKEN`), accept an `X-Project-Id`
header per call, and can auto-generate `Idempotency-Key` headers when
`auto_idempotency=True`. Methods return plain dicts (the same shapes the
HTTP endpoints emit) so callers don't import server-side Pydantic models.

Example
-------

    from agentcore.client import AgentcoreClient

    cli = AgentcoreClient("http://localhost:8088", api_token="...")
    res = cli.run(to_agent="architect", payload={"brief": "add /metrics"})
    print(res["task_id"])
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import httpx


class _BaseClient:
    """Shared config + header construction for sync + async clients."""

    def __init__(
        self,
        base_url: str = "http://localhost:8088",
        *,
        api_token: str | None = None,
        project_id: str | None = None,
        auto_idempotency: bool = False,
        timeout: float = 60.0,
        verify_tls: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token if api_token is not None else os.environ.get(
            "AGENTCORE_API_TOKEN"
        )
        self.default_project = project_id
        self.auto_idempotency = auto_idempotency
        self.timeout = timeout
        self.verify_tls = verify_tls

    def _headers(
        self,
        *,
        idempotency_key: str | None,
        project_id: str | None,
        mutating: bool,
    ) -> dict[str, str]:
        h: dict[str, str] = {"Accept": "application/json"}
        if self.api_token:
            h["Authorization"] = f"Bearer {self.api_token}"
        pid = project_id or self.default_project
        if pid:
            h["X-Project-Id"] = pid
        if mutating:
            key = idempotency_key
            if key is None and self.auto_idempotency:
                key = uuid.uuid4().hex
            if key:
                h["Idempotency-Key"] = key
        return h


# ---------------------------------------------------------------------------
# Synchronous client
# ---------------------------------------------------------------------------


class AgentcoreClient(_BaseClient):
    """Synchronous client. Construct once per app; safe to share across threads."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client = httpx.Client(
            base_url=self.base_url, timeout=self.timeout, verify=self.verify_tls
        )

    def __enter__(self) -> AgentcoreClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---- read endpoints ----------------------------------------------

    def healthz(self) -> dict[str, Any]:
        r = self._client.get("/healthz", headers=self._headers(
            idempotency_key=None, project_id=None, mutating=False
        ))
        r.raise_for_status()
        return r.json()

    def agents(self) -> dict[str, Any]:
        r = self._client.get("/agents", headers=self._headers(
            idempotency_key=None, project_id=None, mutating=False
        ))
        r.raise_for_status()
        return r.json()

    def capabilities(self) -> dict[str, Any]:
        r = self._client.get("/capabilities", headers=self._headers(
            idempotency_key=None, project_id=None, mutating=False
        ))
        r.raise_for_status()
        return r.json()

    def trace(self, task_id: str) -> dict[str, Any]:
        r = self._client.get(f"/tasks/{task_id}/trace", headers=self._headers(
            idempotency_key=None, project_id=None, mutating=False
        ))
        r.raise_for_status()
        return r.json()

    def chain_status(
        self, chain_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        r = self._client.get(
            f"/chains/{chain_id}",
            headers=self._headers(
                idempotency_key=None, project_id=project_id, mutating=False
            ),
        )
        r.raise_for_status()
        return r.json()

    # ---- mutating endpoints ------------------------------------------

    def run(
        self,
        to_agent: str,
        payload: dict[str, Any],
        *,
        from_agent: str = "user",
        chain: bool = True,
        max_hops: int = 6,
        durable: bool = False,
        idempotency_key: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "to_agent": to_agent,
            "payload": payload,
            "from_agent": from_agent,
            "chain": chain,
            "max_hops": max_hops,
            "durable": durable,
        }
        r = self._client.post(
            "/run",
            json=body,
            headers=self._headers(
                idempotency_key=idempotency_key,
                project_id=project_id,
                mutating=True,
            ),
        )
        r.raise_for_status()
        return r.json()

    def handoff(
        self,
        to_agent: str,
        payload: dict[str, Any],
        *,
        from_agent: str = "user",
        idempotency_key: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        body = {"to_agent": to_agent, "payload": payload, "from_agent": from_agent}
        r = self._client.post(
            "/handoff",
            json=body,
            headers=self._headers(
                idempotency_key=idempotency_key,
                project_id=project_id,
                mutating=True,
            ),
        )
        r.raise_for_status()
        return r.json()

    def signal(
        self,
        source: str,
        kind: str,
        target: str,
        *,
        severity: str = "info",
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "source": source,
            "kind": kind,
            "target": target,
            "severity": severity,
            "payload": payload or {},
        }
        r = self._client.post(
            "/signal",
            json=body,
            headers=self._headers(
                idempotency_key=idempotency_key,
                project_id=project_id,
                mutating=True,
            ),
        )
        r.raise_for_status()
        return r.json()

    # ---- wiki ---------------------------------------------------------

    def wiki_index(self, *, project_id: str | None = None) -> dict[str, Any]:
        r = self._client.get(
            "/wiki",
            headers=self._headers(
                idempotency_key=None, project_id=project_id, mutating=False
            ),
        )
        r.raise_for_status()
        return r.json()

    def wiki_page(
        self, path: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        r = self._client.get(
            f"/wiki/{path}",
            headers=self._headers(
                idempotency_key=None, project_id=project_id, mutating=False
            ),
        )
        r.raise_for_status()
        return r.json()

    def wiki_search(
        self, query: str, *, k: int = 8, project_id: str | None = None
    ) -> dict[str, Any]:
        r = self._client.get(
            "/wiki/search",
            params={"q": query, "k": k},
            headers=self._headers(
                idempotency_key=None, project_id=project_id, mutating=False
            ),
        )
        r.raise_for_status()
        return r.json()

    def wiki_refresh(
        self,
        mode: str = "incremental",
        *,
        changed_paths: list[str] | None = None,
        commit_sha: str | None = None,
        idempotency_key: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "mode": mode,
            "changed_paths": changed_paths or [],
            "commit_sha": commit_sha,
        }
        r = self._client.post(
            "/wiki/refresh",
            json=body,
            headers=self._headers(
                idempotency_key=idempotency_key,
                project_id=project_id,
                mutating=True,
            ),
        )
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Async client (mirrors the sync surface)
# ---------------------------------------------------------------------------


class AsyncAgentcoreClient(_BaseClient):
    """Async client. Use as `async with AsyncAgentcoreClient(...) as cli: ...`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout, verify=self.verify_tls
        )

    async def __aenter__(self) -> AsyncAgentcoreClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def healthz(self) -> dict[str, Any]:
        r = await self._client.get("/healthz", headers=self._headers(
            idempotency_key=None, project_id=None, mutating=False
        ))
        r.raise_for_status()
        return r.json()

    async def agents(self) -> dict[str, Any]:
        r = await self._client.get("/agents", headers=self._headers(
            idempotency_key=None, project_id=None, mutating=False
        ))
        r.raise_for_status()
        return r.json()

    async def capabilities(self) -> dict[str, Any]:
        r = await self._client.get("/capabilities", headers=self._headers(
            idempotency_key=None, project_id=None, mutating=False
        ))
        r.raise_for_status()
        return r.json()

    async def trace(self, task_id: str) -> dict[str, Any]:
        r = await self._client.get(f"/tasks/{task_id}/trace", headers=self._headers(
            idempotency_key=None, project_id=None, mutating=False
        ))
        r.raise_for_status()
        return r.json()

    async def chain_status(
        self, chain_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        r = await self._client.get(
            f"/chains/{chain_id}",
            headers=self._headers(
                idempotency_key=None, project_id=project_id, mutating=False
            ),
        )
        r.raise_for_status()
        return r.json()

    async def run(
        self,
        to_agent: str,
        payload: dict[str, Any],
        *,
        from_agent: str = "user",
        chain: bool = True,
        max_hops: int = 6,
        durable: bool = False,
        idempotency_key: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "to_agent": to_agent,
            "payload": payload,
            "from_agent": from_agent,
            "chain": chain,
            "max_hops": max_hops,
            "durable": durable,
        }
        r = await self._client.post(
            "/run",
            json=body,
            headers=self._headers(
                idempotency_key=idempotency_key,
                project_id=project_id,
                mutating=True,
            ),
        )
        r.raise_for_status()
        return r.json()

    async def handoff(
        self,
        to_agent: str,
        payload: dict[str, Any],
        *,
        from_agent: str = "user",
        idempotency_key: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        body = {"to_agent": to_agent, "payload": payload, "from_agent": from_agent}
        r = await self._client.post(
            "/handoff",
            json=body,
            headers=self._headers(
                idempotency_key=idempotency_key,
                project_id=project_id,
                mutating=True,
            ),
        )
        r.raise_for_status()
        return r.json()

    async def signal(
        self,
        source: str,
        kind: str,
        target: str,
        *,
        severity: str = "info",
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "source": source,
            "kind": kind,
            "target": target,
            "severity": severity,
            "payload": payload or {},
        }
        r = await self._client.post(
            "/signal",
            json=body,
            headers=self._headers(
                idempotency_key=idempotency_key,
                project_id=project_id,
                mutating=True,
            ),
        )
        r.raise_for_status()
        return r.json()

    async def wiki_index(self, *, project_id: str | None = None) -> dict[str, Any]:
        r = await self._client.get(
            "/wiki",
            headers=self._headers(
                idempotency_key=None, project_id=project_id, mutating=False
            ),
        )
        r.raise_for_status()
        return r.json()

    async def wiki_page(
        self, path: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        r = await self._client.get(
            f"/wiki/{path}",
            headers=self._headers(
                idempotency_key=None, project_id=project_id, mutating=False
            ),
        )
        r.raise_for_status()
        return r.json()

    async def wiki_search(
        self, query: str, *, k: int = 8, project_id: str | None = None
    ) -> dict[str, Any]:
        r = await self._client.get(
            "/wiki/search",
            params={"q": query, "k": k},
            headers=self._headers(
                idempotency_key=None, project_id=project_id, mutating=False
            ),
        )
        r.raise_for_status()
        return r.json()

    async def wiki_refresh(
        self,
        mode: str = "incremental",
        *,
        changed_paths: list[str] | None = None,
        commit_sha: str | None = None,
        idempotency_key: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "mode": mode,
            "changed_paths": changed_paths or [],
            "commit_sha": commit_sha,
        }
        r = await self._client.post(
            "/wiki/refresh",
            json=body,
            headers=self._headers(
                idempotency_key=idempotency_key,
                project_id=project_id,
                mutating=True,
            ),
        )
        r.raise_for_status()
        return r.json()


__all__ = ["AgentcoreClient", "AsyncAgentcoreClient"]
