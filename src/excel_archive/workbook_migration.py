"""Migrate archive folders when an unsaved workbook is saved to disk."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .active_workbook import ActiveWorkbook
from .paths import (
    default_archive_root,
    encode_unsaved_workbook,
    encode_workbook_path,
    workbook_root_dir,
    workbook_root_for_name,
)


ALIASES_FILENAME = "workbook_aliases.json"
META_FILENAME = "workbook.meta.json"
REDIRECT_FILENAME = "MIGRATED.json"


@dataclass
class MigrationReport:
    unsaved_name: str
    source_root: Path
    dest_root: Path
    moved: bool
    merged_sessions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.moved or bool(self.merged_sessions)


def aliases_path(archive_root: Path | None = None) -> Path:
    return (archive_root or default_archive_root()) / ALIASES_FILENAME


def load_aliases(archive_root: Path | None = None) -> dict[str, Any]:
    path = aliases_path(archive_root)
    if not path.is_file():
        return {"aliases": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_aliases(data: dict[str, Any], archive_root: Path | None = None) -> None:
    path = aliases_path(archive_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_alias_for_unsaved_name(
    unsaved_name: str,
    *,
    archive_root: Path | None = None,
) -> dict[str, Any] | None:
    data = load_aliases(archive_root)
    entry = (data.get("aliases") or {}).get(unsaved_name.strip())
    return entry if isinstance(entry, dict) else None


def resolve_workbook_root_for_name(
    workbook_name: str,
    *,
    archive_root: Path | None = None,
) -> Path:
    """
    Archive root directory for a workbook identity.

    After migration, unsaved Excel names (e.g. Book3) route to the saved path folder.
    """
    root = archive_root or default_archive_root()
    name = (workbook_name or "").strip()
    alias = get_alias_for_unsaved_name(name, archive_root=root)
    if alias:
        path_str = alias.get("workbook_path")
        if isinstance(path_str, str) and path_str.strip():
            return workbook_root_dir(Path(path_str).expanduser(), archive_root=root)
        encoded = alias.get("encoded_folder")
        if isinstance(encoded, str) and encoded.strip():
            return root / encoded
    return workbook_root_for_name(name, archive_root=root)


def journal_dir_for_workbook_name_resolved(
    workbook_name: str,
    *,
    archive_root: Path | None = None,
) -> Path:
    return resolve_workbook_root_for_name(workbook_name, archive_root=archive_root) / "journal"


def write_workbook_meta(
    workbook_root: Path,
    *,
    unsaved_name: str | None = None,
    workbook_path: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    meta: dict[str, Any] = {
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if unsaved_name:
        meta["unsaved_excel_name"] = unsaved_name
    if workbook_path:
        meta["workbook_path"] = str(workbook_path.expanduser().resolve())
    if extra:
        meta.update(extra)
    path = workbook_root / META_FILENAME
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        existing.update(meta)
        meta = existing
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def register_alias(
    unsaved_name: str,
    workbook_path: Path,
    *,
    archive_root: Path | None = None,
    source_folder: str | None = None,
) -> None:
    root = archive_root or default_archive_root()
    dest = workbook_root_dir(workbook_path, archive_root=root)
    data = load_aliases(root)
    aliases = data.setdefault("aliases", {})
    aliases[unsaved_name.strip()] = {
        "unsaved_name": unsaved_name.strip(),
        "workbook_path": str(workbook_path.expanduser().resolve()),
        "encoded_folder": dest.name,
        "source_folder": source_folder,
        "migrated_at": datetime.now(UTC).isoformat(),
    }
    save_aliases(data, root)


def _merge_journal_session(src_sess: Path, dest_sess: Path, *, dry_run: bool) -> int:
    """Append src session into dest; return number of new JSONL lines."""
    from .journal import JournalState, merge_events

    src_jsonl = src_sess / "events.jsonl"
    dest_jsonl = dest_sess / "events.jsonl"
    if not src_jsonl.is_file():
        return 0

    dest_sess.mkdir(parents=True, exist_ok=True)
    new_lines = 0
    src_events: list[dict[str, Any]] = []
    for line in src_jsonl.read_text(encoding="utf-8").splitlines():
        if line.strip():
            src_events.append(json.loads(line))

    if dry_run:
        return len(src_events)

    state = JournalState.load(dest_sess / "state.json")
    snapshot = "migration_merge"
    new_rows = merge_events(state, src_events, snapshot=snapshot)
    if new_rows:
        state.save(dest_sess / "state.json")
        with dest_jsonl.open("a", encoding="utf-8") as fh:
            for row in new_rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        new_lines = len(new_rows)

    from .render_tape import write_session_tape

    if state.messages or state.tool_uses or state.tool_results or state.snip_notes:
        write_session_tape(dest_sess, title=f"Session — {dest_sess.name}")
    return new_lines


def _merge_tree(src: Path, dest: Path, *, dry_run: bool) -> list[str]:
    """Merge src workbook archive tree into dest."""
    notes: list[str] = []
    if not src.is_dir():
        return notes

    dest.mkdir(parents=True, exist_ok=True)
    for child in sorted(src.iterdir()):
        if child.name in (REDIRECT_FILENAME, META_FILENAME):
            continue
        target = dest / child.name
        if child.is_dir():
            if child.name == "journal":
                for sess in child.iterdir():
                    if not sess.is_dir():
                        continue
                    dest_sess = target / sess.name
                    if not dest_sess.exists():
                        if not dry_run:
                            target.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(sess), str(dest_sess))
                        notes.append(f"moved journal/{sess.name}")
                    else:
                        n = _merge_journal_session(sess, dest_sess, dry_run=dry_run)
                        if n:
                            notes.append(f"merged journal/{sess.name} (+{n} events)")
                        elif not dry_run:
                            shutil.rmtree(sess, ignore_errors=True)
            elif not target.exists():
                if not dry_run:
                    shutil.move(str(child), str(target))
                notes.append(f"moved {child.name}/")
            else:
                for sub in child.iterdir():
                    sub_dest = target / sub.name
                    if not sub_dest.exists():
                        if not dry_run:
                            shutil.move(str(sub), str(sub_dest))
                        notes.append(f"moved {child.name}/{sub.name}")
        elif not target.exists():
            if not dry_run:
                shutil.copy2(child, target)
            notes.append(f"copied {child.name}")
    return notes


def _write_redirect(src_root: Path, dest_root: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    payload = {
        "migrated_to": str(dest_root),
        "encoded_folder": dest_root.name,
        "migrated_at": datetime.now(UTC).isoformat(),
    }
    (src_root / REDIRECT_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def migrate_unsaved_to_path(
    unsaved_name: str,
    workbook_path: Path,
    *,
    archive_root: Path | None = None,
    dry_run: bool = False,
) -> MigrationReport:
    """
    Move `_unsaved_<name>/` archive data to the encoded path folder for `workbook_path`.

    If the destination folder already exists, journal sessions are merged.
    """
    root = archive_root or default_archive_root()
    src = root / encode_unsaved_workbook(unsaved_name)
    dest = workbook_root_dir(workbook_path.expanduser(), archive_root=root)
    report = MigrationReport(
        unsaved_name=unsaved_name.strip(),
        source_root=src,
        dest_root=dest,
        moved=False,
    )

    if not src.is_dir():
        report.notes.append(f"no unsaved archive at {src}")
        return report

    if not workbook_path.expanduser().is_file() and not dry_run:
        report.notes.append(f"workbook file not found: {workbook_path}")
        return report

    if not dest.exists():
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        report.moved = True
        report.notes.append(f"renamed {src.name} → {dest.name}")
    else:
        report.notes.extend(_merge_tree(src, dest, dry_run=dry_run))
        if not dry_run:
            _write_redirect(src, dest)
            try:
                remaining = list(src.iterdir())
                if len(remaining) <= 1 and all(
                    p.name == REDIRECT_FILENAME for p in remaining
                ):
                    pass
                elif not any(p.name == "journal" for p in remaining):
                    shutil.rmtree(src, ignore_errors=True)
            except OSError:
                pass

    if not dry_run and (report.moved or report.notes):
        register_alias(
            unsaved_name,
            workbook_path,
            archive_root=root,
            source_folder=src.name,
        )
        write_workbook_meta(
            dest,
            unsaved_name=unsaved_name,
            workbook_path=workbook_path,
            extra={"migrated_from": src.name},
        )
        if report.moved:
            pass
        elif src.is_dir():
            _write_redirect(src, dest)

        from .navigation import refresh_archive_navigation

        refresh_archive_navigation(journal_root=dest / "journal")

    report.merged_sessions = [n for n in report.notes if "journal/" in n]
    return report


def try_migrate_on_save_transition(
    previous: ActiveWorkbook | None,
    current: ActiveWorkbook | None,
    *,
    archive_root: Path | None = None,
    dry_run: bool = False,
) -> MigrationReport | None:
    """
    When Excel transitions active workbook from unsaved → saved, migrate archive folder.

    Uses the *previous* unsaved name (e.g. Book3) even if the saved file name differs.
    """
    if previous is None or current is None:
        return None
    if previous.saved or not current.saved or current.path is None:
        return None
    if not previous.name.strip():
        return None

    src = (archive_root or default_archive_root()) / encode_unsaved_workbook(previous.name)
    if not src.is_dir():
        return None

    return migrate_unsaved_to_path(
        previous.name,
        current.path,
        archive_root=archive_root,
        dry_run=dry_run,
    )


def find_unsaved_roots(archive_root: Path | None = None) -> list[tuple[str, Path]]:
    """Return (unsaved_excel_name, folder) for each `_unsaved_*` directory."""
    root = archive_root or default_archive_root()
    out: list[tuple[str, Path]] = []
    if not root.is_dir():
        return out
    for p in sorted(root.iterdir()):
        if not p.is_dir() or not p.name.startswith("_unsaved_"):
            continue
        name = p.name.removeprefix("_unsaved_")
        meta_path = p / META_FILENAME
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                name = str(meta.get("unsaved_excel_name") or name)
            except json.JSONDecodeError:
                pass
        out.append((name, p))
    return out
