from pathlib import Path

from excel_archive.navigation import (
    find_latest_tape,
    is_workbook_archive_root,
    iter_tapes,
    write_archive_root_index,
    write_latest_symlink,
)


def test_is_workbook_archive_root(tmp_path: Path) -> None:
    wb = tmp_path / "_Users_me_Downloads_book.xlsx"
    (wb / "journal").mkdir(parents=True)
    assert is_workbook_archive_root(wb)
    assert not is_workbook_archive_root(tmp_path / "journal")


def test_find_latest_tape_across_workbooks(tmp_path: Path) -> None:
    old = tmp_path / "_Users_old.xlsx" / "journal" / "default"
    old.mkdir(parents=True)
    (old / "session.tape.md").write_text("old", encoding="utf-8")
    (old / "state.json").write_text("{}", encoding="utf-8")

    new = tmp_path / "_Users_new.xlsx" / "journal" / "default"
    new.mkdir(parents=True)
    (new / "session.tape.md").write_text("new", encoding="utf-8")
    (new / "state.json").write_text("{}", encoding="utf-8")

    tape_new = new / "session.tape.md"
    tape_new.write_text("newer", encoding="utf-8")

    latest = find_latest_tape(tmp_path)
    assert latest is not None
    assert latest.tape == tape_new


def test_archive_root_index_and_latest_symlink(tmp_path: Path) -> None:
    sess = tmp_path / "_Users_me_book.xlsx" / "journal" / "default"
    sess.mkdir(parents=True)
    tape = sess / "session.tape.md"
    tape.write_text("# tape", encoding="utf-8")
    (sess / "state.json").write_text("{}", encoding="utf-8")

    write_archive_root_index(tmp_path)
    assert (tmp_path / "index.md").is_file()
    link = tmp_path / "latest.md"
    assert link.is_symlink()
    assert link.resolve() == tape.resolve()

    tapes = iter_tapes(tmp_path)
    assert len(tapes) == 1
    write_latest_symlink(tmp_path, tape)
    assert link.resolve() == tape.resolve()
