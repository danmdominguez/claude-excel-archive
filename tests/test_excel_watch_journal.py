from pathlib import Path

from excel_archive.paths import encode_workbook_path, workbook_forensic_live_dir
from excel_archive.watch import IdbWatcher


def test_watcher_forensic_when_workbook_set(tmp_path: Path) -> None:
    wb = tmp_path / "My Book.xlsx"
    wb.write_bytes(b"x")
    watcher = IdbWatcher(
        workbook_path=wb,
        dest_root=tmp_path / "snapshots",
        journal=True,
        session_key="default",
    )
    live = watcher._forensic_live_dir()
    assert live.name == "live"
    assert live.parent.name == "forensic"
    assert encode_workbook_path(wb) in str(live)
    assert live == workbook_forensic_live_dir(wb)
