from pathlib import Path

import pytest

from excel_archive.decode import extract_strings_from_blob
from excel_archive.idb_extract import ExtractedSession
from excel_archive.merge import diff_export_vs_session, load_export_tools

FIXTURES = Path(__file__).parent / "fixtures"
SCRATCH_SQLITE = (
    Path(__file__).resolve().parents[2]
    / ".scratch/excel-idb/417FFAD840D5D2DF8BBF3C8CD4E6C8A358A788F23641EAD3ED4B9EEFCE5B9D66.sqlite3"
)


def test_load_export_tools_counts_snip():
    _, tool_ids, tools, snip = load_export_tools(FIXTURES / "minimal_export.json")
    assert "toolu_fixture_empty" in tool_ids
    assert tools["toolu_fixture_empty"].input == {}
    assert snip >= 1


def test_diff_against_synthetic_session():
    export = FIXTURES / "minimal_export.json"
    session = ExtractedSession(source=FIXTURES / "chat_blob_head.bin")
    session.strings = extract_strings_from_blob((FIXTURES / "chat_blob_head.bin").read_bytes())
    report = diff_export_vs_session(export, session)
    assert report.export_snip_markers >= 1
    assert "toolu_fixture_empty" in report.export_empty_inputs


@pytest.mark.skipif(not SCRATCH_SQLITE.is_file(), reason="scratch IDB not present")
def test_diff_scratch_idb_overlap():
    from excel_archive.idb_extract import extract_from_sqlite

    export = Path("/Users/dmd/Downloads/dan.m.dominguez-2026-05-28T04-45-18-073Z.json")
    if not export.is_file():
        pytest.skip("user export JSON not on disk")

    session = extract_from_sqlite(SCRATCH_SQLITE)
    report = diff_export_vs_session(export, session)
    assert len(report.export_tool_ids & report.idb_tool_ids) == len(report.export_tool_ids)
    assert len(report.only_in_idb) > 0
    assert report.export_empty_inputs
    assert len(report.recovered_inputs) / len(report.export_empty_inputs) >= 0.5
