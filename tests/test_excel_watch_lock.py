from pathlib import Path

from excel_archive.watch_lock import WatchLock, default_watch_lock_path


def test_watch_lock_exclusive(tmp_path: Path) -> None:
    path = default_watch_lock_path(tmp_path)
    lock1 = WatchLock(path)
    lock2 = WatchLock(path)
    assert lock1.acquire() is True
    assert lock2.acquire() is False
    assert lock2.holder_pid() == lock1.holder_pid()
    lock1.release()
    assert lock2.acquire() is True
    lock2.release()
