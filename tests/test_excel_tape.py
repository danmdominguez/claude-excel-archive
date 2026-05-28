from pathlib import Path

from excel_archive.render_tape import export_json_to_events, render_tape_markdown

FIXTURES = Path(__file__).parent / "fixtures"


def test_export_fixture_to_markdown():
    import json

    data = json.loads((FIXTURES / "minimal_export.json").read_text(encoding="utf-8"))
    events = export_json_to_events(data)
    md = render_tape_markdown(events, meta=data.get("meta"))
    assert "# " in md
    assert "Turn" in md
    assert "snip" in md.lower() or "Snipped" in md


def test_user_export_if_present():
    export = Path("/Users/dmd/Downloads/dan.m.dominguez-2026-05-28T04-45-18-073Z.json")
    if not export.is_file():
        return
    from excel_archive.render_tape import export_json_to_tape

    out = export_json_to_tape(export, export.parent / "_test_tape_sample.md")
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert len(text) > 10_000
    assert "## Turn" in text
    out.unlink(missing_ok=True)
