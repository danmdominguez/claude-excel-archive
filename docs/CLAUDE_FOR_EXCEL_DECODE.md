# IndexedDB decode fidelity (excel-archive spike)

## Goal

Determine how much of Claude for Excel’s session state can be recovered from local IndexedDB without running the add-in.

## Storage layers

1. **SQLite `Records` table** — WebKit IndexedDB backing store (`objectStoreID` + binary `value`).
2. **String-embedded JSON** — Many chat snapshots embed literal `tool_use` / `tool_result` JSON in UTF-8 strings inside binary values.
3. **`snipArchive` in agent snapshot** — Often present as a key name with structured-clone wrapper bytes; archive payloads may be length-prefixed or UTF-16 (`\x00` between ASCII letters in devtools dumps).
4. **`results` object store** — When present (schema v11), keyed archive entries from `snippedResultsStore.exportAll()`.

## Phase 1: String extraction (implemented)

`excel_archive.decode.extract_strings_from_blob()`:

- Decodes as UTF-8 with errors ignored.
- Pulls `toolu_*` ids, `[id:…]` tags, `snipArchive` / `snipRegistrations` markers.
- Regex-extracts `"name":"get_cell_ranges"` tool_use blocks with non-empty `"input":{...}`.
- Counts `[snipped — context_snip applied]` markers.

**Empirical result** (May 2026, one EF Shop session):

- Export JSON: 133 tool uses with empty `input`, 277 snip markers.
- IDB chat blob: **~64%** of those tool ids had a fuller `input` elsewhere in the same blob.
- IDB had **0** snip markers in plain strings but **`snipArchive` keys present**.
- Full `tool_result` cell JSON: modest gain (~23 vs ~21 worksheet results) via strings alone.

## Phase 2: Structured clone (partial)

WebKit stores values in a **structured clone** binary format (not plain JSON). Full deserialization would require:

- Blink/WebKit deserializer, or
- Heuristic scanners for embedded JSON arrays (implemented as `extract_json_fragments()`).

`extract_json_fragments()` finds top-level `[{...}]` and `{...}` substrings that parse as JSON. Useful for isolated transcript arrays; fragile on nested braces inside strings.

## Phase 3: Not attempted here

- V8 value deserializer port
- Live `retrieve_snipped` API (requires active Excel session)
- Patching cached `pivot.claude.ai` bundle

## Recommendations

| Need | Approach |
|------|----------|
| Best local recovery | Run `excel-archive watch` during long sessions |
| Tool inputs after export | `excel-archive diff export.json` against latest snapshot |
| Full cell dumps | Prefer OTEL Enterprise + `tool.output_chars` or workbook snapshots |
| Legal/compliance audit | OTEL custom collector (official) |

## Code references

- [`src/excel_archive/decode.py`](../src/excel_archive/decode.py) — string + JSON fragment extractors
- [`src/excel_archive/merge.py`](../src/excel_archive/merge.py) — export diff logic
