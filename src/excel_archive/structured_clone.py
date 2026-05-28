"""Structured-clone-aware text extraction (best-effort).

We do NOT attempt full WebKit structured-clone deserialization here.
Instead, we extract likely text payloads more safely than naive decode:
- UTF-8 decode (ignore errors)
- UTF-16LE decode (ignore errors)
- deinterleave null bytes to recover ASCII (A\\0B\\0C\\0 → ABC)

Callers can decide which stream(s) to search.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextCandidates:
    utf8: str
    utf16le: str
    deinterleaved_ascii: str


def deinterleave_nulls(data: bytes) -> str:
    """Recover ASCII from UTF-16LE-like null interleave (best-effort)."""
    if len(data) < 2:
        return ""
    # Keep bytes at even positions where odd byte is 0; otherwise skip.
    out = bytearray()
    pairs = len(data) // 2
    for i in range(pairs):
        lo = data[i * 2]
        hi = data[i * 2 + 1]
        if hi == 0 and 32 <= lo <= 126:
            out.append(lo)
        else:
            # break runs on non-null interleaves to reduce noise
            out.append(10)  # newline
    try:
        s = out.decode("ascii", errors="ignore")
    except Exception:
        return ""
    # Collapse excessive newlines
    while "\n\n\n" in s:
        s = s.replace("\n\n\n", "\n\n")
    return s


def extract_text_candidates(data: bytes) -> TextCandidates:
    utf8 = data.decode("utf-8", errors="ignore")
    utf16 = data.decode("utf-16le", errors="ignore") if b"\x00" in data else ""
    deint = deinterleave_nulls(data) if b"\x00" in data else ""
    return TextCandidates(utf8=utf8, utf16le=utf16, deinterleaved_ascii=deint)

