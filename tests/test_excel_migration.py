"""Tests for unsaved → saved workbook archive migration."""

from __future__ import annotations

import json
from pathlib import Path

from excel_archive.active_workbook import ActiveWorkbook
from excel_archive.journal import ingest_chat_blob
from excel_archive.paths import encode_unsaved_workbook, workbook_root_dir
from excel_archive.workbook_migration import (
    get_alias_for_unsaved_name,
    journal_dir_for_workbook_name_resolved,
    migrate_unsaved_to_path,
    try_migrate_on_save_transition,
)


def test_migrate_moves_unsaved_folder(tmp_path: Path) -> None:
    unsaved = "Book3"
    wb_file = tmp_path / "MyModel.xlsx"
    wb_file.write_bytes(b"pk")

    src = tmp_path / encode_unsaved_workbook(unsaved)
    sess = src / "journal" / "live"
    sess.mkdir(parents=True)
    (sess / "events.jsonl").write_text(
        '{"kind":"message","role":"user","text":"hello"}\n',
        encoding="utf-8",
    )

    report = migrate_unsaved_to_path(unsaved, wb_file, archive_root=tmp_path)
    assert report.ok
    assert report.moved
    dest = workbook_root_dir(wb_file, archive_root=tmp_path)
    assert dest.is_dir()
    assert (dest / "journal" / "live" / "events.jsonl").is_file()
    assert not (src / "journal").exists() or not src.exists()

    alias = get_alias_for_unsaved_name("Book3", archive_root=tmp_path)
    assert alias is not None
    assert Path(alias["workbook_path"]) == wb_file.resolve()


def test_alias_routes_journal_after_migration(tmp_path: Path) -> None:
    unsaved = "Book3"
    wb_file = tmp_path / "Saved.xlsx"
    wb_file.write_bytes(b"x")
    src = tmp_path / encode_unsaved_workbook(unsaved)
    (src / "journal" / "default").mkdir(parents=True)
    migrate_unsaved_to_path(unsaved, wb_file, archive_root=tmp_path)

    jdir = journal_dir_for_workbook_name_resolved("Book3", archive_root=tmp_path)
    assert encode_unsaved_workbook("Book3") not in str(jdir)
    assert jdir == workbook_root_dir(wb_file, archive_root=tmp_path) / "journal"


def test_save_transition_detected(tmp_path: Path) -> None:
    unsaved = "Book3"
    wb_file = tmp_path / "out.xlsx"
    wb_file.write_bytes(b"x")
    (tmp_path / encode_unsaved_workbook(unsaved) / "journal").mkdir(parents=True)

    prev = ActiveWorkbook(name="Book3", path=None, saved=False)
    curr = ActiveWorkbook(name="out.xlsx", path=wb_file, saved=True)
    report = try_migrate_on_save_transition(prev, curr, archive_root=tmp_path)
    assert report is not None
    assert report.ok


def test_ingest_after_migration_uses_dest(tmp_path: Path) -> None:
    unsaved = "Book3"
    wb_file = tmp_path / "Model.xlsx"
    wb_file.write_bytes(b"x")
    src = tmp_path / encode_unsaved_workbook(unsaved)
    (src / "journal" / "live").mkdir(parents=True)
    migrate_unsaved_to_path(unsaved, wb_file, archive_root=tmp_path)

    dest_journal = workbook_root_dir(wb_file, archive_root=tmp_path) / "journal"
    n = ingest_chat_blob(
        b'{"type":"text","text":"create a simple table"}',
        journal_root=dest_journal,
        session_key="live",
        snapshot="test",
        workbook_name="Book3",
    )
    assert n >= 0
    # Alias should still route Book3 name to dest
    assert journal_dir_for_workbook_name_resolved("Book3", archive_root=tmp_path) == dest_journal
