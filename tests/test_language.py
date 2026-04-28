"""Repository language detection + LSP probing."""

from __future__ import annotations

from pathlib import Path

from agentcore.language import detect_languages, probe_lsps


def test_detect_languages_picks_python(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hi')")
    (tmp_path / "lib.py").write_text("def f(): pass")
    (tmp_path / "README.md").write_text("# repo")
    profile = detect_languages(tmp_path)
    assert profile.primary == "python"
    assert profile.counts["python"] == 2


def test_detect_languages_skips_excluded_dirs(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("//")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.go").write_text("package main")
    profile = detect_languages(tmp_path)
    assert profile.primary == "go"
    assert "javascript" not in profile.counts


def test_probe_lsps_returns_recommendation_when_missing() -> None:
    statuses = probe_lsps(["python"])
    assert len(statuses) == 1
    assert statuses[0].language == "python"
    # The CI host may or may not have pyright installed; either way we get a
    # status with an install_hint.
    assert statuses[0].install_hint
