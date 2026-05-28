"""Append-only session journal — capture tool I/O before context snip erases it."""

from __future__ import annotations

import json
import re
import sqlite3
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .decode import decode_best_effort_text, recover_tool_inputs
from .paths import STORE_CHATS, default_archive_root
from .snip import SNIP_SYSTEM_TOOLS, extract_snip_notes_from_blob, is_snipped_content
from .match_workbook import infer_workbook_names_from_blob, pick_workbook_name, remember_workbook_name
from .paths import IdbDatabasePaths

TEXT_BLOCK_RE = re.compile(r'\{"type"\s*:\s*"text"\s*,\s*"text"\s*:\s*"')

TOOL_USE_BLOCK_RE = re.compile(
    r'\{"type"\s*:\s*"tool_use"[^}]*"id"\s*:\s*"(toolu_[^"]+)"[^}]*"name"\s*:\s*"([^"]+)"',
)
TOOL_RESULT_BLOCK_RE = re.compile(
    r'\{"type"\s*:\s*"tool_result"[^}]*"tool_use_id"\s*:\s*"(toolu_[^"]+)"',
)


@dataclass
class JournalState:
    """Tracks persisted events; snipped/compressed replays must not overwrite."""

    messages: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_uses: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    snip_notes: dict[str, dict[str, Any]] = field(default_factory=dict)  # key -> snip_note

    @classmethod
    def load(cls, path: Path) -> JournalState:
        if not path.is_file():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            messages=data.get("messages") or {},
            tool_uses=data.get("tool_uses") or {},
            tool_results=data.get("tool_results") or {},
            snip_notes=data.get("snip_notes") or {},
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "messages": self.messages,
                    "tool_uses": self.tool_uses,
                    "tool_results": self.tool_results,
                    "snip_notes": self.snip_notes,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def default_journal_dir(root: Path | None = None) -> Path:
    base = root or default_archive_root()
    return base / "journal"


def _message_key(role: str, text: str) -> str:
    h = hashlib.sha256(f"{role}|{text}".encode()).hexdigest()[:20]
    return h


def _extract_text_blocks_with_roles(blob_text: str) -> list[dict[str, Any]]:
    """
    Best-effort extraction of `type:text` blocks from the chat blob.

    We infer role by looking back for `"role":"user"` / `"role":"assistant"`.
    This is intentionally heuristic (string scan), but it reliably captures:
    - <user_context>, <conductor_context>, <connected_peers>, <uploaded_files>
    - user prompts
    - assistant narration
    """
    out: list[dict[str, Any]] = []
    idx = 0
    while True:
        hit = blob_text.find('{"type":"text","text":"', idx)
        if hit < 0:
            break
        # Determine role from nearby context
        back = blob_text[max(0, hit - 250) : hit]
        role = "user" if '"role":"user"' in back else "assistant" if '"role":"assistant"' in back else "unknown"

        # Parse JSON string content until closing quote (handle escapes)
        start = hit + len('{"type":"text","text":"')
        chars: list[str] = []
        i = start
        escape = False
        while i < len(blob_text):
            ch = blob_text[i]
            if escape:
                chars.append(ch)
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                break
            else:
                chars.append(ch)
            i += 1
        text = "".join(chars)
        idx = i + 1
        if not text.strip():
            continue
        if is_snipped_content(text):
            # do not store snip placeholders; we want the pre-snip first copy
            continue
        out.append({"kind": "message", "role": role, "text": text})
    return out


def _largest_chat_blob(conn: sqlite3.Connection, store_names: dict[int, str]) -> bytes | None:
    chat_store = next((sid for sid, name in store_names.items() if name == "chats"), STORE_CHATS)
    row = conn.execute(
        """
        SELECT value FROM Records
        WHERE objectStoreID = ?
        ORDER BY length(value) DESC
        LIMIT 1
        """,
        (chat_store,),
    ).fetchone()
    return row[0] if row and row[0] else None


def _extract_tool_result_contents(text: str, tool_use_id: str) -> str | None:
    """Best-effort pull of tool_result content for one id from embedded JSON strings."""
    needle = f'"tool_use_id":"{tool_use_id}"'
    pos = 0
    best: str | None = None
    while True:
        hit = text.find(needle, pos)
        if hit < 0:
            break
        pos = hit + len(needle)
        window = text[hit : hit + 120_000]
        for key in ('"content":"', '"content": "'):
            ci = window.find(key)
            if ci < 0:
                continue
            start = hit + ci + len(key)
            # Read JSON string value
            out: list[str] = []
            i = start
            escape = False
            while i < len(text):
                ch = text[i]
                if escape:
                    out.append(ch)
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    break
                else:
                    out.append(ch)
                i += 1
            candidate = "".join(out)
            if is_snipped_content(candidate):
                continue
            if best is None or len(candidate) > len(best):
                best = candidate
    return best


def extract_events_from_chat_blob(data: bytes) -> list[dict[str, Any]]:
    """
    Pull substantive tool I/O and snip *notes* from the chats store blob.

    Excludes context_snip / retrieve_snipped as normal tools, snip placeholders,
    and empty inputs. Snip intent is captured via extract_snip_notes_from_blob().
    """
    text = decode_best_effort_text(data)
    events: list[dict[str, Any]] = []
    events.extend(_extract_text_blocks_with_roles(text))
    events.extend(extract_snip_notes_from_blob(data))
    inputs = recover_tool_inputs(text)

    for m in TOOL_USE_BLOCK_RE.finditer(text):
        tid, name = m.group(1), m.group(2)
        if name in SNIP_SYSTEM_TOOLS:
            continue
        inp = inputs.get(tid) or {}
        if not inp:
            continue
        events.append(
            {
                "kind": "tool_use",
                "id": tid,
                "name": name,
                "input": inp,
            }
        )

    seen_results: set[str] = set()
    for m in TOOL_RESULT_BLOCK_RE.finditer(text):
        tid = m.group(1)
        if tid in seen_results:
            continue
        seen_results.add(tid)
        content = _extract_tool_result_contents(text, tid)
        if is_snipped_content(content):
            continue
        events.append(
            {
                "kind": "tool_result",
                "tool_use_id": tid,
                "content": content,
            }
        )

    return events


