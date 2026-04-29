"""HTTP client header behavior."""

from __future__ import annotations

from agentcore.client import AgentcoreClient


def test_client_headers_include_token_project_and_idempotency_key() -> None:
    client = AgentcoreClient(
        "http://localhost:8088",
        api_token="secret",
        project_id="project-a",
    )
    try:
        headers = client._headers(
            idempotency_key="idem-1",
            project_id=None,
            mutating=True,
        )
    finally:
        client.close()

    assert headers["Authorization"] == "Bearer secret"
    assert headers["X-Project-Id"] == "project-a"
    assert headers["Idempotency-Key"] == "idem-1"


def test_client_per_call_project_overrides_default() -> None:
    client = AgentcoreClient(
        "http://localhost:8088",
        api_token="secret",
        project_id="project-a",
    )
    try:
        headers = client._headers(
            idempotency_key=None,
            project_id="project-b",
            mutating=False,
        )
    finally:
        client.close()

    assert headers["X-Project-Id"] == "project-b"
    assert "Idempotency-Key" not in headers


def test_client_auto_idempotency_only_for_mutating_calls() -> None:
    client = AgentcoreClient(
        "http://localhost:8088",
        auto_idempotency=True,
    )
    try:
        read_headers = client._headers(
            idempotency_key=None,
            project_id=None,
            mutating=False,
        )
        write_headers = client._headers(
            idempotency_key=None,
            project_id=None,
            mutating=True,
        )
    finally:
        client.close()

    assert "Idempotency-Key" not in read_headers
    assert len(write_headers["Idempotency-Key"]) == 32
