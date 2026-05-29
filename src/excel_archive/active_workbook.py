"""Resolve the active Excel workbook name/path via AppleScript (macOS)."""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ActiveWorkbook:
    """Active workbook in Microsoft Excel."""

    name: str
    path: Path | None
    saved: bool

    @property
    def display(self) -> str:
        if self.saved and self.path:
            return f"{self.name} ({self.path})"
        return f"{self.name} (unsaved)"


def resolve_active_workbook() -> ActiveWorkbook | None:
    """
    Return the front Excel workbook, or None if Excel is not running / script fails.

    Unsaved workbooks have a name but no filesystem path.
    """
    if platform.system() != "Darwin":
        return None

    script = """
    tell application "Microsoft Excel"
        if (count of workbooks) = 0 then return ""
        set wb to active workbook
        set wbName to name of wb
        try
            set wbPath to path of wb
            if wbPath is missing value then
                return wbName & tab & "UNSAVED"
            end if
            return wbName & tab & wbPath
        on error
            return wbName & tab & "UNSAVED"
        end try
    end tell
    """
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    line = (out.stdout or "").strip()
    if not line or out.returncode != 0:
        return None

    if "\t" in line:
        name, path_part = line.split("\t", 1)
        name = name.strip()
        path_part = path_part.strip()
        if path_part.upper() == "UNSAVED" or not path_part:
            return ActiveWorkbook(name=name, path=None, saved=False)
        return ActiveWorkbook(name=name, path=Path(path_part).expanduser(), saved=True)

    return ActiveWorkbook(name=line, path=None, saved=False)
