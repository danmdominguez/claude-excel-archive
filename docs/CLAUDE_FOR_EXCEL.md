# Claude for Excel — docs, OTEL, and local archiving

Claude for Excel is an **Office add-in** (hosted taskpane), not claude.ai web chat. This repo provides **`excel-archive`** for Excel sessions on macOS.

## Official documentation

| Topic | Link |
|-------|------|
| Product overview | [Claude for Excel (office agents)](https://claude.com/docs/office-agents/excel) |
| Session logging (Claude Log sheet) | Help Center → enable in add-in settings |
| Context / compaction (user-facing) | Auto-compaction articles in Help Center |
| **Enterprise OTEL** | [Configure a custom OpenTelemetry collector for Office agents](https://support.claude.com/en/articles/14447276-configure-a-custom-opentelemetry-collector-for-office-agents) |

Anthropic does **not** publish a schema for the in-app “Export session log” JSON or for `context_snip` internals. Behavior below is from bundle reverse engineering — see [CLAUDE_FOR_EXCEL_ARCHITECTURE.md](CLAUDE_FOR_EXCEL_ARCHITECTURE.md).

## What success actually means (vs export diff)

The add-in **intentionally** drops history in two places:

1. **`context_snip`** — replaces ranges in the live transcript with `[snipped — context_snip applied]` and moves raw blocks into an internal archive (`results` store).
2. **Export session log** — downloads only the **current in-memory** transcript, not the archive.

**Success for you** is not “recover 64% of empty fields in an export file.” It is:

| Goal | Mechanism |
|------|-----------|
| Capture **while the session runs**, before or between snips | `excel-archive watch` (default **2s** poll) |
| Keep an **append-only history** that snip cannot rewrite | `~/Documents/ExcelArchive/journal/<session>/events.jsonl` |
| **First full copy wins** — later snipped replays are ignored | Journal merge policy in `journal.py` |
| Forensic backup of raw IDB | `forensic/live/IndexedDB.sqlite3` (rolling; overwritten) |
| Optional IDB checkpoints | `forensic/history/YYYYMMDD_HHMM_*_IndexedDB.sqlite3` (flat files; `excel-archive snapshot`) |
| Legacy per-poll folders | `snapshots/<timestamp>/` only with `--snapshot-style per-poll` |

Run during every long Excel session:

```bash
excel-archive watch --session ef-shop
```

After the fact, replay old snapshots into the journal:

```bash
excel-archive journal-rebuild --session ef-shop
```

### Primary output: Markdown tape (LLM-optimized)

Claude’s in-app export is **JSON** for download convenience. For forensics and LLM decomposition, this repo renders **`session.tape.md`** — chronological Markdown with role headings, snip callouts, and tool sections.

```bash
# From your existing Claude export:
excel-archive tape ~/Downloads/dan.m.dominguez-….json

# Live session (regenerated on each watch poll):
excel-archive watch --session ef-shop
# → ~/Documents/ExcelArchive/journal/ef-shop/session.tape.md
```

Give models **`session.tape.md`** (not raw JSONL). Keep `events.jsonl` as the internal append-only log.

### Two tapes: readable vs full

Each journal session writes both:

- `session.tape.md` — readable, **truncated** long tool payloads; full bodies are stored under `artifacts/`.
- `session.full.tape.md` — full-fidelity tape (still uses collapsible blocks).

### Matching workbook ↔ chat (filename vs full path)

- **Reliable to infer**: workbook **filename** (e.g. `EF Shop Model DD.xlsx`) because it often appears in snip summaries / narration.
- **Not reliably inferable**: the workbook’s **full local filesystem path**. Office.js typically exposes workbook *name*; the add-in does not consistently serialize absolute paths into IndexedDB.

Recommended usage:

```bash
# Always correct (best): explicit full path
excel-archive watch --workbook "/full/path/EF Shop Model DD.xlsx" --session main
# (also copies the workbook file itself into the archive by default)

# Best-effort: infer filename from IDB blob text (naming + tape title only)
excel-archive watch --infer-workbook --session main
```

When inference is enabled and a single `.xlsx` name dominates, `excel-archive` stores a remembered mapping in `~/Documents/ExcelArchive/mappings.json` keyed by the IndexedDB origin, so future snapshots can be named consistently.

### Run continuously (LaunchAgent)

If you want this running in the background (recommended for long sessions), install a LaunchAgent:

```bash
excel-archive daemon-install --workbook "/full/path/EF Shop Model DD.xlsx" --session main
```

Uninstall:

```bash
excel-archive daemon-uninstall
```

### Retention / rotation (recommended for daemon mode)

To prevent unbounded growth when `watch` runs continuously, use retention:

- Snapshots: keep last N snapshot dirs
- Workbook copies: keep last N `.xlsx` copies
- Sessions: keep last N journal session dirs
- Artifacts: cap per-session `artifacts/` size

Commands:

```bash
excel-archive retention-run --workbook "/full/path/EF Shop Model DD.xlsx" --dry-run
excel-archive retention-run --workbook "/full/path/EF Shop Model DD.xlsx"
```

Defaults are configurable via `excel-archive config-init --workbook ...`.

### index.md (workbook overview)

Each workbook root gets an `index.md` listing sessions, last updated time, snip-note count, and top tools.

### Peers section

Tapes include a `## Peers` section summarizing:\n- `send_message` calls by `agent_id`\n- conductor/connected-peers blocks\n\nThis helps reconstruct cross-workbook coordination even when the peer’s VM transcript isn’t locally accessible.

### Configuration

Initialize a config file:\n\n```bash\nexcel-archive config-init --workbook \"/full/path/EF Shop Model DD.xlsx\"\nexcel-archive config-show --workbook \"/full/path/EF Shop Model DD.xlsx\"\n```\n\nConfig lives at `<workbook_root>/excel-archive.json` and controls retention and tape truncation thresholds.

### What gets recorded (selective, not “everything”)

The journal is **not** a dump of the live transcript after snip. It keeps:

| Recorded | Skipped |
|----------|---------|
| Real tool calls (`get_cell_ranges`, `set_cell_range`, `execute_office_js`, …) with **non-empty input**, captured once | `context_snip` / `retrieve_snipped` as normal tools |
| Tool results with **full JSON/text** (first/longest wins) | `[snipped — context_snip applied]` placeholders |
| **`snip_note`** rows: `from_id`, `to_id`, `summary` (what Claude registered to compress) | Re-snipped shorter copies of the same tool result |

Example `snip_note` line in `events.jsonl`:

```json
{
  "kind": "snip_note",
  "from_id": "id:seghrs",
  "to_id": "id:6zyjqs",
  "summary": "Full IS/Rev/COGS diagnostic…",
  "status": "registered",
  "registration_tool_id": "toolu_01EFwhmP9q9F3SGUfj6TXfzK"
}
```

That is the **intent** of compression, not the compressed body. Bodies you already captured in earlier `tool_use` / `tool_result` rows stay authoritative.

### Where snip shows up in Claude’s data

| Location | What it tells you |
|----------|-------------------|
| `context_snip` **tool_use** | `from_id`, `to_id`, `summary` on user-message `[id:…]` tags (deferred until ~60% context) |
| **`agent.snipRegistrations`** in IDB snapshot | Same registrations (often structured-clone encoded beside `fromId` / `toId`) |
| Transcript after apply | `[snipped — context_snip applied]` + optional `Original range abc→xyz` breadcrumb |
| **`agent.snipArchive`** / `results` store | Archived raw blocks (binary; not replayed into the journal) |
| User **export JSON** | Snipped view only — registrations usually **missing** |

**Honest limits:** We cannot hook `executeTool` inside the WebView. If a snip runs **between** polls and the only copy left is archive bytes we cannot decode, that turn may still be lossy. Shorter `--interval` and enabling the add-in **Session logging** sheet reduce that window. Enterprise OTEL captures per-tool I/O at execution time but truncates output (audit, not a full workbook transcript).

## Two ways to intercept tool I/O

### Tier A — Local `excel-archive` (Pro / Max, macOS)

**When:** You want append-only captures from what Excel already writes to disk, including data **stripped from export JSON** after `context_snip`.

**How:**

```bash
excel-archive paths          # find WebKit IndexedDB.sqlite3
excel-archive watch          # poll WAL; copy to ~/Documents/ExcelArchive/snapshots/
excel-archive snapshot       # one-shot copy
excel-archive extract <dir>  # string extract from chats/results stores
excel-archive diff export.json [--idb <snapshot>]
```

**Captures:**

- IndexedDB database **`claude-chat-history`** (schema v11): stores **`chats`**, **`blobs`**, **`results`**
- Periodic `captureSnapshot()` payloads (transcript + `snipRegistrations` + `snipArchive` metadata)
- Pre-snip tool bodies in **`results`** when snip has been applied (decode fidelity limits: [CLAUDE_FOR_EXCEL_DECODE.md](CLAUDE_FOR_EXCEL_DECODE.md))

**Does not capture:**

- Live API traffic to Anthropic (no MITM)
- Remote peer agent VMs (`/agents/.../transcript.jsonl` paths in bash tool output)

Set `EXCEL_ARCHIVE_ROOT` to change the default `~/Documents/ExcelArchive` output tree.

### Tier B — Enterprise OpenTelemetry (official audit trail)

**When:** Org-wide, per-turn audit at execution time with a collector you control.

**Span hierarchy** (from Anthropic OTEL article, aligned with bundle telemetry names):

| Span | Meaning |
|------|---------|
| `agent.query` | User turn |
| `agent.stream` | Model streaming response |
| `agent.tool_execution` | Child per tool call |
| `agent.compaction` | Context summarized (related to snip/compaction, not identical to `context_snip` name) |

**Common attributes on `agent.tool_execution`:**

| Attribute | Content |
|-----------|---------|
| `tool.name` | e.g. `get_cell_ranges`, `set_cell_range`, `execute_office_js`, `context_snip`, `retrieve_snipped`, `send_message` |
| `tool.input` | Serialized tool input (truncated at emission) |
| `tool.output` | First **4000** characters of tool output |
| `tool.output_chars` | Full output length |
| `sheet.cells_read` / `sheet.cells_written` | Workbook I/O counts where applicable |

Configure via org admin, manifest `otlp_endpoint`, Entra, or bootstrap URL per the support article.

**Limitation:** OTEL is truncated at export from the add-in; it does not ship the full `snipArchive` blob. Use OTEL for **audit at execution time**; use **`excel-archive watch`** for **local forensic recovery** when snip has already run.

## Comparison

| | `excel-archive` | Enterprise OTEL |
|--|-----------------|-----------------|
| Requires | macOS, Excel WebKit IDB access | Org collector + admin setup |
| Timing | On each IDB write (after tools) | Per tool execution |
| Full cell JSON after snip | Partial (strings + `results` store) | No (4k output cap) |
| Empty export `tool_use.input` | Often recoverable from IDB | `tool.input` at call time |
| Multi-machine | Per Mac | Central collector |

## Complementary mitigations (no extra code)

1. **Session logging** → “Claude Log” worksheet tab in the workbook.
2. **Workbook version control** → formulas and structure survive transcript snip.
3. **Export JSON** → useful for sharing, **not** a full session dump.

## Related files in this repo

- [CLAUDE_FOR_EXCEL_ARCHITECTURE.md](CLAUDE_FOR_EXCEL_ARCHITECTURE.md) — pipeline, snip lifecycle, export vs `captureSnapshot`
- [CLAUDE_FOR_EXCEL_DECODE.md](CLAUDE_FOR_EXCEL_DECODE.md) — IndexedDB decode fidelity spike
- [`src/excel_archive/`](../src/excel_archive/) — implementation
