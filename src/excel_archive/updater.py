"""Lightweight git-based self-update for dev installs and frozen .app rebuilds."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .paths import default_archive_root


class UpdateError(Exception):
    """User-visible update failure."""


@dataclass(frozen=True)
class AppSettings:
    repo_root: str | None = None
    branch: str = "main"
    remote: str = "origin"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AppSettings:
        return cls(
            repo_root=raw.get("repo_root"),
            branch=str(raw.get("branch") or "main"),
            remote=str(raw.get("remote") or "origin"),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"branch": self.branch, "remote": self.remote}
        if self.repo_root:
            out["repo_root"] = self.repo_root
        return out


@dataclass(frozen=True)
class UpdateStatus:
    current_sha: str
    remote_sha: str
    behind: bool
    branch: str
    remote: str
    summary: str

    @property
    def up_to_date(self) -> bool:
        return not self.behind and self.current_sha == self.remote_sha


def default_app_settings_path() -> Path:
    return default_archive_root() / "app.json"


def load_app_settings() -> AppSettings:
    path = default_app_settings_path()
    if not path.is_file():
        return AppSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UpdateError(f"Invalid app settings at {path}: {exc}") from exc
    return AppSettings.from_dict(raw if isinstance(raw, dict) else {})


def save_app_settings(settings: AppSettings) -> Path:
    path = default_app_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def _is_valid_repo_root(path: Path) -> bool:
    return (path / ".git").is_dir() and (path / "pyproject.toml").is_file()


def discover_repo_root(*, settings: AppSettings | None = None) -> Path | None:
    """Resolve local clone used for git pull + rebuild."""
    settings = settings or load_app_settings()

    env = os.environ.get("EXCEL_ARCHIVE_REPO", "").strip()
    if env:
        candidate = Path(env).expanduser().resolve()
        if _is_valid_repo_root(candidate):
            return candidate

    if settings.repo_root:
        candidate = Path(settings.repo_root).expanduser().resolve()
        if _is_valid_repo_root(candidate):
            return candidate

    pkg_root = Path(__file__).resolve().parents[2]
    if _is_valid_repo_root(pkg_root):
        return pkg_root

    for guess in (
        Path.home() / "Documents" / "GitHub" / "claude-excel-archive",
        Path.home() / "Documents" / "GitHub" / "PPLX-archiver",
    ):
        if _is_valid_repo_root(guess):
            return guess.resolve()

    return None


def git_cmd(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def git_short_sha(repo: Path, ref: str = "HEAD") -> str:
    proc = git_cmd(repo, "rev-parse", "--short", ref, check=False)
    if proc.returncode != 0:
        return "unknown"
    return proc.stdout.strip() or "unknown"


def working_tree_clean(repo: Path) -> tuple[bool, str]:
    proc = git_cmd(repo, "status", "--porcelain", check=False)
    if proc.returncode != 0:
        return False, proc.stderr.strip() or "git status failed"
    dirty = proc.stdout.strip()
    if dirty:
        return False, "Working tree has uncommitted changes. Commit or stash before updating."
    return True, ""


def local_version_label(repo: Path | None = None) -> str:
    """Display version: package version + optional git sha."""
    if repo is None:
        repo = discover_repo_root()
    if repo is not None:
        sha = git_short_sha(repo)
        if sha != "unknown":
            return f"v{__version__} ({sha})"
    return f"v{__version__}"


def check_for_updates(
    repo: Path,
    *,
    branch: str | None = None,
    remote: str | None = None,
    settings: AppSettings | None = None,
) -> UpdateStatus:
    settings = settings or load_app_settings()
    branch = branch or settings.branch
    remote = remote or settings.remote

    fetch = git_cmd(repo, "fetch", remote, branch, check=False)
    if fetch.returncode != 0:
        raise UpdateError(fetch.stderr.strip() or fetch.stdout.strip() or "git fetch failed")

    current = git_short_sha(repo, "HEAD")
    remote_ref = f"{remote}/{branch}"
    remote_proc = git_cmd(repo, "rev-parse", "--short", remote_ref, check=False)
    if remote_proc.returncode != 0:
        raise UpdateError(
            f"Remote ref {remote_ref} not found. "
            f"Check branch name in {default_app_settings_path()}."
        )
    remote_sha = remote_proc.stdout.strip()

    base_proc = git_cmd(repo, "merge-base", "HEAD", remote_ref, check=False)
    if base_proc.returncode != 0:
        raise UpdateError("Could not compare local HEAD with remote branch.")

    behind_proc = git_cmd(
        repo,
        "rev-list",
        "--count",
        f"HEAD..{remote_ref}",
        check=False,
    )
    ahead_proc = git_cmd(
        repo,
        "rev-list",
        "--count",
        f"{remote_ref}..HEAD",
        check=False,
    )
    behind_n = int(behind_proc.stdout.strip() or "0") if behind_proc.returncode == 0 else 0
    ahead_n = int(ahead_proc.stdout.strip() or "0") if ahead_proc.returncode == 0 else 0

    behind = behind_n > 0
    if behind:
        summary = f"{behind_n} commit(s) behind {remote}/{branch} ({current} → {remote_sha})"
    elif ahead_n > 0:
        summary = f"Up to date with remote; {ahead_n} local commit(s) ahead ({current})"
    else:
        summary = f"Up to date ({current})"

    return UpdateStatus(
        current_sha=current,
        remote_sha=remote_sha,
        behind=behind,
        branch=branch,
        remote=remote,
        summary=summary,
    )


def pull_updates(
    repo: Path,
    *,
    branch: str | None = None,
    remote: str | None = None,
    settings: AppSettings | None = None,
) -> str:
    settings = settings or load_app_settings()
    branch = branch or settings.branch
    remote = remote or settings.remote

    clean, msg = working_tree_clean(repo)
    if not clean:
        raise UpdateError(msg)

    proc = git_cmd(
        repo,
        "pull",
        "--ff-only",
        remote,
        branch,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise UpdateError(detail or "git pull failed")

    return git_short_sha(repo, "HEAD")


def refresh_install(repo: Path, *, frozen: bool) -> str:
    """Refresh running install after pull. Returns human-readable result."""
    if frozen:
        script = repo / "scripts" / "build_app.sh"
        if not script.is_file():
            raise UpdateError(f"Build script not found: {script}")
        proc = subprocess.run(
            ["bash", str(script)],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
            raise UpdateError("Rebuild failed:\n" + "\n".join(tail))
        return "Rebuilt and installed Excel Archive.app to /Applications"

    venv_pip = repo / ".venv" / "bin" / "pip"
    if not venv_pip.is_file():
        subprocess.run(
            ["python3", "-m", "venv", str(repo / ".venv")],
            cwd=repo,
            check=True,
        )
    proc = subprocess.run(
        [str(venv_pip), "install", "-q", "-e", ".[app,build]"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise UpdateError(detail or "pip install failed")
    return "Updated editable install in .venv"


def run_update(
    *,
    check_only: bool = False,
    frozen: bool = False,
    branch: str | None = None,
    remote: str | None = None,
    repo: Path | None = None,
) -> tuple[UpdateStatus, str | None]:
    """
    Check for updates; optionally pull and refresh install.

    Returns (status, post_action_message or None).
    """
    settings = load_app_settings()
    resolved = repo or discover_repo_root(settings=settings)
    if resolved is None:
        raise UpdateError(
            "No git repo found. Set EXCEL_ARCHIVE_REPO, choose Set Repo Path in the menu, "
            f"or add repo_root to {default_app_settings_path()}."
        )

    status = check_for_updates(resolved, branch=branch, remote=remote, settings=settings)
    if check_only or not status.behind:
        return status, None

    new_sha = pull_updates(resolved, branch=branch, remote=remote, settings=settings)
    install_msg = refresh_install(resolved, frozen=frozen)
    restart = (
        "Quit Excel Archive and reopen the app to use the new build."
        if frozen
        else "Restart the menu bar app to load the updated code."
    )
    return status, f"Pulled {new_sha}. {install_msg}. {restart}"


def set_repo_root(path: Path) -> Path:
    """Persist repo path for menu bar / frozen app updates."""
    resolved = path.expanduser().resolve()
    if not _is_valid_repo_root(resolved):
        raise UpdateError(f"Not a claude-excel-archive repo: {resolved}")
    settings = load_app_settings()
    save_app_settings(
        AppSettings(
            repo_root=str(resolved),
            branch=settings.branch,
            remote=settings.remote,
        )
    )
    return resolved
