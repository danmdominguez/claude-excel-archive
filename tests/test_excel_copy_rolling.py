from pathlib import Path

from excel_archive.copy import copy_database_checkpoint, copy_database_rolling
from excel_archive.paths import IdbDatabasePaths


def test_rolling_overwrites_live_dir(tmp_path: Path) -> None:
    src_sqlite = tmp_path / "src" / "IndexedDB.sqlite3"
    src_sqlite.parent.mkdir(parents=True)
    src_sqlite.write_bytes(b"version-one")
    db = IdbDatabasePaths(sqlite=src_sqlite, wal=None, shm=None, site_folder=src_sqlite.parent)

    live = tmp_path / "forensic" / "live"
    copy_database_rolling(db, live)
    assert (live / "IndexedDB.sqlite3").read_bytes() == b"version-one"

    src_sqlite.write_bytes(b"version-two")
    copy_database_rolling(db, live)
    assert (live / "IndexedDB.sqlite3").read_bytes() == b"version-two"
    assert not any(live.parent.glob("*_snapshot"))


def test_checkpoint_is_flat_file(tmp_path: Path) -> None:
    src_sqlite = tmp_path / "IndexedDB.sqlite3"
    src_sqlite.write_bytes(b"db")
    db = IdbDatabasePaths(sqlite=src_sqlite, wal=None, shm=None, site_folder=tmp_path)

    history = tmp_path / "forensic" / "history"
    out = copy_database_checkpoint(db, history, workbook_name="Book.xlsx")
    assert out.parent == history
    assert out.name.endswith("_Book.xlsx_IndexedDB.sqlite3")
    assert out.is_file()
