# claude-excel-archive

Local-first archiver for **Claude for Excel** sessions on macOS. Captures WebKit IndexedDB state before `context_snip` degrades the export, and renders human/LLM-readable session tapes.

Claude for Excel is an **Office add-in** (hosted taskpane), not claude.ai web chat.

## Install

```bash
git clone https://github.com/danmdominguez/claude-excel-archive.git
cd claude-excel-archive
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

CLI entry point: **`excel-archive`**

## Quick start

```bash
excel-archive watch --workbook "/full/path/My Model.xlsx" --copy-workbook
# → journal/default/events.jsonl (append-only) + session.tape.md + forensic/live/

excel-archive status          # newest tape by mtime
excel-archive open-latest     # open it
excel-archive snapshot --workbook ...   # optional flat checkpoint in forensic/history/
```

**Layout per workbook** (sort by folder/file name = chronological):

| Path | Purpose |
|------|---------|
| `journal/<session>/events.jsonl` | Append-only capture (primary) |
| `journal/<session>/session.tape.md` | Readable tape |
| `forensic/live/IndexedDB.sqlite3` | Rolling IDB copy (overwritten) |
| `forensic/history/YYYYMMDD_HHMM_*_IndexedDB.sqlite3` | Manual checkpoints only |
| `workbook/YYYYMMDD_HHMM_*_workbook.xlsx` | Workbook copies |

Legacy `snapshots/*_snapshot/` folders require `--snapshot-style per-poll`.

See [docs/CLAUDE_FOR_EXCEL.md](docs/CLAUDE_FOR_EXCEL.md) for full usage (retention, daemon, peers, snip notes, OTEL comparison).

### Menu bar (test before DMG)

```bash
./scripts/run_menu_bar_dev.sh          # from source
# or: pip install -e ".[app]" && excel-archive-app

./scripts/build_app.sh               # py2app → dist/Excel Archive.app
open "dist/Excel Archive.app"
```

Grant **Full Disk Access** to Terminal or the `.app` if capture is empty.

## Documentation

| Doc | Topic |
|-----|--------|
| [docs/CLAUDE_FOR_EXCEL.md](docs/CLAUDE_FOR_EXCEL.md) | User guide |
| [docs/CLAUDE_FOR_EXCEL_ARCHITECTURE.md](docs/CLAUDE_FOR_EXCEL_ARCHITECTURE.md) | Add-in pipeline, snip lifecycle |
| [docs/CLAUDE_FOR_EXCEL_DECODE.md](docs/CLAUDE_FOR_EXCEL_DECODE.md) | IndexedDB decode fidelity (best-effort) |

## Forensic sources

| Source | Role |
|--------|------|
| `~/Documents/ExcelArchive/.../snapshots/` | WAL-safe IndexedDB sqlite copies (authoritative) |
| Workbook `.xlsx` copies | Spreadsheet + Claude Log sheet |
| `events.jsonl` / `session.tape.md` | Append-only journal and readable tape (derived; best-effort decode) |

Set `EXCEL_ARCHIVE_ROOT` to change the default archive root.

## Requirements

- macOS with Microsoft Excel (Claude for Excel add-in)
- Python 3.11+
