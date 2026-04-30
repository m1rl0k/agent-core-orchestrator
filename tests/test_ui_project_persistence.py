"""Regression: explicit `?project=...` survives navigation.

Operators pick a tenant from the header switcher; if any nav or
intra-page link drops the query param, the next click silently falls
back to the server-default project and the dashboard "resets".

These tests mount the real UI routes against stubbed deps and assert the
rendered HTML preserves `?project=` on every link that goes back into
the UI.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_settings import SettingsConfigDict

import agentcore.ui.routes as routes
from agentcore.settings import Settings
from agentcore.ui import mount_ui


class _IsolatedSettings(Settings):
    """Settings that ignore host `.env` so tests stay deterministic."""

    model_config = SettingsConfigDict(env_file=None, env_prefix="AGENTCORE_")


@dataclass
class _HostInfo:
    os: str = "linux"
    arch: str = "x86_64"
    shell: str = "bash"


class _Registry:
    def all(self) -> list[Any]:
        return []

    def errors(self) -> list[Any]:
        return []


class _JobQueue:
    is_persistent = False

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def list_dead_letter(self, *, project_id: str, limit: int) -> list[Any]:
        return []


class _IdemCache:
    def get(self, *_a: Any, **_k: Any) -> dict[str, Any] | None:
        return None


@dataclass
class _WikiPage:
    rel = "modules/demo.md"
    title = "Demo"
    sources: list[str] = field(default_factory=lambda: ["src/demo.py"])
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body = "demo"


class _WikiStorage:
    def __init__(
        self,
        wiki_root: str = "/tmp/wiki",
        project: str = "default",
        branch: str = "main",
        *,
        settings: Any = None,
    ) -> None:
        self.wiki_root = wiki_root
        self.project = project
        self.branch = branch
        self.settings = settings
        self.root = f"{wiki_root}/{project}/{branch}"

    def walk(self) -> list[_WikiPage]:
        return [_WikiPage()]

    def read(self, rel: str) -> _WikiPage | None:
        return _WikiPage() if rel == _WikiPage.rel else None


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """A TestClient with all DB-touching helpers stubbed to no-ops so the
    tests focus on link rendering, not data plumbing."""
    settings = _IsolatedSettings(
        AGENTCORE_PROJECT_NAME="default",
        AGENTCORE_AGENTS_DIR="/tmp/agents-doesnt-matter",
        AGENTCORE_ENABLE_WIKI=True,
    )

    # Pretend two tenants exist in the DB so the switcher renders both.
    monkeypatch.setattr(
        routes, "_known_projects", lambda _s: ["__all__", "default", "tenant-a"]
    )
    monkeypatch.setattr(routes, "_compute_stats", lambda *_a, **_k: {})
    monkeypatch.setattr(routes, "_recent_chains", lambda *_a, **_k: [])
    monkeypatch.setattr(routes, "_agent_activity", lambda *_a, **_k: {})
    monkeypatch.setattr(routes, "_recent_jobs", lambda *_a, **_k: [])
    monkeypatch.setattr(routes, "_job_counts", lambda *_a, **_k: {})
    monkeypatch.setattr(
        routes, "_graph_snapshot", lambda *_a, **_k: ([], [], {})
    )
    monkeypatch.setattr(routes, "_graph_sizes", lambda *_a, **_k: (0, 0))
    monkeypatch.setattr(
        "agentcore.wiki.storage.WikiStorage",
        _WikiStorage,
    )

    @contextmanager
    def fake_pg_conn(*_a: Any, **_k: Any):
        raise RuntimeError("DB should not be touched in this test")
        yield  # pragma: no cover

    monkeypatch.setattr(routes, "pg_conn", fake_pg_conn)

    app = FastAPI()
    mount_ui(
        app,
        settings=settings,
        registry=_Registry(),
        job_queue=_JobQueue(settings),
        idem_cache=_IdemCache(),
        host_info=_HostInfo(),
        wiki_storage=_WikiStorage(project="default"),
    )
    return TestClient(app)


def test_dashboard_nav_links_carry_explicit_project(client: TestClient) -> None:
    """Header nav links must thread `?project=tenant-a` so clicking
    Chains/Agents/Jobs/Graph/Wiki doesn't fall back to default."""
    r = client.get("/ui/dashboard", params={"project": "tenant-a"})
    assert r.status_code == 200
    html = r.text

    for path in ("/ui/dashboard", "/ui/agents", "/ui/chains", "/ui/jobs", "/ui/graph"):
        assert f'href="http://testserver{path}?project=tenant-a"' in html, (
            f"nav link {path!r} dropped ?project=tenant-a — operator's "
            f"tenant choice would be lost on click"
        )


