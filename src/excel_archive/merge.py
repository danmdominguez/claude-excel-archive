"""Diff user export JSON against IndexedDB extract."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .decode import SNIP_MARKER
from .idb_extract import ExtractedSession


@dataclass
class ExportToolUse:
    tool_id: str
    name: str
    input: dict[str, Any]


@dataclass
class DiffReport:
    export_path: Path
    idb_source: Path
    export_tool_ids: set[str] = field(default_factory=set)
    idb_tool_ids: set[str] = field(default_factory=set)
    only_in_export: set[str] = field(default_factory=set)
    only_in_idb: set[str] = field(default_factory=set)
    export_empty_inputs: dict[str, str] = field(default_factory=dict)  # id -> tool name
    recovered_inputs: dict[str, dict] = field(default_factory=dict)
    export_snip_markers: int = 0
    idb_snip_markers: int = 0

    def to_dict(self) -> dict:
        return {
            "export": str(self.export_path),
            "idb": str(self.idb_source),
            "export_tool_count": len(self.export_tool_ids),
            "idb_tool_count": len(self.idb_tool_ids),
            "overlap": len(self.export_tool_ids & self.idb_tool_ids),
            "only_in_export": len(self.only_in_export),
            "only_in_idb": len(self.only_in_idb),
            "export_empty_input_count": len(self.export_empty_inputs),
            "recovered_input_count": len(self.recovered_inputs),
            "recovery_rate": (
                len(self.recovered_inputs) / len(self.export_empty_inputs)
                if self.export_empty_inputs
                else 1.0
            ),
            "export_snip_markers": self.export_snip_markers,
            "idb_snip_markers": self.idb_snip_markers,
            "idb_has_snip_archive": getattr(self, "_idb_has_snip_archive", False),
        }


def load_export_tools(export_path: Path) -> tuple[list[dict], set[str], dict[str, ExportToolUse], int]:
    data = json.loads(export_path.read_text(encoding="utf-8"))
    messages = data.get("messages") or []
    tool_ids: set[str] = set()
    tools: dict[str, ExportToolUse] = {}
    snip_count = 0

    def walk(blocks: Any) -> None:
        nonlocal snip_count
        if isinstance(blocks, str):
            if SNIP_MARKER in blocks:
                snip_count += blocks.count(SNIP_MARKER)
            return
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text") or block.get("content") or ""
                if isinstance(text, str) and SNIP_MARKER in text:
                    snip_count += text.count(SNIP_MARKER)
            if block.get("type") == "tool_use":
                tid = block.get("id", "")
                if tid:
                    tool_ids.add(tid)
                    tools[tid] = ExportToolUse(
                        tool_id=tid,
                        name=str(block.get("name", "")),
                        input=block.get("input") if isinstance(block.get("input"), dict) else {},
                    )
            content = block.get("content")
            if content is not None:
                walk(content)

    for msg in messages:
        walk(msg.get("content"))

    return messages, tool_ids, tools, snip_count


def diff_export_vs_session(export_path: Path, session: ExtractedSession) -> DiffReport:
    _, export_ids, export_tools, export_snip = load_export_tools(export_path)
    report = DiffReport(
        export_path=export_path,
        idb_source=session.source,
        export_tool_ids=export_ids,
        idb_tool_ids=session.strings.tool_ids,
        only_in_export=export_ids - session.strings.tool_ids,
        only_in_idb=session.strings.tool_ids - export_ids,
        export_snip_markers=export_snip,
        idb_snip_markers=session.strings.snip_markers,
    )
    report._idb_has_snip_archive = session.strings.has_snip_archive  # type: ignore[attr-defined]

    for tid, tool in export_tools.items():
        if not tool.input or tool.input == {}:
            report.export_empty_inputs[tid] = tool.name
            recovered = session.strings.tool_inputs.get(tid)
            if recovered:
                report.recovered_inputs[tid] = recovered

    return report


def write_diff_report(report: DiffReport, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    payload["only_in_export_ids"] = sorted(report.only_in_export)[:50]
    payload["only_in_idb_ids"] = sorted(report.only_in_idb)[:50]
    payload["recovered_input_tool_ids"] = sorted(report.recovered_inputs.keys())[:100]
    dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return dest
