"""Copy the workbook file itself into the archive (option A)."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .paths import workbook_root_dir


def _safe_name(name: str) -> str:
    name = name.strip().replace(" ", "_")
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in name)


@dataclass
class WorkbookCopyState:
    last_sig: tuple[int, int] | None = None  # (mtime, size)


def workbook_copy_label(workbook_path: Path) -> str:
    ts = time.strftime("%Y%m%d_%H%M")
    return f"{ts}_{_safe_name(workbook_path.name)}_workbook.xlsx"


def workbook_signature(path: Path) -> tuple[int, int]:
    st = path.stat()
    return int(st.st_mtime), int(st.st_size)


def copy_workbook(
    workbook_path: Path,
    *,
    archive_root: Path | None = None,
    dest_dir: Path | None = None,
    state: WorkbookCopyState | None = None,
) -> Path | None:
    """
    Copy the .xlsx into <workbook_root>/workbook/ with timestamped name.

    Returns copied path, or None if unchanged since last copy (when state provided).
    """
    src = workbook_path.expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Workbook not found: {src}")

    sig = workbook_signature(src)
    if state is not None and state.last_sig == sig:
        return None

    out_dir = dest_dir or (workbook_root_dir(src, archive_root=archive_root) / "workbook")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / workbook_copy_label(src)
    shutil.copy2(src, out_path)

    if state is not None:
        state.last_sig = sig
    return out_path

