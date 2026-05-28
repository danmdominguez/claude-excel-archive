"""Retention and rotation policies for ExcelArchive output."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config_excel import RetentionConfig


@dataclass(frozen=True)
class RetentionReport:
    deleted_files: int
    deleted_dirs: int
    kept_files: int
    notes: list[str]


def _sorted_dirs(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    dirs = [p for p in path.iterdir() if p.is_dir()]
    return sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True)


def _sorted_files(path: Path, suffix: str | None = None) -> list[Path]:
    if not path.is_dir():
        return []
    files = [p for p in path.iterdir() if p.is_file()]
    if suffix:
        files = [p for p in files if p.name.endswith(suffix)]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _delete_path(p: Path, *, dry_run: bool) -> tuple[int, int]:
    """Returns (deleted_files, deleted_dirs)."""
    if dry_run:
        return (0, 1 if p.is_dir() else 0) if p.exists() else (0, 0)
    if not p.exists():
        return (0, 0)
    if p.is_file():
        p.unlink(missing_ok=True)
        return (1, 0)
    # directory: delete recursively
    import shutil

    shutil.rmtree(p, ignore_errors=True)
    return (0, 1)


def enforce_retention_for_workbook_root(
    workbook_root: Path,
    *,
    cfg: RetentionConfig,
    dry_run: bool = False,
) -> RetentionReport:
    deleted_files = 0
    deleted_dirs = 0
    kept_files = 0
    notes: list[str] = []

    # 1) snapshots/
    snapshots = workbook_root / "snapshots"
    snap_dirs = _sorted_dirs(snapshots)
    for d in snap_dirs[cfg.keep_snapshot_dirs :]:
        f, dd = _delete_path(d, dry_run=dry_run)
        deleted_files += f
        deleted_dirs += dd
    notes.append(f"snapshots: kept={min(len(snap_dirs), cfg.keep_snapshot_dirs)} total={len(snap_dirs)}")

    # 1b) forensic/history/ flat checkpoint files (ordinal filenames)
    history = workbook_root / "forensic" / "history"
    hist_files = _sorted_files(history, suffix=".sqlite3")
    for fpath in hist_files[cfg.keep_forensic_checkpoints :]:
        f, dd = _delete_path(fpath, dry_run=dry_run)
        deleted_files += f
        deleted_dirs += dd
    notes.append(
        f"forensic/history: kept={min(len(hist_files), cfg.keep_forensic_checkpoints)} "
        f"total={len(hist_files)}"
    )

    # 2) workbook/ copies
    wb_dir = workbook_root / "workbook"
    wb_files = _sorted_files(wb_dir, suffix=".xlsx")
    for fpath in wb_files[cfg.keep_workbook_copies :]:
        f, dd = _delete_path(fpath, dry_run=dry_run)
        deleted_files += f
        deleted_dirs += dd
    kept_files += min(len(wb_files), cfg.keep_workbook_copies)
    notes.append(f"workbook: kept={min(len(wb_files), cfg.keep_workbook_copies)} total={len(wb_files)}")

    # 3) journal sessions: keep most recent N session dirs
    journal_root = workbook_root / "journal"
    sessions = _sorted_dirs(journal_root)
    for sess in sessions[cfg.keep_sessions :]:
        f, dd = _delete_path(sess, dry_run=dry_run)
        deleted_files += f
        deleted_dirs += dd
    notes.append(f"journal sessions: kept={min(len(sessions), cfg.keep_sessions)} total={len(sessions)}")

    # 4) per-session artifacts size cap
    max_bytes = cfg.max_artifacts_mb * 1024 * 1024
    for sess in _sorted_dirs(journal_root):
        artifacts = sess / "artifacts"
        if not artifacts.is_dir():
            continue
        files = _sorted_files(artifacts)
        total = 0
        for fp in files:
            total += fp.stat().st_size
        if total <= max_bytes:
            continue
        # Delete oldest first until under cap
        files_oldest_first = list(reversed(files))
        for fp in files_oldest_first:
            if total <= max_bytes:
                break
            sz = fp.stat().st_size
            f, dd = _delete_path(fp, dry_run=dry_run)
            deleted_files += f
            deleted_dirs += dd
            total -= sz
        notes.append(f"{sess.name}/artifacts capped to ~{cfg.max_artifacts_mb}MB")

    return RetentionReport(
        deleted_files=deleted_files,
        deleted_dirs=deleted_dirs,
        kept_files=kept_files,
        notes=notes,
    )

