"""Resolve executables for dev installs vs frozen .app bundles."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def python_executable() -> Path:
    return Path(sys.executable).resolve()


def cli_module_argv(*args: str) -> list[str]:
    """Argv to run excel-archive CLI (editable install or bundle)."""
    if is_frozen():
        return [str(python_executable()), "--run-cli", *args]
    return [str(python_executable()), "-m", "excel_archive.cli", *args]


def run_cli(
    *args: str,
    check: bool = False,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Editable dev install: ensure src on path when not frozen
    if not is_frozen():
        src = Path(__file__).resolve().parents[1]
        prev = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{src}{os.pathsep}{prev}" if prev else str(src)
    return subprocess.run(
        cli_module_argv(*args),
        env=env,
        check=check,
        capture_output=capture_output,
        text=True,
    )


def watch_lock_pid(lock_path: Path) -> int | None:
    if not lock_path.is_file():
        return None
    try:
        return int(lock_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_watch(lock_path: Path) -> bool:
    """SIGTERM watch process holding the lock. Returns True if a signal was sent."""
    pid = watch_lock_pid(lock_path)
    if pid is None or not is_pid_alive(pid):
        if lock_path.is_file():
            lock_path.unlink(missing_ok=True)
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        lock_path.unlink(missing_ok=True)
        return False
