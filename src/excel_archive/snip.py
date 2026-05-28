"""Parse context_snip registrations — intent metadata, not compressed bodies."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .decode import SNIP_MARKER, recover_tool_inputs

# Tools that only manage context compression; never archive as normal tool I/O.
SNIP_SYSTEM_TOOLS = frozenset({"context_snip", "retrieve_snipped"})

CONTEXT_SNIP_ID_RE = re.compile(
    r'"id"\s*:\s*"(toolu_[^"]+)"[^}]{0,400}?"name"\s*:\s*"context_snip"',
    re.IGNORECASE | re.DOTALL,
)
CONTEXT_SNIP_NAME_FIRST_RE = re.compile(
    r'"name"\s*:\s*"context_snip"[^}]{0,400}?"id"\s*:\s*"(toolu_[^"]+)"',
    re.IGNORECASE | re.DOTALL,
)
BREADCRUMB_RANGE_RE = re.compile(
    r"Original range ([a-z0-9:]+)→([a-z0-9:]+)",
    re.IGNORECASE,
)
# Structured-clone snipRegistrations often embed as fromId / toId (camelCase).
CLONE_REG_RE = re.compile(
    r"fromId\x10[\x00-\xff]{0,4}([\w:]{4,12})\x04\x00\x00toId\x10[\x00-\xff]{0,4}([\w:]{4,12})",
)


def _normalize_id(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("id:"):
        return raw
    if ":" not in raw and len(raw) <= 12:
        return f"id:{raw}"
    return raw


def snip_note_key(from_id: str, to_id: str, summary: str) -> str:
    h = hashlib.sha256(f"{from_id}|{to_id}|{summary[:500]}".encode()).hexdigest()[:16]
    return h


def is_snipped_content(content: str | None) -> bool:
    if not content:
        return True
    if SNIP_MARKER in content:
        return True
    if content.strip() in ("{}", "[]", '""'):
        return True
    return False


def parse_snip_input(inp: dict[str, Any]) -> dict[str, str] | None:
    """Normalize context_snip tool input to from_id / to_id / summary."""
    if not inp:
        return None
    from_id = inp.get("from_id") or inp.get("fromId") or ""
    to_id = inp.get("to_id") or inp.get("toId") or ""
    summary = inp.get("summary") or ""
    if not summary and not from_id:
        return None
    return {
        "from_id": _normalize_id(str(from_id)) if from_id else "",
        "to_id": _normalize_id(str(to_id)) if to_id else "",
        "summary": str(summary).strip(),
    }


def extract_snip_notes_from_blob(data: bytes) -> list[dict[str, Any]]:
    """
    Extract deferred snip *registrations* (what Claude asked to compress later).

    Does not return snipArchive payloads or [snipped — …] placeholder text.
    """
    text = data.decode("utf-8", errors="ignore")
    inputs = recover_tool_inputs(text)
    notes: list[dict[str, Any]] = []
    seen: set[str] = set()

    tool_ids: set[str] = set()
    for pat in (CONTEXT_SNIP_ID_RE, CONTEXT_SNIP_NAME_FIRST_RE):
        for m in pat.finditer(text):
            tool_ids.add(m.group(1))

    for tool_id in tool_ids:
        parsed = parse_snip_input(inputs.get(tool_id) or {})
        if not parsed:
            continue
        key = snip_note_key(parsed["from_id"], parsed["to_id"], parsed["summary"])
        if key in seen:
            continue
        seen.add(key)
        notes.append(
            {
                "kind": "snip_note",
                "registration_tool_id": tool_id,
                "from_id": parsed["from_id"],
                "to_id": parsed["to_id"],
                "summary": parsed["summary"],
                "status": "registered",
            }
        )

    for m in CLONE_REG_RE.finditer(text):
        from_id = _normalize_id(m.group(1))
        to_id = _normalize_id(m.group(2))
        # Summary is not in this regex; dedupe by range only.
        window = text[m.end() : m.end() + 400]
        summary = ""
        sm = re.search(r"[\x20-\x7e]{40,}", window)
        if sm:
            summary = sm.group(0).strip()[:2000]
        key = snip_note_key(from_id, to_id, summary)
        if key in seen:
            continue
        seen.add(key)
        notes.append(
            {
                "kind": "snip_note",
                "from_id": from_id,
                "to_id": to_id,
                "summary": summary,
                "status": "registered",
                "source": "snipRegistrations",
            }
        )

    for from_id, to_id in BREADCRUMB_RANGE_RE.findall(text):
        fid, tid = _normalize_id(from_id), _normalize_id(to_id)
        key = snip_note_key(fid, tid, "")
        if key in seen:
            continue
        seen.add(key)
        notes.append(
            {
                "kind": "snip_note",
                "from_id": fid,
                "to_id": tid,
                "summary": "",
                "status": "applied",
                "source": "breadcrumb",
            }
        )

    return notes
