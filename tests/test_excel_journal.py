from pathlib import Path

import pytest

from excel_archive.journal import JournalState, extract_events_from_chat_blob, merge_events

FIXTURES = Path(__file__).parent / "fixtures"
SCRATCH_SQLITE = (
    Path(__file__).resolve().parents[2]
    / ".scratch/excel-idb/417FFAD840D5D2DF8BBF3C8CD4E6C8A358A788F23641EAD3ED4B9EEFCE5B9D66.sqlite3"
)


def test_merge_events_prefers_first_full_tool_use():
    state = JournalState()
    full = {"kind": "tool_use", "id": "toolu_a", "name": "get_cell_ranges", "input": {"ranges": ["A1"]}}
    empty = {"kind": "tool_use", "id": "toolu_a", "name": "get_cell_ranges", "input": {}}
    new1 = merge_events(state, [full], snapshot="s1")
    new2 = merge_events(state, [empty], snapshot="s2")
    assert len(new1) == 1
    assert len(new2) == 0
    assert state.tool_uses["toolu_a"]["input"] == {"ranges": ["A1"]}


def test_merge_events_ignores_snipped_tool_result():
    state = JournalState()
    snipped = {
        "kind": "tool_result",
        "tool_use_id": "toolu_x",
        "content": "[snipped — context_snip applied]",
    }
    assert merge_events(state, [snipped], snapshot="s1") == []


@pytest.mark.skipif(not SCRATCH_SQLITE.is_file(), reason="scratch IDB not present")
def test_extract_events_from_scratch_blob():
    from excel_archive.journal import ingest_sqlite
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        n = ingest_sqlite(SCRATCH_SQLITE, journal_root=Path(tmp), session_key="t")
        assert n > 100
        state = JournalState.load(Path(tmp) / "t" / "state.json")
        assert len(state.tool_uses) > 50
        assert len(state.tool_results) > 50


def test_extract_from_fixture_blob():
    data = (FIXTURES / "chat_blob_head.bin").read_bytes()
    events = extract_events_from_chat_blob(data)
    # Head slice may lack parseable tool_use blocks; should not crash.
    assert isinstance(events, list)
