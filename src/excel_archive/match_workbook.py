"""Match Excel workbook identity to a Claude for Excel chat snapshot.

We cannot reliably infer a *filesystem path* from the add-in’s IndexedDB.
We can often infer a workbook *filename* from:
- user/assistant text (e.g. "... on EF Shop Model DD.xlsx")
- snip summaries (context_snip summary often includes workbook name)
- tool results that serialize workbook name (less common in export JSON)

Hybrid strategy:
- explicit --workbook path wins (full-path grouping)
- else infer a single workbook filename from the blob; if ambiguous, return None
- persist remembered mapping keyed by IndexedDB origin for future runs
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import default_archive_root, IdbDatabasePaths


_XLSX_RE = re.compile(r"\.xlsx\b", re.IGNORECASE)


@dataclass(frozen=True)
class WorkbookMatch:
    workbook_name: str
    confidence: float
    reasons: list[str]


def _normalize_workbook_name(name: str) -> str:
    # Strip common trailing punctuation that can appear adjacent to filenames in prose.
    name = name.strip().replace("\u00a0", " ")
    return name.rstrip(").,;:\"'")


def infer_workbook_names_from_blob(blob_bytes: bytes) -> dict[str, int]:
    """Return workbook filename candidates with rough frequency counts."""
    text = blob_bytes.decode("utf-8", errors="ignore")
    counts: dict[str, int] = {}
    # Scan each ".xlsx" occurrence and extract a plausible filename by
    # taking the last N tokens leading up to the extension.
    for ext in _XLSX_RE.finditer(text):
        end = ext.end()
        start = ext.start()

        # Ensure we include the whole token that ends with ".xlsx".
        token_start = start
        k = start - 1
        while k >= 0:
            ch = text[k]
            if ch.isalnum() or ch in "_-().":
                token_start = k
                k -= 1
                continue
            break

        # Walk backwards to collect up to N whitespace-delimited tokens.
        token_limit = 14
        tokens = 0
        i = start - 1
        in_token = False
        while i >= 0:
            ch = text[i]
            if ch.isspace():
                if in_token:
                    tokens += 1
                    in_token = False
                    if tokens >= token_limit:
                        i += 1
                        break
            else:
                in_token = True
                # Stop at hard delimiters that are unlikely inside filenames.
                if ch in "<>\"'\\n\\t":
                    i += 1
                    break
            i -= 1
        else:
            i = 0

        if i > token_start:
            i = token_start
        window = text[i:end]
        # Trim leading punctuation/whitespace so we start at a token boundary.
        while window and not window[0].isalnum():
            window = window[1:]
        candidate = _normalize_workbook_name(window)
        # If the window contains multiple .xlsx mentions, keep only the last one.
        if candidate.lower().count(".xlsx") > 1:
            last = candidate.lower().rfind(".xlsx")
            # Find the token start before the last ".xlsx"
            j = candidate.rfind(" ", 0, last)
            candidate = candidate[j + 1 : last + 5]
            candidate = _normalize_workbook_name(candidate)

        # Final sanity: must end with .xlsx and contain at least one alpha/digit.
        if not candidate.lower().endswith(".xlsx"):
            continue
        if not any(c.isalnum() for c in candidate):
            continue

        counts[candidate] = counts.get(candidate, 0) + 1
    return counts


def pick_workbook_name(counts: dict[str, int]) -> WorkbookMatch | None:
    """Pick a single workbook name if unambiguous."""
    if not counts:
        return None
    items = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    top_name, top_count = items[0]
    if len(items) == 1:
        return WorkbookMatch(top_name, 1.0, [f"only_candidate(count={top_count})"])
    second_count = items[1][1]
    # Require clear separation to avoid false matches when multiple workbooks are discussed.
    if top_count >= max(3, second_count * 2):
        conf = min(0.95, 0.5 + (top_count - second_count) / max(top_count, 1))
        return WorkbookMatch(
            top_name,
            conf,
            [f"top_candidate(count={top_count})", f"second_candidate(count={second_count})"],
        )
    return None


def default_mapping_path(archive_root: Path | None = None) -> Path:
    root = archive_root or default_archive_root()
    return root / "mappings.json"


def load_mappings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_mappings(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def mapping_keys_for_idb(idb: IdbDatabasePaths) -> list[str]:
    # Use both sqlite path and site folder; either can be stable across runs.
    return [f"sqlite:{idb.sqlite}", f"site:{idb.site_folder}"]


def get_remembered_workbook_name(idb: IdbDatabasePaths, *, mapping_path: Path | None = None) -> str | None:
    mp = mapping_path or default_mapping_path()
    data = load_mappings(mp)
    for key in mapping_keys_for_idb(idb):
        entry = data.get(key) or {}
        name = entry.get("workbook_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def remember_workbook_name(
    idb: IdbDatabasePaths,
    workbook_name: str,
    *,
    mapping_path: Path | None = None,
    workbook_path: Path | None = None,
    reason: str | None = None,
) -> None:
    mp = mapping_path or default_mapping_path()
    data = load_mappings(mp)
    entry: dict[str, Any] = {
        "workbook_name": workbook_name,
    }
    if workbook_path is not None:
        entry["workbook_path"] = str(workbook_path.expanduser().resolve())
    if reason:
        entry["reason"] = reason
    for key in mapping_keys_for_idb(idb):
        data[key] = entry
    save_mappings(mp, data)

