"""Generate per-workbook index.md for navigation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SessionSummary:
    name: str
    tape: Path | None
    tape_full: Path | None
    updated_at: float
    tool_counts: dict[str, int]
    snip_notes: int


def _count_tools(state: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ev in (state.get("tool_uses") or {}).values():
        name = ev.get("name")
        if isinstance(name, str) and name:
            counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


def summarize_session_dir(session_dir: Path) -> SessionSummary | None:
    state_path = session_dir / "state.json"
    if not state_path.is_file():
        return None
    state = json.loads(state_path.read_text(encoding="utf-8"))
    tape = session_dir / "session.tape.md"
    tape_full = session_dir / "session.full.tape.md"
    updated_at = max(
        p.stat().st_mtime
        for p in [state_path, *([tape] if tape.is_file() else []), *([tape_full] if tape_full.is_file() else [])]
    )
    tool_counts = _count_tools(state)
    snip_notes = len(state.get("snip_notes") or {})
    return SessionSummary(
        name=session_dir.name,
        tape=tape if tape.is_file() else None,
        tape_full=tape_full if tape_full.is_file() else None,
        updated_at=updated_at,
        tool_counts=tool_counts,
        snip_notes=snip_notes,
    )


def generate_index_md(workbook_root: Path) -> str:
    journal_root = workbook_root / "journal"
    sessions: list[SessionSummary] = []
    if journal_root.is_dir():
        for sess_dir in journal_root.iterdir():
            if not sess_dir.is_dir():
                continue
            s = summarize_session_dir(sess_dir)
            if s:
                sessions.append(s)
    sessions.sort(key=lambda s: s.updated_at, reverse=True)

    lines: list[str] = []
    lines.append(f"# Excel archive index")
    lines.append("")
    lines.append(f"- **Workbook root**: `{workbook_root}`")
    lines.append(f"- **Generated**: {datetime.utcnow().isoformat()}Z")
    lines.append("")

    wb_dir = workbook_root / "workbook"
    if wb_dir.is_dir():
        copies = sorted([p for p in wb_dir.iterdir() if p.is_file() and p.suffix.lower() == ".xlsx"], key=lambda p: p.stat().st_mtime, reverse=True)
        if copies:
            lines.append("## Latest workbook copies")
            lines.append("")
            for p in copies[:10]:
                ts = datetime.utcfromtimestamp(p.stat().st_mtime).isoformat() + "Z"
                rel = p.relative_to(workbook_root)
                lines.append(f"- `{ts}` — `{rel}`")
            lines.append("")

    lines.append("## Sessions")
    lines.append("")
    if not sessions:
        lines.append("_No sessions found yet._")
        lines.append("")
        return "\n".join(lines)

    for s in sessions:
        ts = datetime.utcfromtimestamp(s.updated_at).isoformat() + "Z"
        lines.append(f"### {s.name}")
        lines.append("")
        lines.append(f"- **Updated**: `{ts}`")
        lines.append(f"- **Snip notes**: {s.snip_notes}")
        if s.tape:
            lines.append(f"- **Tape**: `{s.tape.relative_to(workbook_root)}`")
        if s.tape_full:
            lines.append(f"- **Full tape**: `{s.tape_full.relative_to(workbook_root)}`")
        if s.tool_counts:
            top = list(s.tool_counts.items())[:8]
            lines.append("- **Top tools**: " + ", ".join(f"`{k}`×{v}" for k, v in top))
        lines.append("")

    return "\n".join(lines)


def write_index_md(workbook_root: Path) -> Path:
    out = workbook_root / "index.md"
    out.write_text(generate_index_md(workbook_root), encoding="utf-8")
    return out

