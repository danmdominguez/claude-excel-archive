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
# → ~/Documents/ExcelArchive/.../journal/default/session.tape.md
```

See [docs/CLAUDE_FOR_EXCEL.md](docs/CLAUDE_FOR_EXCEL.md) for full usage (retention, daemon, peers, snip notes, OTEL comparison).

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
