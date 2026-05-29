"""Multi-chat IndexedDB ingest (per-record fan-out, Book3 / initial_state)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from excel_archive.journal import (
    ChatRecord,
    iter_chat_records,
    parse_initial_state,
    resolve_record_workbook,
    ingest_sqlite,
)
from excel_archive.paths import (
    encode_unsaved_workbook,
    encode_workbook_filename,
    journal_dir_for_workbook_name,
)

SCRATCH_SQLITE = (
    Path(__file__).resolve().parents[2]
    / ".scratch/excel-idb/417FFAD840D5D2DF8BBF3C8CD4E6C8A358A788F23641EAD3ED4B9EEFCE5B9D66.sqlite3"
)

EXPORT_JSON = Path("/Users/dmd/Downloads/dan.m.dominguez-2026-05-29T00-34-33-745Z.json")

INITIAL_STATE_SNIPPET = """<initial_state>
{
  "success": true,
  "fileName": "Book3",
  "sheetsMetadata": [{"id": 1, "name": "Sheet1"}],
  "totalSheets": 1
}
</initial_state>"""


def test_parse_initial_state_from_export_shape() -> None:
    parsed = parse_initial_state(INITIAL_STATE_SNIPPET)
    assert parsed is not None
    assert parsed["fileName"] == "Book3"
    assert parsed["totalSheets"] == 1


def test_encode_unsaved_and_workbook_paths() -> None:
    assert encode_unsaved_workbook("Book3") == "_unsaved_Book3"
    assert "EF_Shop" in encode_workbook_filename("EF Shop Model DD.xlsx")
    root = journal_dir_for_workbook_name("Book3", archive_root=Path("/tmp/a"))
    assert root == Path("/tmp/a/_unsaved_Book3/journal")


@pytest.mark.skipif(not SCRATCH_SQLITE.is_file(), reason="scratch IDB not present")
def test_iter_chat_records_multiple() -> None:
    import sqlite3

    conn = sqlite3.connect(f"file:{SCRATCH_SQLITE}?mode=ro", uri=True)
    try:
        store_names = {
            int(r[0]): str(r[1]) for r in conn.execute("SELECT id, name FROM ObjectStoreInfo")
        }
        records = iter_chat_records(conn, store_names)
    finally:
        conn.close()
    assert len(records) >= 2
    assert all(isinstance(r, ChatRecord) for r in records)
    sizes = sorted((r.size for r in records), reverse=True)
    assert sizes[0] > sizes[-1]


@pytest.mark.skipif(not SCRATCH_SQLITE.is_file(), reason="scratch IDB not present")
def test_multi_ingest_creates_unsaved_book3_journal(tmp_path: Path) -> None:
    n = ingest_sqlite(
        SCRATCH_SQLITE,
        archive_root=tmp_path,
        session_key="t",
        fan_out=True,
    )
    assert n > 0
    book3_dir = tmp_path / "_unsaved_Book3" / "journal" / "t"
    assert book3_dir.is_dir()
    jsonl = book3_dir / "events.jsonl"
    assert jsonl.is_file()
    text = jsonl.read_text(encoding="utf-8")
    assert "Book3" in text or "bar graph" in text


@pytest.mark.skipif(not EXPORT_JSON.is_file(), reason="export JSON not on disk")
@pytest.mark.skipif(not SCRATCH_SQLITE.is_file(), reason="scratch IDB not present")
def test_book3_journal_matches_export_turns(tmp_path: Path) -> None:
    export = json.loads(EXPORT_JSON.read_text(encoding="utf-8"))
    export_tools = [
        b["id"]
        for msg in export["messages"]
        for b in msg.get("content") or []
        if b.get("type") == "tool_use"
    ]
    ingest_sqlite(SCRATCH_SQLITE, archive_root=tmp_path, session_key="live", fan_out=True)
    jsonl = tmp_path / "_unsaved_Book3" / "journal" / "live" / "events.jsonl"
    assert jsonl.is_file(), "expected Book3 fan-out journal"
    lines = [json.loads(ln) for ln in jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
    blob_text = jsonl.read_text(encoding="utf-8")
    assert "create a simple table" in blob_text or any(
        "bar graph" in (ev.get("text") or "") for ev in lines if ev.get("kind") == "message"
    )
    assert "<initial_state>" in blob_text or any(
        ev.get("initial_state", {}).get("fileName") == "Book3"
        for ev in lines
        if ev.get("kind") == "message"
    )
    tool_ids = {ev["id"] for ev in lines if ev.get("kind") == "tool_use"}
    tool_names = {ev["name"] for ev in lines if ev.get("kind") == "tool_use"}
    for tid in export_tools:
        assert tid in tool_ids, f"missing export tool {tid}"
    assert "set_cell_range" in tool_names
    assert "execute_office_js" in tool_names
    assert any("excel-4dbb55" in (ev.get("text") or "") for ev in lines if ev.get("kind") == "message")