def merge_events(state: JournalState, events: list[dict[str, Any]], *, snapshot: str) -> list[dict[str, Any]]:
    """Apply merge policy; return newly appended rows for JSONL."""
    ts = datetime.now(UTC).isoformat()
    new_rows: list[dict[str, Any]] = []

    for ev in events:
        if ev["kind"] == "message":
            role = ev.get("role") or "unknown"
            text = ev.get("text") or ""
            if not text.strip() or is_snipped_content(text):
                continue
            key = _message_key(role, text)
            if key in state.messages:
                continue
            row = {**ev, "captured_at": ts, "snapshot": snapshot}
            state.messages[key] = row
            new_rows.append(row)
        elif ev["kind"] == "snip_note":
            from .snip import snip_note_key

            fid = ev.get("from_id") or ""
            tid = ev.get("to_id") or ""
            summary = ev.get("summary") or ""
            key = snip_note_key(fid, tid, summary)
            prev = state.snip_notes.get(key)
            # Prefer richer summary; never replace with empty if we have text.
            if prev:
                if len(summary) <= len(prev.get("summary") or ""):
                    continue
            row = {**ev, "captured_at": ts, "snapshot": snapshot}
            state.snip_notes[key] = row
            new_rows.append(row)
        elif ev["kind"] == "tool_use":
            tid = ev["id"]
            if ev.get("name") in SNIP_SYSTEM_TOOLS:
                continue
            prev = state.tool_uses.get(tid)
            if prev and prev.get("input"):
                continue
            row = {**ev, "captured_at": ts, "snapshot": snapshot}
            state.tool_uses[tid] = row
            new_rows.append(row)
        elif ev["kind"] == "tool_result":
            content = ev.get("content") or ""
            if is_snipped_content(content):
                continue
            tid = ev["tool_use_id"]
            prev = state.tool_results.get(tid)
            prev_len = len(prev.get("content") or "") if prev else 0
            new_len = len(content)
            if prev and prev_len >= new_len:
                continue
            row = {**ev, "captured_at": ts, "snapshot": snapshot}
            state.tool_results[tid] = row
            new_rows.append(row)

    return new_rows


def ingest_sqlite(
    db_path: Path,
    *,
    journal_root: Path | None = None,
    session_key: str = "default",
    workbook_name: str | None = None,
    idb_origin: IdbDatabasePaths | None = None,
) -> int:
    """
    Read latest chat blob from a snapshot sqlite and append new events to JSONL.

    Returns count of new JSONL lines written.
    """
    journal_root = journal_root or default_journal_dir()
    session_dir = journal_root / session_key
    state_path = session_dir / "state.json"
    jsonl_path = session_dir / "events.jsonl"

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        store_names: dict[int, str] = {}
        for row in conn.execute("SELECT id, name FROM ObjectStoreInfo"):
            store_names[int(row[0])] = str(row[1])
        blob = _largest_chat_blob(conn, store_names)
    finally:
        conn.close()

    if not blob:
        return 0

    # If we weren't told the workbook name, try to infer and remember it.
    if workbook_name is None and idb_origin is not None:
        counts = infer_workbook_names_from_blob(blob)
        picked = pick_workbook_name(counts)
        if picked:
            workbook_name = picked.workbook_name
            remember_workbook_name(idb_origin, workbook_name, reason="inferred_from_blob")

    events = extract_events_from_chat_blob(blob)
    state = JournalState.load(state_path)
    snapshot = db_path.parent.name if db_path.name == "IndexedDB.sqlite3" else db_path.name
    new_rows = merge_events(state, events, snapshot=snapshot)

    if new_rows:
        state.save(state_path)
        with jsonl_path.open("a", encoding="utf-8") as fh:
            for row in new_rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    from .render_tape import write_session_tape

    if state.messages or state.tool_uses or state.tool_results or state.snip_notes:
        write_session_tape(
            session_dir,
            title=f"Session — {workbook_name or session_key}",
            source_note="Live capture from Excel IndexedDB (pre-snip merge policy).",
        )
        from .navigation import refresh_archive_navigation

        refresh_archive_navigation(journal_root=journal_root)
    return len(new_rows)


def rebuild_journal_from_snapshots(
    snapshots_dir: Path,
    *,
    journal_root: Path | None = None,
    session_key: str = "default",
) -> int:
    """Replay all snapshot dirs in order (oldest first) to build journal."""
    if not snapshots_dir.is_dir():
        return 0
    dirs = sorted(
        (p for p in snapshots_dir.iterdir() if p.is_dir() and (p / "IndexedDB.sqlite3").is_file()),
        key=lambda p: p.name,
    )
    total = 0
    session_dir = (journal_root or default_journal_dir()) / session_key
    if session_dir.exists():
        import shutil

        shutil.rmtree(session_dir)
    for d in dirs:
        total += ingest_sqlite(d / "IndexedDB.sqlite3", journal_root=journal_root, session_key=session_key)
    return total
