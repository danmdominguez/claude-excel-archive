"""WAL-safe copy of IndexedDB SQLite files."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from .paths import IdbDatabasePaths, default_archive_root


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


def default_snapshots_dir(root: Path | None = None) -> Path:
    base = root or default_archive_root()
    return base / "snapshots"
