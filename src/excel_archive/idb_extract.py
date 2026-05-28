"""Read WebKit IndexedDB SQLite and extract Claude for Excel session data."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .decode import (
    StringExtractResult,
    extract_json_fragments,
    extract_strings_from_blob,
    merge_string_extracts,
)
from .paths import STORE_BLOBS, STORE_CHATS


@dataclass
class RecordRow:
    object_store_id: int
    record_id: int
    size: int


@dataclass
class ExtractedSession:
    """Aggregated extract from one IndexedDB snapshot directory or sqlite file."""

    source: Path
    store_names: dict[int, str] = field(default_factory=dict)
    records_by_store: dict[int, list[RecordRow]] = field(default_factory=dict)
    strings: StringExtractResult = field(default_factory=StringExtractResult)
    json_fragments: list[object] = field(default_factory=list)
    largest_chat_blob_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            "source": str(self.source),
            "store_names": self.store_names,
            "record_counts": {
                sid: len(rows) for sid, rows in self.records_by_store.items()
            },
            "strings": {
                "tool_id_count": len(self.strings.tool_ids),
                "message_id_count": len(self.strings.message_ids),
                "snip_markers": self.strings.snip_markers,
                "has_snip_archive": self.strings.has_snip_archive,
                "has_snip_registrations": self.strings.has_snip_registrations,
                "tool_inputs_recovered": len(self.strings.tool_inputs),
                "worksheet_result_count": self.strings.worksheet_result_count,
                "largest_chat_blob_bytes": self.largest_chat_blob_bytes,
            },
            "json_fragment_count": len(self.json_fragments),
        }


def _resolve_store_names(conn: sqlite3.Connection) -> dict[int, str]:
    names: dict[int, str] = {}
    try:
        for row in conn.execute("SELECT id, name FROM ObjectStoreInfo"):
            names[int(row[0])] = str(row[1])
    except sqlite3.Error:
        pass
    return names


def extract_from_sqlite(db_path: Path, *, max_records_per_store: int = 32) -> ExtractedSession:
    """
    Read Records from a copied IndexedDB.sqlite3.

    Scans the largest values per object store (chats, blobs, results).
    """
    out = ExtractedSession(source=db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out.store_names = _resolve_store_names(conn)
        store_ids = list(out.store_names.keys()) or [STORE_CHATS, STORE_BLOBS]

        extracts: list[StringExtractResult] = []

        for store_id in store_ids:
            rows: list[RecordRow] = []
            cur = conn.execute(
                """
                SELECT recordID, length(value)
                FROM Records
                WHERE objectStoreID = ?
                ORDER BY length(value) DESC
                LIMIT ?
                """,
                (store_id, max_records_per_store),
            )
            for record_id, size in cur:
                rows.append(RecordRow(store_id, int(record_id), int(size)))
            out.records_by_store[store_id] = rows

            for record_id, _ in [(r.record_id, r.size) for r in rows]:
                row = conn.execute(
                    "SELECT value FROM Records WHERE objectStoreID = ? AND recordID = ?",
                    (store_id, record_id),
                ).fetchone()
                if not row or not row[0]:
                    continue
                blob: bytes = row[0]
                name = out.store_names.get(store_id, str(store_id))
                if name == "chats" or store_id == STORE_CHATS:
                    out.largest_chat_blob_bytes = max(out.largest_chat_blob_bytes, len(blob))
                part = extract_strings_from_blob(blob)
                extracts.append(part)
                if len(out.json_fragments) < 20:
                    out.json_fragments.extend(
                        extract_json_fragments(blob, max_fragments=10)
                    )

        out.strings = merge_string_extracts(extracts)
    finally:
        conn.close()

    return out


def extract_from_snapshot_dir(snapshot_dir: Path, **kwargs) -> ExtractedSession:
    """Extract from a snapshot folder produced by excel-archive watch."""
    sqlite = snapshot_dir / "IndexedDB.sqlite3"
    if not sqlite.is_file():
        raise FileNotFoundError(f"No IndexedDB.sqlite3 in {snapshot_dir}")
    return extract_from_sqlite(sqlite, **kwargs)


def write_extract_artifact(session: ExtractedSession, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = session.to_dict()
    payload["tool_inputs"] = session.strings.tool_inputs
    dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return dest
