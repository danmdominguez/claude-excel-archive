"""Tests for EF/GP workbook attribution and analyze-session."""

from __future__ import annotations

import json
from pathlib import Path

from excel_archive.workbook_attribution import (
    analyze_session_events,
    annotate_events,
    attribute_event,
    build_agent_registry,
    parse_connected_peers,
    workbook_family,
)


PEERS_BLOCK = """<connected_peers>
- excel (agent_id: "excel-94cefc"): EF Shop Model DD alt.xlsx
- excel (agent_id: "excel-4dbb55"): GP Model - Complete -- EXPANDED vF.xlsx
</connected_peers>"""


def test_parse_connected_peers() -> None:
    agents = parse_connected_peers(PEERS_BLOCK)
    assert agents["excel-94cefc"] == "EF Shop Model DD alt.xlsx"
    assert "GP Model" in agents["excel-4dbb55"]


def test_workbook_family() -> None:
    assert workbook_family("EF Shop Model DD.xlsx") == "ef"
    assert workbook_family("GP Model - Complete -- EXPANDED vF.xlsx") == "gp"
    assert workbook_family("Book3") is None


def test_send_message_attributes_to_peer_gp() -> None:
    events = [
        {"kind": "message", "text": PEERS_BLOCK, "message_class": "peers"},
        {
            "kind": "tool_use",
            "name": "send_message",
            "input": {"agent_id": "excel-4dbb55", "message": "read Production"},
        },
    ]
    registry = build_agent_registry(events)
    attr = attribute_event(
        events[1],
        registry=registry,
        local_workbook="EF Shop Model DD.xlsx",
    )
    assert attr.workbook_hint == "gp"
    assert attr.lane == "peer"
    assert attr.target_agent_id == "excel-4dbb55"


def test_annotate_events_writes_workbook_hint() -> None:
    events = [
        {"kind": "message", "text": PEERS_BLOCK},
        {
            "kind": "tool_use",
            "name": "get_cell_ranges",
            "input": {"sheetName": "Production", "ranges": ["A1"]},
        },
    ]
    annotated = annotate_events(events, local_workbook="EF Shop Model DD.xlsx")
    assert annotated[0]["workbook_hint"] == "peer"
    assert "workbook_hint" in annotated[1]
    assert annotated[1]["lane"] in ("local", "unattributed")


def test_analyze_session_counts(tmp_path: Path) -> None:
    events = annotate_events(
        [
            {"kind": "message", "text": PEERS_BLOCK},
            {
                "kind": "tool_use",
                "name": "send_message",
                "input": {"agent_id": "excel-4dbb55"},
            },
            {
                "kind": "tool_use",
                "name": "set_cell_range",
                "input": {"sheetName": "Valuation"},
            },
        ],
        local_workbook="EF Shop Model DD.xlsx",
    )
    jsonl = tmp_path / "events.jsonl"
    with jsonl.open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")

    analysis = analyze_session_events(events, local_workbook="EF Shop Model DD.xlsx")
    assert analysis.event_count == 3
    assert analysis.registry.agents["excel-4dbb55"].startswith("GP Model")
    assert analysis.hint_counts.get("gp", 0) >= 1
    assert analysis.send_message_timeline
