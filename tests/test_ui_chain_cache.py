"""UI chain detail cache helpers."""

from __future__ import annotations

import agentcore.ui.routes as routes


def test_cached_reuses_value_within_ttl(monkeypatch) -> None:
    routes._CACHE.clear()
    now = [100.0]
    calls = {"count": 0}

    monkeypatch.setattr(routes.time, "monotonic", lambda: now[0])

    def load() -> dict[str, int]:
        calls["count"] += 1
        return {"value": calls["count"]}

    assert routes._cached(("proj", "chain_detail:abc"), load) == {"value": 1}
    assert routes._cached(("proj", "chain_detail:abc"), load) == {"value": 1}
    assert calls["count"] == 1

    now[0] += routes._CACHE_TTL + 0.1
    assert routes._cached(("proj", "chain_detail:abc"), load) == {"value": 2}
    assert calls["count"] == 2


def test_load_chain_detail_unifies_sources(monkeypatch) -> None:
    class _Idem:
        def get(self, scope: str, key: str, *, project_id: str):
            assert scope == "chain"
            assert key == "chain-1"
            assert project_id == "tenant-a"
            return {"chain_id": key, "status": "done", "hops": []}

    monkeypatch.setattr(routes, "_chain_in_flight_jobs", lambda *_a, **_k: [{"id": 1}])
    monkeypatch.setattr(routes, "_chain_review_history", lambda *_a, **_k: [{"kind": "result"}])

    detail = routes._load_chain_detail(
        object(), object(), _Idem(), "chain-1", project_id="tenant-a"
    )

    assert detail == {
        "chain": {"chain_id": "chain-1", "status": "done", "hops": []},
        "in_flight": [{"id": 1}],
        "review_history": [{"kind": "result"}],
    }
