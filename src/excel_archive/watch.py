"""Poll Excel WebKit IndexedDB for changes and snapshot copies."""

from __future__ import annotations

import time
from pathlib import Path

from .active_workbook import ActiveWorkbook, resolve_active_workbook
from .copy import (
    SnapshotStyle,
    copy_database,
    copy_database_rolling,
    default_snapshots_dir,
)
from .journal import ingest_sqlite
from .paths import (
    IdbDatabasePaths,
    archive_forensic_live_dir,
    default_archive_root,
    pick_database_for_active_workbook,
    workbook_forensic_live_dir,
    workbook_root_dir,
)
from .match_workbook import get_remembered_workbook_name
from .workbook_copy import WorkbookCopyState, copy_workbook
from .config_excel import load_config_for_workbook
from .retention import enforce_retention_for_workbook_root
from .watch_lock import WatchLock, default_watch_lock_path
from .workbook_migration import try_migrate_on_save_transition


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
        snapshot_style: SnapshotStyle = "rolling",
        enforce_single_watcher: bool = True,
        use_active_workbook: bool = True,
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
        self.snapshot_style = snapshot_style
        self.enforce_single_watcher = enforce_single_watcher
        self.use_active_workbook = use_active_workbook
        self._workbook_copy_state = WorkbookCopyState()
        self._last_signature: tuple[int, int, int] | None = None
        self._watch_lock: WatchLock | None = None
        self._active: ActiveWorkbook | None = None
        self._last_active: ActiveWorkbook | None = None
        self._idb_warning: str | None = None

    def _forensic_live_dir(self) -> Path:
        if self.workbook_path is not None:
            return workbook_forensic_live_dir(self.workbook_path)
        return archive_forensic_live_dir()

    def _resolve_active(self) -> ActiveWorkbook | None:
        if not self.use_active_workbook:
            return None
        if self.workbook_path is not None:
            return ActiveWorkbook(
                name=self.workbook_path.name,
                path=self.workbook_path,
                saved=True,
            )
        return resolve_active_workbook()

    def _resolve_db(self) -> IdbDatabasePaths | None:
        if self.database and self.database.exists():
            return self.database
        active_name = self._active.name if self._active else None
        db, warning = pick_database_for_active_workbook(active_name)
        self._idb_warning = warning
        return db

    @staticmethod
    def _signature(db: IdbDatabasePaths) -> tuple[int, int, int]:
        sql_mtime = int(db.sqlite.stat().st_mtime) if db.sqlite.is_file() else 0
        wal_mtime = int(db.wal.stat().st_mtime) if db.wal and db.wal.is_file() else 0
        wal_size = db.wal.stat().st_size if db.wal and db.wal.is_file() else 0
        return sql_mtime, wal_mtime, wal_size

    def _capture_sqlite(self, db: IdbDatabasePaths, wb_name: str | None) -> tuple[Path, Path]:
        """
        Returns (report_path, sqlite_path_for_ingest).

        rolling: overwrite forensic/live/ (default)
        per-poll: legacy snapshots/<YYYYMMDD_HHMM>_…_snapshot/ folders
        off: ingest directly from Excel IDB (no local copy)
        """
        if self.snapshot_style == "per-poll":
            out = copy_database(db, self.dest_root, workbook_name=wb_name)
            return out, out / "IndexedDB.sqlite3"
        if self.snapshot_style == "rolling":
            live = copy_database_rolling(db, self._forensic_live_dir())
            return live, live / "IndexedDB.sqlite3"
        return db.sqlite.parent, db.sqlite

    def poll_once(self) -> Path | None:
        """On IDB change: optional forensic copy + append journal. Returns capture path or None."""
        self._active = self._resolve_active()
        mig = try_migrate_on_save_transition(
            self._last_active,
            self._active,
            archive_root=default_archive_root(),
        )
        if mig and mig.ok:
            print(f"migration: {mig.unsaved_name} → {mig.dest_root.name} ({'; '.join(mig.notes[:3])})")
        self._last_active = self._active
        db = self._resolve_db()
        if not db:
            return None

        if self._idb_warning:
            print(f"warning: {self._idb_warning}")

        sig = self._signature(db)
        if sig == self._last_signature:
            return None

        self._last_signature = sig
        wb_name = self.workbook_name
        if wb_name is None and self._active:
            wb_name = self._active.name
        if wb_name is None and self.infer_workbook:
            wb_name = get_remembered_workbook_name(db)

        report_path, sqlite_path = self._capture_sqlite(db, wb_name)

        copy_path = self.workbook_path
        if copy_path is None and self._active and self._active.saved and self._active.path:
            copy_path = self._active.path

        if self.copy_workbook_file:
            if copy_path is None:
                if self._active and not self._active.saved:
                    print(f"workbook copy skipped: {self._active.name} is unsaved (no path)")
            else:
                try:
                    copied = copy_workbook(copy_path, state=self._workbook_copy_state)
                    if copied:
                        print(f"workbook: {copied}")
                except FileNotFoundError as exc:
                    print(f"workbook copy skipped: {exc}")
                except Exception:
                    pass

        if self.journal:
            n = ingest_sqlite(
                sqlite_path,
                journal_root=None,
                session_key=self.session_key,
                workbook_name=wb_name,
                idb_origin=db if self.infer_workbook else None,
                archive_root=default_archive_root(),
                fan_out=True,
            )
            if n:
                print(f"journal: +{n} events (multi-workbook fan-out)")

        if self.retention and copy_path is not None:
            try:
                wb_root = workbook_root_dir(copy_path)
                cfg = load_config_for_workbook(copy_path).retention
                report = enforce_retention_for_workbook_root(wb_root, cfg=cfg, dry_run=False)
                if report.deleted_files or report.deleted_dirs:
                    print(
                        f"retention: deleted_files={report.deleted_files} "
                        f"deleted_dirs={report.deleted_dirs}"
                    )
            except Exception:
                pass

        return report_path

    def run_forever(self) -> None:
        archive_root = default_archive_root()
        lock = WatchLock(default_watch_lock_path(archive_root))
        if self.enforce_single_watcher and not lock.acquire():
            holder = lock.holder_pid()
            msg = f"Another excel-archive watch is already running (pid {holder})."
            raise RuntimeError(msg)

        self._watch_lock = lock
        if self._active is None:
            self._active = self._resolve_active()
        self._last_active = self._active
        if self._active:
            print(f"Active workbook: {self._active.display}")
        print(
            "Note: --workbook is for .xlsx copies and forensic paths only; "
            "journal fan-out uses initial_state.fileName per chat record."
        )
        try:
            while True:
                path = self.poll_once()
                if path:
                    if self.snapshot_style == "rolling":
                        print(f"forensic/live: {path}")
                    elif self.snapshot_style == "per-poll":
                        print(f"snapshot: {path}")
                    else:
                        print("journal: updated")
                time.sleep(self.interval_sec)
        finally:
            lock.release()
