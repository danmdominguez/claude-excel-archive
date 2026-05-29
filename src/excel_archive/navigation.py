"""Archive navigation: find session tapes and generate archive dashboards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .index_md import generate_index_md, write_index_md
from .paths import default_archive_root


@dataclass(frozen=True)
class TapeRef:
    tape: Path
    tape_full: Path | None
    session: str
    workbook_root: Path | None
    updated_at: float

    @property
    def label(self) -> str:
        if self.workbook_root is None:
            return f"(archive) / {self.session}"
        return f"{self.workbook_root.name} / {self.session}"


def is_workbook_archive_root(path: Path) -> bool:
    """Encoded workbook folder under the archive root (_Users_…, _workbook_…, _unsaved_…)."""
    return path.is_dir() and path.name.startswith("_") and (
        (path / "journal").is_dir()
        or (path / "snapshots").is_dir()
        or (path / "forensic").is_dir()
    )


def workbook_display_name(encoded_folder: str) -> str:
    """Human label for an archive folder name."""
    if encoded_folder.startswith("_unsaved_"):
        return f"{encoded_folder.removeprefix('_unsaved_')} (unsaved)"
    if encoded_folder.startswith("_workbook_"):
        return encoded_folder.removeprefix("_workbook_")
    if encoded_folder.startswith("_Users_") or encoded_folder.startswith("_users_"):
        return encoded_folder.replace("_", "/").lstrip("/")
    return encoded_folder


def discover_workbook_roots(archive_root: Path | None = None) -> list[Path]:
    root = archive_root or default_archive_root()
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.iterdir() if is_workbook_archive_root(p)),
        key=lambda p: p.name,
    )


def _tape_mtime(session_dir: Path) -> float:
    candidates = [session_dir / "state.json", session_dir / "session.tape.md"]
    full = session_dir / "session.full.tape.md"
    if full.is_file():
        candidates.append(full)
    times = [p.stat().st_mtime for p in candidates if p.is_file()]
    return max(times) if times else 0.0


def iter_tapes(archive_root: Path | None = None) -> list[TapeRef]:
    """Collect all session.tape.md paths under the archive, newest first."""
    root = archive_root or default_archive_root()
    refs: list[TapeRef] = []

    legacy_journal = root / "journal"
    if legacy_journal.is_dir():
        for sess_dir in legacy_journal.iterdir():
            if not sess_dir.is_dir():
                continue
            tape = sess_dir / "session.tape.md"
            if tape.is_file():
                refs.append(
                    TapeRef(
                        tape=tape,
                        tape_full=sess_dir / "session.full.tape.md"
                        if (sess_dir / "session.full.tape.md").is_file()
                        else None,
                        session=sess_dir.name,
                        workbook_root=None,
                        updated_at=_tape_mtime(sess_dir),
                    )
                )

    for wb_root in discover_workbook_roots(root):
        journal = wb_root / "journal"
        if not journal.is_dir():
            continue
        for sess_dir in journal.iterdir():
            if not sess_dir.is_dir():
                continue
            tape = sess_dir / "session.tape.md"
            if tape.is_file():
                refs.append(
                    TapeRef(
                        tape=tape,
                        tape_full=sess_dir / "session.full.tape.md"
                        if (sess_dir / "session.full.tape.md").is_file()
                        else None,
                        session=sess_dir.name,
                        workbook_root=wb_root,
                        updated_at=_tape_mtime(sess_dir),
                    )
                )

    refs.sort(key=lambda r: r.updated_at, reverse=True)
    return refs


def find_latest_tape(archive_root: Path | None = None) -> TapeRef | None:
    tapes = iter_tapes(archive_root)
    return tapes[0] if tapes else None


def generate_archive_root_index_md(archive_root: Path | None = None) -> str:
    root = archive_root or default_archive_root()
    latest = find_latest_tape(root)
    tapes = iter_tapes(root)
    workbook_roots = discover_workbook_roots(root)

    lines: list[str] = []
    lines.append("# Excel archive")
    lines.append("")
    lines.append(f"- **Archive root**: `{root}`")
    lines.append(f"- **Generated**: {datetime.utcnow().isoformat()}Z")
    lines.append("")

    lines.append("## Layout (sort by name = sort by time)")
    lines.append("")
    lines.append("| Path | Role |")
    lines.append("|------|------|")
    lines.append("| `<workbook>/journal/<session>/events.jsonl` | **Append-only** event log (authoritative merge)") 
    lines.append("| `<workbook>/journal/<session>/session.tape.md` | Readable tape (regenerated from journal)") 
    lines.append("| `<workbook>/forensic/live/IndexedDB.sqlite3` | Rolling IDB image (overwritten each poll)") 
    lines.append("| `<workbook>/forensic/history/YYYYMMDD_HHMM_*_IndexedDB.sqlite3` | Optional checkpoints (flat files)") 
    lines.append("| `<workbook>/workbook/YYYYMMDD_HHMM_*_workbook.xlsx` | Workbook copies") 
    lines.append("| `<workbook>/snapshots/` | Legacy per-poll folders only (`--snapshot-style per-poll`)") 
    lines.append("")

    lines.append("## Most recently updated tape")
    lines.append("")
    if latest:
        ts = datetime.utcfromtimestamp(latest.updated_at).isoformat() + "Z"
        rel = latest.tape.relative_to(root)
        lines.append(f"- **`{ts}`** — [`{rel}`]({rel})")
        if latest.tape_full and latest.tape_full.is_file():
            rel_full = latest.tape_full.relative_to(root)
            lines.append(f"- Full tape: [`{rel_full}`]({rel_full})")
    else:
        lines.append("_No session tapes yet. Run `excel-archive watch --workbook /path/to/file.xlsx`._")
    lines.append("")

    if workbook_roots:
        lines.append("## Workbooks")
        lines.append("")
        for wb in workbook_roots:
            wb_tapes = [t for t in tapes if t.workbook_root == wb]
            if wb_tapes:
                t0 = wb_tapes[0]
                ts = datetime.utcfromtimestamp(t0.updated_at).isoformat() + "Z"
                rel = t0.tape.relative_to(root)
                label = workbook_display_name(wb.name)
                lines.append(f"- **{label}** — updated `{ts}` — [`{rel}`]({rel})")
            else:
                label = workbook_display_name(wb.name)
                lines.append(f"- **{label}** — _no journal yet_ (snapshots only)")
            idx = wb / "index.md"
            if idx.is_file():
                lines.append(f"  - Workbook index: [`{idx.relative_to(root)}`]({idx.relative_to(root)})")
        lines.append("")

    legacy = [t for t in tapes if t.workbook_root is None]
    if legacy:
        lines.append("## Legacy archive journal")
        lines.append("")
        lines.append(
            "Sessions captured before workbook routing was fixed live under `journal/` at the archive root."
        )
        lines.append("")
        for t in legacy[:5]:
            ts = datetime.utcfromtimestamp(t.updated_at).isoformat() + "Z"
            rel = t.tape.relative_to(root)
            lines.append(f"- `{ts}` — [`{rel}`]({rel})")
        lines.append("")

    lines.append("## Commands")
    lines.append("")
    lines.append("```bash")
    lines.append("excel-archive status          # show newest tape paths")
    lines.append("excel-archive open-latest     # open newest tape in default app")
    lines.append("excel-archive index-rebuild   # refresh this file")
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


def remove_legacy_latest_symlink(archive_root: Path | None = None) -> None:
    link = (archive_root or default_archive_root()) / "latest.md"
    if link.is_symlink() or link.is_file():
        link.unlink(missing_ok=True)


def write_archive_root_index(archive_root: Path | None = None) -> Path:
    root = archive_root or default_archive_root()
    root.mkdir(parents=True, exist_ok=True)
    remove_legacy_latest_symlink(root)
    out = root / "index.md"
    out.write_text(generate_archive_root_index_md(root), encoding="utf-8")
    return out


def refresh_archive_navigation(*, journal_root: Path | None = None) -> None:
    """Refresh workbook index (if applicable) and archive-root dashboard."""
    archive_root = default_archive_root()
    if journal_root is not None:
        wb_root = journal_root.parent
        if (wb_root / "journal").resolve() == journal_root.resolve():
            try:
                write_index_md(wb_root)
            except Exception:
                pass
    try:
        write_archive_root_index(archive_root)
    except Exception:
        pass