def test_dashboard_default_project_keeps_urls_clean(client: TestClient) -> None:
    """When no explicit override is set, URLs stay clean — no
    `?project=default` clutter."""
    r = client.get("/ui/dashboard")
    assert r.status_code == 200
    html = r.text
    # The Chains link should be the bare URL with no query string.
    assert 'href="http://testserver/ui/chains"' in html
    assert "?project=default" not in html


def test_chains_row_link_carries_project(client: TestClient, monkeypatch) -> None:
    """The chains list row drill-down must preserve the tenant param so
    chain detail loads under the same project."""
    monkeypatch.setattr(
        routes,
        "_recent_chains",
        lambda *_a, **_k: [
            {
                "chain_id": "chain-xyz-1234",
                "status": "done",
                "hops": [{"agent": "developer"}],
                "updated_at": "now",
            }
        ],
    )
    r = client.get("/ui/chains", params={"project": "tenant-a"})
    assert r.status_code == 200
    assert 'href="/ui/chains/chain-xyz-1234?project=tenant-a"' in r.text


def test_project_switcher_marks_current_tenant_selected(
    client: TestClient,
) -> None:
    """The header `<select>` must reflect the active project so the
    operator sees what they're looking at."""
    r = client.get("/ui/dashboard", params={"project": "tenant-a"})
    assert r.status_code == 200
    # Markup wraps `<option>` content over multiple lines and the
    # `selected` attribute is bare — match those traits without being
    # whitespace-sensitive.
    assert '<option value="tenant-a" selected>' in r.text


def test_wiki_index_switcher_marks_current_tenant(client: TestClient) -> None:
    """On the wiki index, the `<select>` dropdown must show the
    requested project as selected — the wiki page is the only one with
    a custom `project_switch_url`, so this guards the dropdown rendering
    on that override path."""
    r = client.get("/ui/wiki", params={"project": "tenant-a"})
    assert r.status_code == 200
    assert '<option value="tenant-a" selected>' in r.text


def test_wiki_page_switcher_marks_current_tenant(client: TestClient) -> None:
    """Same guard for `/ui/wiki/page/<rel>` — switching project from a
    wiki page should kick the operator back to the wiki index for the
    new tenant, but the dropdown must still highlight the current
    tenant on render."""
    r = client.get(
        "/ui/wiki/page/modules/demo.md", params={"project": "tenant-a"}
    )
    assert r.status_code == 200
    assert '<option value="tenant-a" selected>' in r.text


def test_wiki_uses_selected_project_storage(client: TestClient) -> None:
    r = client.get("/ui/wiki", params={"project": "tenant-a"})

    assert r.status_code == 200
    assert "wiki:tenant-a:main" in r.text
    assert "/tmp/wiki/tenant-a/main" in r.text
    assert (
        'href="http://testserver/ui/wiki/page/modules/demo.md?project=tenant-a&branch=main"'
        in r.text
    )
    assert 'method="get" action="http://testserver/ui/wiki"' in r.text


def test_wiki_page_project_switch_returns_to_wiki_index(client: TestClient) -> None:
    r = client.get("/ui/wiki/page/modules/demo.md", params={"project": "tenant-a"})

    assert r.status_code == 200
    assert 'method="get" action="http://testserver/ui/wiki"' in r.text
    assert 'href="http://testserver/ui/wiki?project=tenant-a"' in r.text


def test_wiki_all_view_lists_concrete_project_pages(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(
        routes,
        "_all_wiki_pages",
        lambda *_a, **_k: [
            {
                "project_id": "records-test",
                "branch": "master",
                "rel": "index.md",
                "title": "Records",
                "sources": ["records.py"],
            }
        ],
    )

    r = client.get("/ui/wiki", params={"project": "__all__"})

    assert r.status_code == 200
    assert '<option value="__all__" selected>' in r.text
    assert "<code>records-test</code>" in r.text
    assert "<code>master</code>" in r.text
    assert (
        'href="http://testserver/ui/wiki/page/index.md?project=records-test&branch=master"'
        in r.text
    )
