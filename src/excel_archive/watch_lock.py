"""Ensure only one excel-archive watch process polls a given IndexedDB."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WatchLock:
    path: Path
    acquired: bool = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(str(self.path), flags, 0o644)
        except FileExistsError:
            self.acquired = False
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        self.acquired = True
        return True

    def release(self) -> None:
        if self.acquired and self.path.is_file():
            try:
                self.path.unlink()
            except OSError:
                pass
        self.acquired = False

    def holder_pid(self) -> int | None:
        if not self.path.is_file():
            return None
        try:
            return int(self.path.read_text(encoding="utf-8").strip())
        except ValueError:
            return None

    def __enter__(self) -> WatchLock:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


def default_watch_lock_path(archive_root: Path) -> Path:
    return archive_root / ".watch.lock"
