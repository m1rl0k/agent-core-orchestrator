"""WikiStorage: atomic writes, hash-skip, frontmatter merge, walk."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic_settings import SettingsConfigDict

import agentcore.wiki.storage as storage_module
from agentcore.settings import Settings
from agentcore.wiki.storage import WikiPage, WikiStorage


def _new_storage(tmp_path: Path) -> WikiStorage:
    return WikiStorage(tmp_path / "wiki", project="proj", branch="main")


def test_collection_name_is_project_branch_scoped(tmp_path: Path) -> None:
    s = _new_storage(tmp_path)
    assert s.collection_name() == "wiki:proj:main"


def test_write_creates_page_with_audit_fields(tmp_path: Path) -> None:
    s = _new_storage(tmp_path)
    page = WikiPage(
        rel="modules/foo.md",
        frontmatter={"title": "foo", "sources": ["src/foo.py"]},
        body="Foo summary.",
    )
    changed = s.write(page, commit_sha="abc1234")
    assert changed is True
    on_disk = s.read("modules/foo.md")
    assert on_disk is not None
    assert on_disk.title == "foo"
    assert on_disk.frontmatter["last_commit"] == "abc1234"
    assert on_disk.frontmatter["last_updated"]
    assert on_disk.frontmatter["content_hash"]
    assert on_disk.body.strip() == "Foo summary."


def test_write_is_idempotent_on_unchanged_content(tmp_path: Path) -> None:
    s = _new_storage(tmp_path)
    page = WikiPage(
        rel="modules/foo.md",
        frontmatter={"title": "foo", "sources": ["src/foo.py"]},
        body="Foo summary.",
    )
    assert s.write(page, commit_sha="abc1234") is True
    # Same body + sources → hash-skip; should be a no-op.
    assert s.write(page, commit_sha="abc1234") is False


def test_write_changes_when_body_changes(tmp_path: Path) -> None:
    s = _new_storage(tmp_path)
    base = WikiPage(rel="x.md", frontmatter={"sources": ["a.py"]}, body="v1")
    s.write(base)
    revised = WikiPage(rel="x.md", frontmatter={"sources": ["a.py"]}, body="v2")
    assert s.write(revised) is True


def test_frontmatter_merge_preserves_existing_sources(tmp_path: Path) -> None:
    s = _new_storage(tmp_path)
    s.write(WikiPage(rel="x.md", frontmatter={"sources": ["a.py"]}, body="hi"))
    # Caller emits a partial frontmatter without `sources`; existing must survive.
    s.write(WikiPage(rel="x.md", frontmatter={"title": "X"}, body="hello"))
    on_disk = s.read("x.md")
    assert on_disk is not None
    assert on_disk.title == "X"
    assert on_disk.sources == ["a.py"]


def test_frontmatter_merge_unions_sources(tmp_path: Path) -> None:
    s = _new_storage(tmp_path)
    s.write(WikiPage(rel="x.md", frontmatter={"sources": ["a.py"]}, body="hi"))
    s.write(
        WikiPage(rel="x.md", frontmatter={"sources": ["a.py", "b.py"]}, body="hi v2")
    )
    on_disk = s.read("x.md")
    assert on_disk is not None
    assert on_disk.sources == ["a.py", "b.py"]


def test_audit_fields_cannot_be_smuggled_in(tmp_path: Path) -> None:
    """Callers shouldn't be able to override `last_updated` etc. — the
    storage layer owns those."""
    s = _new_storage(tmp_path)
    s.write(
        WikiPage(
            rel="x.md",
            frontmatter={
                "sources": ["a.py"],
                "last_updated": "1999-01-01T00:00:00+00:00",
                "content_hash": "deadbeef",
            },
            body="hi",
        )
    )
    on_disk = s.read("x.md")
    assert on_disk is not None
    assert on_disk.frontmatter["last_updated"] != "1999-01-01T00:00:00+00:00"
    assert on_disk.frontmatter["content_hash"] != "deadbeef"


def test_walk_yields_all_pages(tmp_path: Path) -> None:
    s = _new_storage(tmp_path)
    s.write(WikiPage(rel="modules/a.md", frontmatter={"sources": ["src/a.py"]}, body="a"))
    s.write(WikiPage(rel="modules/b.md", frontmatter={"sources": ["src/b.py"]}, body="b"))
    s.write(WikiPage(rel="subsystems/x.md", frontmatter={"sources": ["src/x.py"]}, body="x"))
    rels = sorted(p.rel for p in s.walk())
    assert rels == ["modules/a.md", "modules/b.md", "subsystems/x.md"]


def test_delete_returns_true_only_on_existing_page(tmp_path: Path) -> None:
    s = _new_storage(tmp_path)
    s.write(WikiPage(rel="x.md", frontmatter={"sources": ["a.py"]}, body="hi"))
    assert s.delete("x.md") is True
    assert s.delete("x.md") is False
    assert s.read("x.md") is None


def test_rejects_path_traversal(tmp_path: Path) -> None:
    s = _new_storage(tmp_path)
    for rel in ("../outside.md", "modules/../../outside.md", "/tmp/outside.md"):
        try:
            s.page_path(rel)
        except ValueError:
            pass
        else:  # pragma: no cover - assertion branch
            raise AssertionError(f"expected traversal rejection for {rel!r}")


class _IsolatedSettings(Settings):
    """Settings that ignore host `.env` so tests stay deterministic."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)


