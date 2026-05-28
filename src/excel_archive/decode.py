"""Extract strings and JSON fragments from IndexedDB binary values."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .structured_clone import extract_text_candidates

TOOL_ID_RE = re.compile(r"toolu_[A-Za-z0-9]+")
ID_TAG_RE = re.compile(r"\[id:([a-z0-9]+)\]")
SNIP_MARKER = "[snipped — context_snip applied]"
TOOL_ID_IN_JSON_RE = re.compile(r'"id"\s*:\s*"(toolu_[^"]+)"')


@dataclass
class StringExtractResult:
    tool_ids: set[str] = field(default_factory=set)
    message_ids: set[str] = field(default_factory=set)
    snip_markers: int = 0
    has_snip_archive: bool = False
    has_snip_registrations: bool = False
    tool_inputs: dict[str, dict] = field(default_factory=dict)  # toolu_id -> parsed input if small
    worksheet_result_count: int = 0
    raw_text_len: int = 0


def extract_strings_from_blob(data: bytes) -> StringExtractResult:
    """Phase-1 decode: UTF-8 scan for embedded JSON and markers."""
    text = decode_best_effort_text(data)
    result = StringExtractResult(raw_text_len=len(text))
    result.tool_ids = set(TOOL_ID_RE.findall(text))
    result.message_ids = set(ID_TAG_RE.findall(text))
    result.snip_markers = text.count(SNIP_MARKER)
    result.has_snip_archive = "snipArchive" in text
    result.has_snip_registrations = "snipRegistrations" in text
    result.worksheet_result_count = len(
        re.findall(r'"worksheet"\s*:\s*\{\s*"name"\s*:', text)
    )

    result.tool_inputs = recover_tool_inputs(text, result.tool_ids)
    return result


def decode_best_effort_text(data: bytes) -> str:
    """
    Decode text out of WebKit IndexedDB values.

    Many values contain UTF-8 strings, but some structured-clone regions include
    UTF-16LE-ish runs (ASCII bytes with null interleaves). We decode both and
    concatenate, letting downstream regexes find whichever representation exists.
    """
    c = extract_text_candidates(data)
    parts = [c.utf8]
    if c.utf16le and c.utf16le not in c.utf8:
        parts.append(c.utf16le)
    if c.deinterleaved_ascii and c.deinterleaved_ascii not in c.utf8:
        parts.append(c.deinterleaved_ascii)
    return "\n".join(p for p in parts if p)


def _parse_json_object_at(text: str, start: int) -> tuple[dict | None, int]:
    """Parse a JSON object starting at `start` (must point to '{')."""
    if start >= len(text) or text[start] != "{":
        return None, start
    depth = 0
    in_string = False
    escape = False
    for j in range(start, min(start + 200_000, len(text))):
        ch = text[j]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                chunk = text[start : j + 1]
                try:
                    parsed = json.loads(chunk)
                except json.JSONDecodeError:
                    return None, j + 1
                if isinstance(parsed, dict):
                    return parsed, j + 1
                return None, j + 1
    return None, start


def recover_tool_inputs(text: str, tool_ids: set[str] | None = None) -> dict[str, dict]:
    """
    Find non-empty tool_use inputs by locating tool ids and brace-matching input objects.
    """
    ids = tool_ids or set(TOOL_ID_RE.findall(text))
    recovered: dict[str, dict] = {}

    for tid in ids:
        needle = f'"{tid}"'
        pos = 0
        best: dict | None = None
        while True:
            hit = text.find(needle, pos)
            if hit < 0:
                break
            pos = hit + len(needle)
            window_start = max(0, hit - 800)
            window = text[window_start : hit + 4000]
            for m in re.finditer(r'"input"\s*:\s*\{', window):
                obj_start = window_start + m.end() - 1
                parsed, _ = _parse_json_object_at(text, obj_start)
                if parsed:
                    prev = best
                    if not prev or len(json.dumps(parsed)) > len(json.dumps(prev)):
                        best = parsed
            # Also handle id-before-input tool_use blocks
            for m in TOOL_ID_IN_JSON_RE.finditer(window):
                if m.group(1) != tid:
                    continue
                sub = window[m.start() :]
                im = re.search(r'"input"\s*:\s*\{', sub)
                if not im:
                    continue
                obj_start = window_start + m.start() + im.end() - 1
                parsed, _ = _parse_json_object_at(text, obj_start)
                if parsed:
                    prev = best
                    if not prev or len(json.dumps(parsed)) > len(json.dumps(prev)):
                        best = parsed
        if best:
            recovered[tid] = best

    return recovered


def extract_json_fragments(data: bytes, *, max_fragments: int = 50) -> list[object]:
    """
    Heuristic: find parseable JSON objects/arrays in binary blob.

    Useful when structured-clone wraps JSON transcripts.
    """
    text = data.decode("utf-8", errors="ignore")
    fragments: list[object] = []

    for opener, closer in (("[", "]"), ("{", "}")):
        start = 0
        while len(fragments) < max_fragments:
            i = text.find(opener, start)
            if i < 0:
                break
            depth = 0
            for j in range(i, min(i + 5_000_000, len(text))):
                ch = text[j]
                if ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        chunk = text[i : j + 1]
                        try:
                            fragments.append(json.loads(chunk))
                        except json.JSONDecodeError:
                            pass
                        start = j + 1
                        break
            else:
                break

    return fragments


def merge_string_extracts(parts: list[StringExtractResult]) -> StringExtractResult:
    merged = StringExtractResult()
    for p in parts:
        merged.tool_ids |= p.tool_ids
        merged.message_ids |= p.message_ids
        merged.snip_markers += p.snip_markers
        merged.has_snip_archive = merged.has_snip_archive or p.has_snip_archive
        merged.has_snip_registrations = merged.has_snip_registrations or p.has_snip_registrations
        merged.worksheet_result_count += p.worksheet_result_count
        merged.raw_text_len += p.raw_text_len
        for tid, inp in p.tool_inputs.items():
            prev = merged.tool_inputs.get(tid)
            if not prev or len(json.dumps(inp)) > len(json.dumps(prev)):
                merged.tool_inputs[tid] = inp
    return merged
