"""Bundled assets (menu bar icon, etc.)."""

from __future__ import annotations

import sys
from pathlib import Path


def menu_icon_path() -> Path | None:
    """Path to menu bar PNG (22×22), or None if missing."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "excel_archive" / "resources" / "menu_icon.png")
        candidates.append(Path(sys._MEIPASS) / "resources" / "menu_icon.png")
    pkg = Path(__file__).resolve().parent / "resources" / "menu_icon.png"
    candidates.append(pkg)
    repo = Path(__file__).resolve().parents[2] / "resources" / "menu_icon.png"
    candidates.append(repo)
    for p in candidates:
        if p.is_file():
            return p
    return None