class _Cursor:
    def __init__(
        self,
        *,
        one: tuple[Any, ...] | None = None,
        many: list[tuple[Any, ...]] | None = None,
        rowcount: int = 0,
    ) -> None:
        self.one = one
        self.many = many or []
        self.rowcount = rowcount
        self.statements: list[tuple[str, tuple[Any, ...] | None]] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.statements.append((sql, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.one

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.many


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor


def _persistent_storage(tmp_path: Path, cursor: _Cursor, monkeypatch) -> WikiStorage:
    s = _new_storage(tmp_path)
    s.settings = _IsolatedSettings()
    s._persistent = True
    monkeypatch.setattr(
        storage_module.psycopg,
        "connect",
        lambda *_args, **_kwargs: _Connection(cursor),
    )
    return s


def test_read_prefers_postgres_page_over_disk(tmp_path: Path, monkeypatch) -> None:
    disk = _new_storage(tmp_path)
    disk.write(WikiPage(rel="x.md", frontmatter={"title": "disk"}, body="disk body"))

    cursor = _Cursor(one=({"title": "db", "sources": ["db.py"]}, "db body"))
    s = _persistent_storage(tmp_path, cursor, monkeypatch)

    page = s.read("x.md")

    assert page is not None
    assert page.title == "db"
    assert page.body == "db body"


def test_walk_unions_postgres_and_disk_preferring_postgres(
    tmp_path: Path, monkeypatch
) -> None:
    disk = _new_storage(tmp_path)
    disk.write(WikiPage(rel="x.md", frontmatter={"title": "disk-x"}, body="disk x"))
    disk.write(WikiPage(rel="y.md", frontmatter={"title": "disk-y"}, body="disk y"))

    cursor = _Cursor(
        many=[
            ("x.md", {"title": "db-x"}, "db x"),
            ("z.md", {"title": "db-z"}, "db z"),
        ]
    )
    s = _persistent_storage(tmp_path, cursor, monkeypatch)

    pages = {p.rel: p for p in s.walk()}

    assert sorted(pages) == ["x.md", "y.md", "z.md"]
    assert pages["x.md"].body == "db x"
    assert pages["y.md"].body == "disk y"
    assert pages["z.md"].body == "db z"


def test_delete_returns_true_when_only_postgres_row_deleted(
    tmp_path: Path, monkeypatch
) -> None:
    cursor = _Cursor(rowcount=1)
    s = _persistent_storage(tmp_path, cursor, monkeypatch)

    assert s.delete("db-only.md") is True
    assert "DELETE FROM agentcore_wiki_pages" in cursor.statements[-1][0]
