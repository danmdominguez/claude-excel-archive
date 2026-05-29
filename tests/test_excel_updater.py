"""Tests for git-based app self-update."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
import pytest

from excel_archive.updater import (
    AppSettings,
    UpdateError,
    check_for_updates,
    discover_repo_root,
    pull_updates,
    save_app_settings,
    set_repo_root,
    working_tree_clean,
)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=path, check=True, capture_output=True)


def test_discover_repo_root_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "clone"
    _init_repo(repo)
    monkeypatch.setenv("EXCEL_ARCHIVE_REPO", str(repo))
    monkeypatch.setattr(
        "excel_archive.updater.default_app_settings_path",
        lambda: tmp_path / "app.json",
    )
    assert discover_repo_root() == repo.resolve()


def test_set_repo_root_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "clone"
    _init_repo(repo)
    settings_path = tmp_path / "app.json"
    monkeypatch.setattr(
        "excel_archive.updater.default_app_settings_path",
        lambda: settings_path,
    )
    resolved = set_repo_root(repo)
    assert resolved == repo.resolve()
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["repo_root"] == str(repo.resolve())


def test_check_for_updates_behind(tmp_path: Path) -> None:
    repo = tmp_path / "local"
    remote = tmp_path / "remote.git"
    _init_repo(repo)
    subprocess.run(["git", "init", "--bare", str(remote)], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True, capture_output=True)

    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "v2"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=repo, check=True, capture_output=True)

    subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=repo, check=True, capture_output=True)

    status = check_for_updates(repo, branch="main", remote="origin")
    assert status.behind
    assert status.current_sha != status.remote_sha


def test_working_tree_clean_rejects_dirty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "dirty.txt").write_text("x", encoding="utf-8")
    clean, msg = working_tree_clean(repo)
    assert not clean
    assert "uncommitted" in msg.lower()


def test_pull_updates_dirty_tree_raises(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "dirty.txt").write_text("x", encoding="utf-8")
    with pytest.raises(UpdateError, match="uncommitted"):
        pull_updates(repo, branch="main", remote="origin")


def test_save_app_settings_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "app.json"
    monkeypatch.setattr("excel_archive.updater.default_app_settings_path", lambda: path)
    save_app_settings(AppSettings(repo_root="/tmp/x", branch="claude-archive"))
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["branch"] == "claude-archive"
    assert loaded["repo_root"] == "/tmp/x"
