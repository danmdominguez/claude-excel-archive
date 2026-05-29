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
from .workbook_migration import (
    journal_dir_for_workbook_name_resolved,
    write_workbook_meta,
)
from .snip import SNIP_SYSTEM_TOOLS, extract_snip_notes_from_blob, is_snipped_content
from .match_workbook import infer_workbook_names_from_blob, pick_workbook_name, remember_workbook_name
from .paths import IdbDatabasePaths
from .workbook_attribution import annotate_events

INITIAL_STATE_RE = re.compile(
    r"<initial_state>\s*(\{.*?\})\s*</initial_state>",
    re.DOTALL,
)

TEXT_BLOCK_RE = re.compile(r'\{"type"\s*:\s*"text"\s*,\s*"text"\s*:\s*"')

TOOL_USE_BLOCK_RE = re.compile(
    r'\{"type"\s*:\s*"tool_use"[^}]*"id"\s*:\s*"(toolu_[^"]+)"[^}]*"name"\s*:\s*"([^"]+)"',
)
TOOL_RESULT_BLOCK_RE = re.compile(
    r'\{"type"\s*:\s*"tool_result"[^}]*"tool_use_id"\s*:\s*"(toolu_[^"]+)"',
)
TOOLU_ID_RE = re.compile(r"(toolu_[A-Za-z0-9]+)")
_ANGLE_TAG_BLOCK_RE = re.compile(
    r"<(user_context|conductor_context|connected_peers|initial_state|user_changes|uploaded_files)>"
    r"([\s\S]*?)"
    r"</\1>",
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


@dataclass(frozen=True)
class ChatRecord:
    """One row in the IndexedDB `chats` object store."""

    rowid: int
    size: int
    data: bytes


def parse_initial_state(text: str) -> dict[str, Any] | None:
    """Parse JSON inside `<initial_state>…</initial_state>` (export or blob text)."""
    m = INITIAL_STATE_RE.search(text)
    raw = m.group(1) if m else None
    if raw is None:
        start = text.find("<initial_state>")
        if start < 0:
            return None
        chunk = text[start : start + 80_000]
        end = chunk.find("</initial_state>")
        if end < 0:
            return None
        inner = chunk[len("<initial_state>") : end].strip()
        if inner.startswith("{"):
            raw = inner
        else:
            return None
    for candidate in (raw, raw.replace("\\n", "\n").replace('\\"', '"')):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _extract_angle_tag_blocks(blob_text: str) -> list[str]:
    """Pull `<user_context>`, `<initial_state>`, etc. from mixed binary/UTF-8 blobs."""
    return [f"<{m.group(1)}>{m.group(2)}</{m.group(1)}>" for m in _ANGLE_TAG_BLOCK_RE.finditer(blob_text)]


def _extract_structured_clone_texts(blob_text: str) -> list[str]:
    """
  Extract user-visible strings from WebKit structured-clone `text` fields.

  Observed prefix: `type\\x10\\x04\\x00\\x00text\\x13\\x10` + 3-byte header + payload.
    """
    out: list[str] = []
    marker = "text\x13\x10"
    idx = 0
    while True:
        hit = blob_text.find(marker, idx)
        if hit < 0:
            break
        start = hit + len(marker) + 3
        end = start
        while end < len(blob_text):
            if end - start > 500_000:
                break
            ch = blob_text[end]
            if ord(ch) < 32 and ch not in "\n\r\t":
                if end - start > 8:
                    break
            if ch == "<" and end > start + 4:
                nxt = blob_text[end + 1 : end + 24]
                if any(
                    nxt.startswith(p)
                    for p in ("user_", "conductor", "connected", "initial", "uploaded", "/")
                ):
                    break
            end += 1
        chunk = blob_text[start:end].strip()
        idx = end
        if len(chunk) >= 4 and not is_snipped_content(chunk):
            out.append(chunk)
    return out


def _extract_structured_clone_tool_uses(blob_text: str) -> list[dict[str, Any]]:
    """Best-effort tool_use rows when JSON `type:tool_use` blocks are absent."""
    uses: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in TOOLU_ID_RE.finditer(blob_text):
        tid = m.group(1)
        if tid in seen:
            continue
        window = blob_text[m.start() : m.start() + 12_000]
        name_m = re.search(r"name\x10[\x00-\xff]{0,4}([A-Za-z_][A-Za-z0-9_]*)", window)
        if not name_m:
            continue
        name = name_m.group(1)
        if name in SNIP_SYSTEM_TOOLS:
            continue
        inp: dict[str, Any] = {}
        sheet_m = re.search(r"sheetName\x10[\x00-\xff]{0,4}([^\x00\x01\x02]{1,80})", window)
        if sheet_m:
            inp["sheetName"] = sheet_m.group(1).strip()
        if name == "execute_office_js":
            code_m = re.search(r"code\x10[\x00-\xff]{0,4}([\s\S]{20,8000}?)(?:\x01\x00\x00\x00|explanation\x10)", window)
            if code_m:
                inp["code"] = code_m.group(1).strip()
        if name == "set_cell_range" and "cells" in window:
            inp.setdefault("cells", {})
        if not inp and name not in ("bash", "get_cell_ranges"):
            continue
        seen.add(tid)
        uses.append({"kind": "tool_use", "id": tid, "name": name, "input": inp})
    return uses


def resolve_record_workbook(blob: bytes) -> str:
    """
    Workbook identity for routing a chat record to an archive folder.

    Priority: initial_state.fileName (any tag block) → unknown.
    """
    text = decode_best_effort_text(blob)
    for block in _extract_angle_tag_blocks(text):
        state = parse_initial_state(block)
        if state:
            fn = state.get("fileName")
            if isinstance(fn, str) and fn.strip():
                return fn.strip()
    state = parse_initial_state(text)
    if state:
        fn = state.get("fileName")
        if isinstance(fn, str) and fn.strip():
            return fn.strip()
    return "unknown"


def _message_key(role: str, text: str, *, record_rowid: int | None = None) -> str:
    scope = f"{record_rowid}|" if record_rowid is not None else ""
    h = hashlib.sha256(f"{scope}{role}|{text}".encode()).hexdigest()[:20]
    return h


def _tool_key(tool_id: str, *, record_rowid: int | None = None) -> str:
    if record_rowid is None:
        return tool_id
    return f"{record_rowid}:{tool_id}"


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


def _chat_store_id(store_names: dict[int, str]) -> int:
    return next((sid for sid, name in store_names.items() if name == "chats"), STORE_CHATS)


def iter_chat_records(conn: sqlite3.Connection, store_names: dict[int, str]) -> list[ChatRecord]:
    """All chat blobs in the `chats` store (largest first). Uses rowid to avoid IDBKEY collation."""
    chat_store = _chat_store_id(store_names)
    rows = conn.execute(
        "SELECT rowid, length(value), value FROM Records WHERE objectStoreID = ?",
        (chat_store,),
    ).fetchall()
    records = [
        ChatRecord(rowid=int(r[0]), size=int(r[1]), data=r[2])
        for r in rows
        if r[2]
    ]
    records.sort(key=lambda rec: rec.size, reverse=True)
    return records


def _largest_chat_blob(conn: sqlite3.Connection, store_names: dict[int, str]) -> bytes | None:
    records = iter_chat_records(conn, store_names)
    return records[0].data if records else None


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
    for tag_block in _extract_angle_tag_blocks(text):
        mclass = _message_class_for_text(tag_block)
        ev: dict[str, Any] = {"kind": "message", "role": "user", "text": tag_block}
        if mclass:
            ev["message_class"] = mclass
            if mclass == "workbook_state":
                parsed = parse_initial_state(tag_block)
                if parsed:
                    ev["initial_state"] = parsed
        events.append(ev)
    for plain in _extract_structured_clone_texts(text):
        if plain.startswith("<") and ">" in plain[:40]:
            continue
        events.append({"kind": "message", "role": "user", "text": plain})
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

    json_tool_ids = {e["id"] for e in events if e.get("kind") == "tool_use"}
    for tu in _extract_structured_clone_tool_uses(text):
        if tu["id"] not in json_tool_ids:
            events.append(tu)
            json_tool_ids.add(tu["id"])

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


def _message_class_for_text(text: str) -> str | None:
    t = text.strip()
    if t.startswith("<initial_state>"):
        return "workbook_state"
    return None


def merge_events(
    state: JournalState,
    events: list[dict[str, Any]],
    *,
    snapshot: str,
    record_rowid: int | None = None,
    workbook_file_name: str | None = None,
    chat_record_bytes: int | None = None,
) -> list[dict[str, Any]]:
    """Apply merge policy; return newly appended rows for JSONL."""
    ts = datetime.now(UTC).isoformat()
    new_rows: list[dict[str, Any]] = []
    record_meta: dict[str, Any] = {}
    if record_rowid is not None:
        record_meta["idb_record_rowid"] = record_rowid
    if workbook_file_name:
        record_meta["workbook_file_name"] = workbook_file_name
    if chat_record_bytes is not None:
        record_meta["chat_record_bytes"] = chat_record_bytes

    for ev in events:
        if ev["kind"] == "message":
            role = ev.get("role") or "unknown"
            text = ev.get("text") or ""
            if not text.strip() or is_snipped_content(text):
                continue
            mclass = ev.get("message_class") or _message_class_for_text(text)
            key = _message_key(role, text, record_rowid=record_rowid)
            if key in state.messages:
                continue
            row = {**ev, "captured_at": ts, "snapshot": snapshot, **record_meta}
            if mclass:
                row["message_class"] = mclass
                parsed = parse_initial_state(text) if mclass == "workbook_state" else None
                if parsed:
                    row["initial_state"] = parsed
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
            row = {**ev, "captured_at": ts, "snapshot": snapshot, **record_meta}
            state.snip_notes[key] = row
            new_rows.append(row)
        elif ev["kind"] == "tool_use":
            tid = ev["id"]
            if ev.get("name") in SNIP_SYSTEM_TOOLS:
                continue
            tkey = _tool_key(tid, record_rowid=record_rowid)
            prev = state.tool_uses.get(tkey)
            if prev and prev.get("input"):
                continue
            row = {**ev, "captured_at": ts, "snapshot": snapshot, **record_meta}
            state.tool_uses[tkey] = row
            new_rows.append(row)
        elif ev["kind"] == "tool_result":
            content = ev.get("content") or ""
            if is_snipped_content(content):
                continue
            tid = ev["tool_use_id"]
            tkey = _tool_key(tid, record_rowid=record_rowid)
            prev = state.tool_results.get(tkey)
            prev_len = len(prev.get("content") or "") if prev else 0
            new_len = len(content)
            if prev and prev_len >= new_len:
                continue
            row = {**ev, "captured_at": ts, "snapshot": snapshot, **record_meta}
            state.tool_results[tkey] = row
            new_rows.append(row)

    return new_rows


def ingest_chat_blob(
    blob: bytes,
    *,
    journal_root: Path,
    session_key: str,
    snapshot: str,
    workbook_name: str | None = None,
    idb_record_rowid: int | None = None,
    chat_record_bytes: int | None = None,
    idb_origin: IdbDatabasePaths | None = None,
) -> int:
    """Ingest one chat record blob into a specific journal directory."""
    session_dir = journal_root / session_key
    state_path = session_dir / "state.json"
    jsonl_path = session_dir / "events.jsonl"

    wb_name = workbook_name
    if wb_name is None:
        wb_name = resolve_record_workbook(blob)

    if idb_origin is not None and wb_name and wb_name != "unknown":
        remember_workbook_name(idb_origin, wb_name, reason="initial_state_or_inferred")

    if wb_name and wb_name != "unknown":
        wb_root = journal_root.parent
        if wb_root.name.startswith("_unsaved_"):
            write_workbook_meta(wb_root, unsaved_name=wb_name)

    events = extract_events_from_chat_blob(blob)
    events = annotate_events(events, local_workbook=wb_name)
    state = JournalState.load(state_path)
    new_rows = merge_events(
        state,
        events,
        snapshot=snapshot,
        record_rowid=idb_record_rowid,
        workbook_file_name=wb_name,
        chat_record_bytes=chat_record_bytes,
    )

    if new_rows:
        state.save(state_path)
        with jsonl_path.open("a", encoding="utf-8") as fh:
            for row in new_rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    from .render_tape import write_session_tape

    if state.messages or state.tool_uses or state.tool_results or state.snip_notes:
        write_session_tape(
            session_dir,
            title=f"Session — {wb_name or session_key}",
            source_note="Live capture from Excel IndexedDB (pre-snip merge policy).",
        )
    return len(new_rows)


def ingest_sqlite(
    db_path: Path,
    *,
    journal_root: Path | None = None,
    session_key: str = "default",
    workbook_name: str | None = None,
    idb_origin: IdbDatabasePaths | None = None,
    archive_root: Path | None = None,
    fan_out: bool | None = None,
) -> int:
    """
    Read all chat records from a snapshot sqlite and append new events to JSONL.

    When fan_out is True (default when journal_root is None), each chat record is
    routed to `journal_dir_for_workbook_name(initial_state.fileName)`.

    Returns count of new JSONL lines written across all targets.
    """
    archive_root = archive_root or default_archive_root()
    if fan_out is None:
        fan_out = journal_root is None

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        store_names: dict[int, str] = {}
        for row in conn.execute("SELECT id, name FROM ObjectStoreInfo"):
            store_names[int(row[0])] = str(row[1])
        records = iter_chat_records(conn, store_names)
    finally:
        conn.close()

    if not records:
        return 0

    snapshot = db_path.parent.name if db_path.name == "IndexedDB.sqlite3" else db_path.name
    total = 0
    touched_roots: set[Path] = set()

    for rec in records:
        wb_name = resolve_record_workbook(rec.data)
        if fan_out:
            target_journal = journal_dir_for_workbook_name_resolved(
                wb_name, archive_root=archive_root
            )
        else:
            target_journal = journal_root or default_journal_dir()
        n = ingest_chat_blob(
            rec.data,
            journal_root=target_journal,
            session_key=session_key,
            snapshot=snapshot,
            workbook_name=workbook_name or wb_name,
            idb_record_rowid=rec.rowid,
            chat_record_bytes=rec.size,
            idb_origin=idb_origin,
        )
        total += n
        if n:
            touched_roots.add(target_journal)

    if touched_roots:
        from .navigation import refresh_archive_navigation

        for jroot in touched_roots:
            refresh_archive_navigation(journal_root=jroot)
        refresh_archive_navigation(journal_root=None)

    return total


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
