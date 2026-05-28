"""Poll Excel WebKit IndexedDB for changes and snapshot copies."""

from __future__ import annotations

import time
from pathlib import Path

from .copy import copy_database, default_snapshots_dir
from .journal import ingest_sqlite
from .paths import IdbDatabasePaths, pick_primary_database, workbook_journal_dir
from .match_workbook import (
    get_remembered_workbook_name,
    infer_workbook_names_from_blob,
    pick_workbook_name,
    remember_workbook_name,
)
from .workbook_copy import WorkbookCopyState, copy_workbook
from .config_excel import load_config_for_workbook
from .retention import enforce_retention_for_workbook_root


class IdbWatcher:
    def __init__(
        self,
        *,
        interval_sec: float = 5.0,
        dest_root: Path | None = None,
        database: IdbDatabasePaths | None = None,
        journal: bool = True,
        session_key: str = "default",
        workbook_name: str | None = None,
        infer_workbook: bool = True,
        workbook_path: Path | None = None,
        copy_workbook_file: bool = False,
        retention: bool = True,
    ) -> None:
        self.interval_sec = interval_sec
        self.dest_root = dest_root or default_snapshots_dir()
        self.database = database
        self.journal = journal
        self.session_key = session_key
        self.workbook_name = workbook_name
        self.infer_workbook = infer_workbook
        self.workbook_path = workbook_path
        self.copy_workbook_file = copy_workbook_file
        self.retention = retention
        self._workbook_copy_state = WorkbookCopyState()
        self._last_signature: tuple[int, int, int] | None = None

    def _journal_root(self) -> Path | None:
        """Per-workbook journal when --workbook is set; else archive-root journal."""
        if self.workbook_path is not None:
            return workbook_journal_dir(self.workbook_path)
        return None

    def _resolve_db(self) -> IdbDatabasePaths | None:
        if self.database and self.database.exists():
            return self.database
        return pick_primary_database()

    @staticmethod
    def _signature(db: IdbDatabasePaths) -> tuple[int, int, int]:
        sql_mtime = int(db.sqlite.stat().st_mtime) if db.sqlite.is_file() else 0
        wal_mtime = int(db.wal.stat().st_mtime) if db.wal and db.wal.is_file() else 0
        wal_size = db.wal.stat().st_size if db.wal and db.wal.is_file() else 0
        return sql_mtime, wal_mtime, wal_size

    def poll_once(self) -> Path | None:
        """Copy DB if WAL/sqlite changed since last poll. Returns snapshot dir or None."""
        db = self._resolve_db()
        if not db:
            return None

        sig = self._signature(db)
        if sig == self._last_signature:
            return None

        self._last_signature = sig
        # If workbook_name wasn't provided, try a remembered mapping (stable across polls).
        wb_name = self.workbook_name
        if wb_name is None and self.infer_workbook:
            wb_name = get_remembered_workbook_name(db)
        path = copy_database(db, self.dest_root, workbook_name=wb_name)

        if self.copy_workbook_file and self.workbook_path is not None:
            try:
                copied = copy_workbook(self.workbook_path, state=self._workbook_copy_state)
                if copied:
                    print(f"workbook: {copied}")
            except Exception:
                pass
        if self.journal:
            sqlite_path = path / "IndexedDB.sqlite3"
            # Ingest may infer workbook name from blob text and update mappings.
            n = ingest_sqlite(
                sqlite_path,
                journal_root=self._journal_root(),
                session_key=self.session_key,
                workbook_name=wb_name,
                idb_origin=db if self.infer_workbook else None,
            )
            if n:
                print(f"journal: +{n} events")
            # Post-ingest inference: update mapping for next snapshot naming.
            if not self.infer_workbook:
                return path
            try:
                # We already copied sqlite, so safe to read now.
                import sqlite3

                conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
                try:
                    row = conn.execute(
                        "SELECT value FROM Records WHERE objectStoreID = 1634 ORDER BY length(value) DESC LIMIT 1"
                    ).fetchone()
                    blob = row[0] if row and row[0] else None
                finally:
                    conn.close()
                if blob:
                    counts = infer_workbook_names_from_blob(blob)
                    picked = pick_workbook_name(counts)
                    if picked:
                        remember_workbook_name(db, picked.workbook_name, reason="inferred_from_blob")
            except Exception:
                # Best-effort only; watcher must not crash.
                pass

        # Apply retention after successful poll, if we know the workbook root.
        if self.retention and self.workbook_path is not None:
            try:
                from .paths import workbook_root_dir

                wb_root = workbook_root_dir(self.workbook_path)
                cfg = load_config_for_workbook(self.workbook_path).retention
                report = enforce_retention_for_workbook_root(wb_root, cfg=cfg, dry_run=False)
                if report.deleted_files or report.deleted_dirs:
                    print(f"retention: deleted_files={report.deleted_files} deleted_dirs={report.deleted_dirs}")
            except Exception:
                pass
        return path

    def run_forever(self) -> None:
        while True:
            path = self.poll_once()
            if path:
                print(f"snapshot: {path}")
            time.sleep(self.interval_sec)
