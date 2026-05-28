from pathlib import Path

from excel_archive.watch import IdbWatcher


def test_watcher_journal_root_when_workbook_set(tmp_path: Path) -> None:
    wb = tmp_path / "My Book.xlsx"
    wb.write_bytes(b"x")
    watcher = IdbWatcher(
        workbook_path=wb,
        dest_root=tmp_path / "snapshots",
        journal=True,
        session_key="default",
    )
    root = watcher._journal_root()
    assert root is not None
    assert root.name == "journal"
    assert "My_Book.xlsx" in str(root.parent)
