"""Discover Claude for Excel IndexedDB files on macOS."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path
import re

EXCEL_CONTAINER = Path.home() / "Library/Containers/com.microsoft.Excel/Data"
WEBKIT_WEBSITE_DATA = EXCEL_CONTAINER / "Library/WebKit/WebsiteData/Default"

DEFAULT_ARCHIVE_ROOT = Path.home() / "Documents/ExcelArchive"

# Object store names from claude-chat-history schema v11 (bundle).
STORE_CHATS = 1634
STORE_BLOBS = 1635
STORE_RESULTS = "results"  # resolved by name when present


@dataclass(frozen=True)
class IdbDatabasePaths:
    """Paths to one IndexedDB SQLite database (+ WAL sidecars)."""

    sqlite: Path
    wal: Path | None
    shm: Path | None
    site_folder: Path

    def exists(self) -> bool:
        return self.sqlite.is_file()

    def sidecar_paths(self) -> list[Path]:
        out = [self.sqlite]
        if self.wal and self.wal.is_file():
            out.append(self.wal)
        if self.shm and self.shm.is_file():
            out.append(self.shm)
        return out


def default_archive_root() -> Path:
    env = os.environ.get("EXCEL_ARCHIVE_ROOT")
    return Path(env).expanduser() if env else DEFAULT_ARCHIVE_ROOT


_SANITIZE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


def encode_workbook_path(workbook_path: Path) -> str:
    """
    Encode a workbook path into a folder-safe name.

    User convention: underscores instead of '/' so it is filesystem-safe and grepable:
    `~/Documents/ExcelArchive/_Users_name_path_File.xlsx/`
    """
    p = workbook_path.expanduser().resolve()
    encoded = "_" + str(p).lstrip("/").replace("/", "_")
    encoded = _SANITIZE_SEGMENT_RE.sub("_", encoded)
    return encoded


def workbook_root_dir(workbook_path: Path, *, archive_root: Path | None = None) -> Path:
    root = archive_root or default_archive_root()
    return root / encode_workbook_path(workbook_path)


def workbook_snapshots_dir(workbook_path: Path, *, archive_root: Path | None = None) -> Path:
    return workbook_root_dir(workbook_path, archive_root=archive_root) / "snapshots"


def workbook_journal_dir(workbook_path: Path, *, archive_root: Path | None = None) -> Path:
    return workbook_root_dir(workbook_path, archive_root=archive_root) / "journal"


def discover_indexeddb_databases() -> list[IdbDatabasePaths]:
    """Find IndexedDB.sqlite3 files under Excel WebKit WebsiteData."""
    if platform.system() != "Darwin":
        return []

    if not WEBKIT_WEBSITE_DATA.is_dir():
        return []

    seen: set[Path] = set()
    found: list[IdbDatabasePaths] = []

    for sqlite in WEBKIT_WEBSITE_DATA.glob("**/IndexedDB/*/IndexedDB.sqlite3"):
        if sqlite in seen:
            continue
        seen.add(sqlite)
        db_dir = sqlite.parent
        wal = db_dir / "IndexedDB.sqlite3-wal"
        shm = db_dir / "IndexedDB.sqlite3-shm"
        site_folder = db_dir
        for _ in range(8):
            if site_folder.parent == WEBKIT_WEBSITE_DATA:
                break
            site_folder = site_folder.parent
        found.append(
            IdbDatabasePaths(
                sqlite=sqlite,
                wal=wal if wal.is_file() else None,
                shm=shm if shm.is_file() else None,
                site_folder=site_folder,
            )
        )

    return found


def pick_primary_database(databases: list[IdbDatabasePaths] | None = None) -> IdbDatabasePaths | None:
    """Prefer the database with the largest WAL (most active chat history)."""
    dbs = databases if databases is not None else discover_indexeddb_databases()
    if not dbs:
        return None

    def score(db: IdbDatabasePaths) -> int:
        wal_size = db.wal.stat().st_size if db.wal and db.wal.is_file() else 0
        sql_size = db.sqlite.stat().st_size if db.sqlite.is_file() else 0
        return wal_size * 10 + sql_size

    return max(dbs, key=score)
