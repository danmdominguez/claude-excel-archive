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


def encode_unsaved_workbook(name: str) -> str:
    """Folder name for unsaved workbooks (e.g. Book3) with no filesystem path."""
    safe = _SANITIZE_SEGMENT_RE.sub("_", name.strip())
    return f"_unsaved_{safe}"


def encode_workbook_filename(filename: str) -> str:
    """Folder name when only a workbook filename is known (no full path)."""
    safe = _SANITIZE_SEGMENT_RE.sub("_", filename.strip())
    return f"_workbook_{safe}"


def workbook_root_for_name(workbook_name: str, *, archive_root: Path | None = None) -> Path:
    """
    Archive folder for a workbook identity from IndexedDB (initial_state.fileName).

    - Unsaved names (Book3) → `_unsaved_Book3/`
    - Saved filenames (*.xlsx) → `_workbook_EF_Shop_Model_DD.xlsx/`
    """
    root = archive_root or default_archive_root()
    name = (workbook_name or "").strip()
    if not name:
        return root / "_unknown_workbook"
    lower = name.lower()
    if lower.endswith(".xlsx") or lower.endswith(".xlsm") or lower.endswith(".xls"):
        return root / encode_workbook_filename(name)
    return root / encode_unsaved_workbook(name)


def journal_dir_for_workbook_name(
    workbook_name: str,
    *,
    archive_root: Path | None = None,
) -> Path:
    return workbook_root_for_name(workbook_name, archive_root=archive_root) / "journal"


def workbook_forensic_live_dir(workbook_path: Path, *, archive_root: Path | None = None) -> Path:
    return workbook_root_dir(workbook_path, archive_root=archive_root) / "forensic" / "live"


def workbook_forensic_history_dir(workbook_path: Path, *, archive_root: Path | None = None) -> Path:
    return workbook_root_dir(workbook_path, archive_root=archive_root) / "forensic" / "history"


def archive_forensic_live_dir(archive_root: Path | None = None) -> Path:
    base = archive_root or default_archive_root()
    return base / "forensic" / "live"


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


def database_has_chats_store(db: IdbDatabasePaths) -> bool:
    """True if this IndexedDB contains a `chats` object store with at least one row."""
    import sqlite3

    if not db.sqlite.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{db.sqlite}?mode=ro", uri=True)
        try:
            stores = {
                str(r[1]).lower(): int(r[0])
                for r in conn.execute("SELECT id, name FROM ObjectStoreInfo")
            }
            chat_id = stores.get("chats", STORE_CHATS)
            row = conn.execute(
                "SELECT 1 FROM Records WHERE objectStoreID = ? LIMIT 1",
                (chat_id,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def pick_database_for_active_workbook(
    active_workbook_name: str | None,
    databases: list[IdbDatabasePaths] | None = None,
) -> tuple[IdbDatabasePaths | None, str | None]:
    """
    Choose IndexedDB to watch/ingest.

    Prefers databases with a `chats` store, then most recently modified WAL.
    When active_workbook_name is set, prefer a DB whose chat records mention that name.
    Returns (database, warning_message).
    """
    dbs = databases if databases is not None else discover_indexeddb_databases()
    chat_dbs = [db for db in dbs if database_has_chats_store(db)]
    if not chat_dbs:
        primary = pick_primary_database(dbs)
        if primary:
            return primary, "No IndexedDB with chats store found; using largest WAL database."
        return None, "No Excel IndexedDB databases found."

    if not active_workbook_name:
        return _pick_most_recent_chat_db(chat_dbs), None

    from .journal import iter_chat_records, resolve_record_workbook
    import sqlite3

    name_lower = active_workbook_name.strip().lower()
    matches: list[tuple[int, IdbDatabasePaths]] = []
    for db in chat_dbs:
        try:
            conn = sqlite3.connect(f"file:{db.sqlite}?mode=ro", uri=True)
            try:
                store_names = {
                    int(r[0]): str(r[1]) for r in conn.execute("SELECT id, name FROM ObjectStoreInfo")
                }
                for rec in iter_chat_records(conn, store_names)[:8]:
                    wb = resolve_record_workbook(rec.data).lower()
                    if wb == name_lower or name_lower in wb:
                        wal_mtime = int(db.wal.stat().st_mtime) if db.wal and db.wal.is_file() else 0
                        matches.append((wal_mtime, db))
                        break
            finally:
                conn.close()
        except sqlite3.Error:
            continue

    if matches:
        matches.sort(key=lambda x: x[0], reverse=True)
        return matches[0][1], None

    chosen = _pick_most_recent_chat_db(chat_dbs)
    warn = (
        f"Active workbook '{active_workbook_name}' not found in any chats store; "
        f"using most recent chat DB. Save the workbook or run a separate Excel profile "
        f"if sessions may mix."
    )
    return chosen, warn


def _pick_most_recent_chat_db(dbs: list[IdbDatabasePaths]) -> IdbDatabasePaths:
    def mtime_score(db: IdbDatabasePaths) -> tuple[int, int]:
        wal_mtime = int(db.wal.stat().st_mtime) if db.wal and db.wal.is_file() else 0
        sql_mtime = int(db.sqlite.stat().st_mtime) if db.sqlite.is_file() else 0
        sql_size = db.sqlite.stat().st_size if db.sqlite.is_file() else 0
        return wal_mtime, sql_size

    return max(dbs, key=mtime_score)
