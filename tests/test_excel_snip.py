from excel_archive.snip import (
    extract_snip_notes_from_blob,
    is_snipped_content,
    parse_snip_input,
)


def test_parse_snip_input():
    inp = {
        "from_id": "id:abc",
        "to_id": "id:xyz",
        "summary": "Compressed diagnostic block",
    }
    parsed = parse_snip_input(inp)
    assert parsed is not None
    assert parsed["from_id"] == "id:abc"
    assert parsed["summary"].startswith("Compressed")


def test_is_snipped_content():
    assert is_snipped_content("[snipped — context_snip applied]")
    assert not is_snipped_content('{"success":true}')


def test_extract_snip_notes_from_synthetic():
    payload = (
        b'{"type":"tool_use","id":"toolu_snip1","name":"context_snip",'
        b'"input":{"from_id":"id:a","to_id":"id:b","summary":"Will drop old reads"}}'
    )
    notes = extract_snip_notes_from_blob(payload)
    assert any(n["kind"] == "snip_note" and n["from_id"] == "id:a" for n in notes)
