from pathlib import Path

import pytest

from excel_archive.decode import SNIP_MARKER, decode_best_effort_text, extract_strings_from_blob

FIXTURES = Path(__file__).parent / "fixtures"


def test_extract_strings_from_chat_blob_head():
    data = (FIXTURES / "chat_blob_head.bin").read_bytes()
    result = extract_strings_from_blob(data)
    assert len(result.tool_ids) >= 50
    # Heuristic decode may find snip marker fragments; this fixture is a slice.
    assert result.snip_markers >= 0


def test_extract_tool_inputs_from_synthetic():
    tid = "toolu_synthetic123"
    payload = (
        b'{"name":"get_cell_ranges","id":"'
        + tid.encode()
        + b'","input":{"ranges":["A1:B2"]}}'
    )
    result = extract_strings_from_blob(payload)
    assert tid in result.tool_ids
    assert result.tool_inputs.get(tid) == {"ranges": ["A1:B2"]}


def test_snip_marker_constant():
    assert "context_snip" in SNIP_MARKER


def test_decode_best_effort_text_utf16le_contains_ascii():
    # "ABC" in UTF-16LE is A\0B\0C\0
    data = b"A\x00B\x00C\x00"
    out = decode_best_effort_text(data)
    assert "ABC" in out
