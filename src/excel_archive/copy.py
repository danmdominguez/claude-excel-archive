"""WAL-safe copy of IndexedDB SQLite files."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Literal

from .paths import IdbDatabasePaths, default_archive_root

SnapshotStyle = Literal["rolling", "per-poll", "off"]


def _safe_name(name: str) -> str:
    name = name.strip().replace(" ", "_")
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in name)


def snapshot_label(*, workbook_name: str | None = None) -> str:
    """
    Snapshot folder label.

    User convention:
    `YYYYMMDD_HHMM_<excelwbname>_snapshot`
    """
    ts = time.strftime("%Y%m%d_%H%M")
    if workbook_name:
        return f"{ts}_{_safe_name(workbook_name)}_snapshot"
    return f"{ts}_snapshot"


def copy_database(
    source: IdbDatabasePaths,
    dest_dir: Path,
    *,
    label: str | None = None,
    workbook_name: str | None = None,
) -> Path:
    """
    Copy sqlite + wal + shm into dest_dir/<label>/.

    Returns the directory containing the copied files.
    """
    label = label or snapshot_label(workbook_name=workbook_name)
    out = dest_dir / label
    out.mkdir(parents=True, exist_ok=True)

    for src in source.sidecar_paths():
        shutil.copy2(src, out / src.name)

    meta = out / "source.txt"
    meta.write_text(
        f"sqlite={source.sqlite}\n"
        f"site={source.site_folder}\n"
        f"copied_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n",
        encoding="utf-8",
    )
    return out


def copy_database_rolling(source: IdbDatabasePaths, live_dir: Path) -> Path:
    """
    Overwrite forensic/live/ with the latest sqlite + WAL sidecars.

    One stable directory per workbook — journal append (events.jsonl) remains the
    ordinal, growing record; this is only the latest recoverable IDB image.
    """
    live_dir.mkdir(parents=True, exist_ok=True)
    for src in source.sidecar_paths():
        shutil.copy2(src, live_dir / src.name)
    meta = live_dir / "source.txt"
    meta.write_text(
        f"sqlite={source.sqlite}\n"
        f"site={source.site_folder}\n"
        f"updated_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n",
        encoding="utf-8",
    )
    return live_dir


def copy_database_checkpoint(
    source: IdbDatabasePaths,
    history_dir: Path,
    *,
    workbook_name: str | None = None,
) -> Path:
    """
    Fallback: one timestamped sqlite file (no subfolder), sorts ordinally by name.

    Use for `excel-archive snapshot` or explicit checkpoints — not every watch poll.
    """
    history_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M")
    wb = _safe_name(workbook_name) if workbook_name else "Excel"
    out = history_dir / f"{ts}_{wb}_IndexedDB.sqlite3"
    shutil.copy2(source.sqlite, out)
    return out


def default_snapshots_dir(root: Path | None = None) -> Path:
    base = root or default_archive_root()
    return base / "snapshots"
