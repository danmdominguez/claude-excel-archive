"""CLI for excel-archive (Claude for Excel IndexedDB archiver)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .copy import copy_database, default_snapshots_dir
from .idb_extract import extract_from_snapshot_dir, extract_from_sqlite, write_extract_artifact
from .merge import diff_export_vs_session, write_diff_report
from .paths import (
    WEBKIT_WEBSITE_DATA,
    discover_indexeddb_databases,
    pick_primary_database,
    workbook_journal_dir,
    workbook_snapshots_dir,
    default_archive_root,
)
from .journal import default_journal_dir, ingest_sqlite, rebuild_journal_from_snapshots
from .render_tape import export_json_to_tape, write_session_tape
from .watch import IdbWatcher
from .match_workbook import default_mapping_path
from .daemon import (
    build_plist,
    install_launchagent,
    launchctl_bootstrap,
    launchctl_kickstart,
    launchctl_unload,
)
from .retention import enforce_retention_for_workbook_root
from .config_excel import load_config_for_workbook, load_global_config
from .navigation import (
    find_latest_tape,
    iter_tapes,
    refresh_archive_navigation,
    write_archive_root_index,
)

app = typer.Typer(
    name="excel-archive",
    help="Local archiver for Claude for Excel (IndexedDB snapshots on macOS).",
    no_args_is_help=True,
)
console = Console()


@app.command("paths")
def cmd_paths() -> None:
    """List discovered Excel WebKit IndexedDB database paths."""
    dbs = discover_indexeddb_databases()
    if not dbs:
        console.print(f"[yellow]No IndexedDB databases under[/yellow] {WEBKIT_WEBSITE_DATA}")
        raise typer.Exit(1)

    table = Table("sqlite", "wal_bytes", "sql_bytes")
    for db in dbs:
        wal_b = db.wal.stat().st_size if db.wal and db.wal.is_file() else 0
        sql_b = db.sqlite.stat().st_size if db.sqlite.is_file() else 0
        table.add_row(str(db.sqlite), str(wal_b), str(sql_b))
    console.print(table)

    primary = pick_primary_database(dbs)
    if primary:
        console.print(f"\n[green]Primary (by activity):[/green] {primary.sqlite}")


@app.command()
def snapshot(
    workbook: Path | None = typer.Option(
        None,
        "--workbook",
        help="Workbook path used to group output under an encoded folder name",
    ),
    dest: Path | None = typer.Option(
        None,
        "--dest",
        help="Snapshots directory (default: ~/Documents/ExcelArchive/snapshots)",
    ),
) -> None:
    """Copy the primary IndexedDB database once (sqlite + wal + shm)."""
    db = pick_primary_database()
    if not db:
        console.print("[red]No Excel IndexedDB found.[/red] Open Claude for Excel at least once.")
        raise typer.Exit(1)

    out_dir = dest or (workbook_snapshots_dir(workbook) if workbook else default_snapshots_dir())
    wb_name = workbook.name if workbook else None
    path = copy_database(db, out_dir, workbook_name=wb_name)
    console.print(f"[green]Snapshot:[/green] {path}")


@app.command()
def watch(
    interval: float = typer.Option(
        2.0,
        "--interval",
        "-i",
        help="Poll interval in seconds (lower = more chances to capture pre-snip state)",
    ),
    workbook: Path | None = typer.Option(
        None,
        "--workbook",
        help="Workbook path used to group output under an encoded folder name",
    ),
    copy_workbook_file: bool = typer.Option(
        True,
        "--copy-workbook/--no-copy-workbook",
        help="Copy the workbook .xlsx into the archive on changes (recommended for forensics)",
    ),
    infer_workbook: bool = typer.Option(
        True,
        "--infer-workbook/--no-infer-workbook",
        help="Best-effort infer workbook filename from IDB blob (used for snapshot naming and tape title)",
    ),
    dest: Path | None = typer.Option(None, "--dest", help="Snapshots directory"),
    no_journal: bool = typer.Option(False, "--no-journal", help="Only copy sqlite, skip JSONL journal"),
    session: str = typer.Option("default", "--session", help="Journal subdirectory name"),
) -> None:
    """Poll IndexedDB WAL changes; snapshot + render Markdown tape journal."""
    wb_name = workbook.name if workbook else None
    resolved_dest = dest or (workbook_snapshots_dir(workbook) if workbook else None)
    watcher = IdbWatcher(
        interval_sec=interval,
        dest_root=resolved_dest,
        journal=not no_journal,
        session_key=session,
        workbook_name=wb_name,
        infer_workbook=infer_workbook,
        workbook_path=workbook,
        copy_workbook_file=bool(workbook and copy_workbook_file),
    )
    db = watcher._resolve_db()
    if not db:
        console.print("[red]No Excel IndexedDB found.[/red]")
        raise typer.Exit(1)
    journal_dir = (workbook_journal_dir(workbook) if workbook else default_journal_dir()) / session
    console.print(f"Watching {db.sqlite}")
    console.print(f"  snapshots → {watcher.dest_root}")
    if not no_journal:
        console.print(f"  journal   → {journal_dir}/session.tape.md (primary)")
        console.print(f"              {journal_dir}/events.jsonl (merge log)")
        if infer_workbook and workbook is None:
            console.print(f"  mappings  → {default_mapping_path()}")
    try:
        watcher.run_forever()
    except KeyboardInterrupt:
        console.print("\nStopped.")


@app.command()
def extract(
    source: Annotated[
        Path,
        typer.Argument(help="Snapshot directory or IndexedDB.sqlite3 path"),
    ],
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write extract JSON (default: <source>/extract.json)",
    ),
) -> None:
    """Extract tool ids and string-recovered inputs from an IDB snapshot."""
    if source.is_dir():
        session = extract_from_snapshot_dir(source)
        out = output or source / "extract.json"
    elif source.suffix == ".sqlite3" and source.is_file():
        session = extract_from_sqlite(source)
        out = output or source.with_suffix(".extract.json")
    else:
        console.print("[red]Source must be a snapshot dir or .sqlite3 file[/red]")
        raise typer.Exit(1)

    write_extract_artifact(session, out)
    s = session.strings
    console.print(f"[green]Wrote[/green] {out}")
    console.print(
        f"tool_ids={len(s.tool_ids)} snip_markers={s.snip_markers} "
        f"snip_archive={s.has_snip_archive} inputs_recovered={len(s.tool_inputs)}"
    )


@app.command()
def diff(
    export_json: Annotated[
        Path,
        typer.Argument(help="Claude for Excel session export JSON"),
    ],
    idb: Path | None = typer.Option(
        None,
        "--idb",
        help="Snapshot dir or sqlite path (default: latest snapshot)",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Gap report JSON path",
    ),
) -> None:
    """Compare export JSON against an IDB extract; report recoverable tool inputs."""
    if not export_json.is_file():
        console.print(f"[red]Export not found:[/red] {export_json}")
        raise typer.Exit(1)

    if idb is None:
        snap_root = default_snapshots_dir()
        if not snap_root.is_dir():
            db = pick_primary_database()
            if not db:
                console.print("[red]No --idb and no snapshots; run snapshot or watch first.[/red]")
                raise typer.Exit(1)
            from .copy import snapshot_label

            idb = copy_database(db, snap_root, label=snapshot_label())
            console.print(f"[dim]Using fresh snapshot {idb}[/dim]")
        else:
            dirs = sorted(
                (p for p in snap_root.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not dirs:
                console.print(f"[red]No snapshots in {snap_root}[/red]")
                raise typer.Exit(1)
            idb = dirs[0]
            console.print(f"[dim]Using latest snapshot {idb}[/dim]")

    if idb.is_dir():
        session = extract_from_snapshot_dir(idb)
    else:
        session = extract_from_sqlite(idb)

    report = diff_export_vs_session(export_json, session)
    out = output or (export_json.parent / f"{export_json.stem}-idb-gap.json")
    write_diff_report(report, out)

    table = Table("metric", "value")
    d = report.to_dict()
    for key in (
        "export_tool_count",
        "idb_tool_count",
        "overlap",
        "only_in_idb",
        "export_empty_input_count",
        "recovered_input_count",
        "recovery_rate",
        "export_snip_markers",
        "idb_snip_markers",
    ):
        table.add_row(key, str(d.get(key, "")))
    console.print(table)
    console.print(f"[green]Report:[/green] {out}")


@app.command()
def journal_rebuild(
    snapshots: Path | None = typer.Option(
        None,
        "--snapshots",
        help="Snapshots directory (default: ~/Documents/ExcelArchive/snapshots)",
    ),
    workbook: Path | None = typer.Option(
        None,
        "--workbook",
        help="Workbook path used to group output under an encoded folder name",
    ),
    session: str = typer.Option("default", "--session", help="Journal subdirectory name"),
) -> None:
    """Replay existing snapshots (oldest first) into the append-only journal."""
    snap_root = snapshots or (workbook_snapshots_dir(workbook) if workbook else default_snapshots_dir())
    journal_root = workbook_journal_dir(workbook) if workbook else default_journal_dir()
    n = rebuild_journal_from_snapshots(snap_root, journal_root=journal_root, session_key=session)
    session_dir = journal_root / session
    tape = session_dir / "session.tape.md"
    console.print(f"[green]Wrote {n} events to[/green] {session_dir / 'events.jsonl'}")
    if tape.is_file():
        console.print(f"[green]Tape:[/green] {tape}")


@app.command()
def journal_ingest(
    source: Annotated[
        Path,
        typer.Argument(help="Snapshot directory or IndexedDB.sqlite3"),
    ],
    workbook: Path | None = typer.Option(
        None,
        "--workbook",
        help="Workbook path used to group output under an encoded folder name",
    ),
    session: str = typer.Option("default", "--session", help="Journal subdirectory name"),
) -> None:
    """Ingest one snapshot into the journal without copying again."""
    if source.is_dir():
        db = source / "IndexedDB.sqlite3"
    else:
        db = source
    journal_root = workbook_journal_dir(workbook) if workbook else default_journal_dir()
    wb_name = workbook.name if workbook else None
    n = ingest_sqlite(db, journal_root=journal_root, session_key=session, workbook_name=wb_name)
    session_dir = journal_root / session
    console.print(f"[green]+{n}[/green] events → {session_dir / 'events.jsonl'}")
    tape = session_dir / "session.tape.md"
    if tape.is_file():
        console.print(f"[green]Tape:[/green] {tape}")


@app.command()
def tape(
    source: Annotated[
        Path,
        typer.Argument(help="Claude export .json, journal session dir, or events.jsonl"),
    ],
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output .md path (default: alongside source)",
    ),
) -> None:
    """Render LLM-optimized session.tape.md from export JSON or journal."""
    if source.is_dir() and (source / "state.json").is_file():
        out = write_session_tape(source)
    elif source.suffix == ".json" and source.is_file():
        out = export_json_to_tape(source, output)
    else:
        console.print("[red]Expected export .json or journal session directory[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Wrote[/green] {out}")


@app.command()
def daemon_install(
    workbook: Path | None = typer.Option(
        None,
        "--workbook",
        help="Workbook path (optional). If provided, output is grouped under that workbook folder.",
    ),
    session: str = typer.Option("default", "--session", help="Journal subdirectory name"),
    interval: float = typer.Option(2.0, "--interval", help="Poll interval seconds"),
    infer_workbook: bool = typer.Option(
        True,
        "--infer-workbook/--no-infer-workbook",
        help="Best-effort infer workbook filename from IDB blob",
    ),
    copy_workbook_file: bool = typer.Option(
        True,
        "--copy-workbook/--no-copy-workbook",
        help="Copy workbook .xlsx into archive on changes (if --workbook set)",
    ),
    label: str = typer.Option(
        "com.dmd.excel-archive.watch",
        "--label",
        help="LaunchAgent label",
    ),
) -> None:
    """Install a LaunchAgent to run excel-archive watch in background."""
    repo_root = Path(__file__).resolve().parents[3]
    python_exe = Path.cwd() / ".venv/bin/python"
    if not python_exe.is_file():
        console.print("[red]Missing .venv python.[/red] Create venv and install deps first.")
        raise typer.Exit(1)

    logs_dir = (default_archive_root() / "logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = logs_dir / f"{label}.out.log"
    stderr_log = logs_dir / f"{label}.err.log"

    plist = build_plist(
        label=label,
        python_executable=python_exe,
        repo_root=repo_root,
        workbook=workbook,
        session=session,
        interval=interval,
        infer_workbook=infer_workbook,
        copy_workbook=bool(workbook and copy_workbook_file),
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )
    spec = install_launchagent(plist, label=label)
    launchctl_bootstrap(spec)
    launchctl_kickstart(label)
    console.print(f"[green]Installed:[/green] {spec.plist_path}")
    console.print(f"[green]Logs:[/green] {stdout_log} / {stderr_log}")


@app.command()
def daemon_uninstall(
    label: str = typer.Option("com.dmd.excel-archive.watch", "--label", help="LaunchAgent label"),
) -> None:
    """Uninstall (bootout) the LaunchAgent."""
    launchctl_unload(label)
    plist = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    if plist.is_file():
        plist.unlink()
        console.print(f"[green]Removed:[/green] {plist}")
    else:
        console.print(f"[yellow]Not found:[/yellow] {plist}")


@app.command()
def status(
    archive_root: Path | None = typer.Option(
        None,
        "--root",
        help="Archive root (default: ~/Documents/ExcelArchive)",
    ),
) -> None:
    """Show newest session tapes and where to open them."""
    from datetime import datetime

    root = archive_root or default_archive_root()
    latest = find_latest_tape(root)
    if latest is None:
        console.print(f"[yellow]No tapes under[/yellow] {root}")
        console.print("Run: excel-archive watch --workbook /path/to/model.xlsx")
        raise typer.Exit(1)

    console.print(f"[bold]Archive root[/bold] {root}")
    console.print(f"[green]Latest tape[/green] {latest.tape}")
    console.print(f"  Updated: {latest.updated_at:.0f}  Label: {latest.label}")
    link = root / "latest.md"
    if link.is_symlink():
        console.print(f"[green]Shortcut[/green] {link} → {link.readlink()}")

    table = Table("Updated (UTC)", "Tape", "Workbook folder")
    for ref in iter_tapes(root)[:12]:
        ts = datetime.utcfromtimestamp(ref.updated_at).strftime("%Y-%m-%d %H:%M")
        wb = ref.workbook_root.name if ref.workbook_root else "(legacy root journal)"
        table.add_row(ts, str(ref.tape.relative_to(root)), wb)
    console.print(table)
    console.print("\n[dim]Forensic: snapshots/ and workbook/ under each encoded folder.[/dim]")


@app.command("open-latest")
def cmd_open_latest(
    archive_root: Path | None = typer.Option(None, "--root", help="Archive root"),
    full: bool = typer.Option(False, "--full", help="Open session.full.tape.md instead"),
) -> None:
    """Open the newest session.tape.md in the default macOS app."""
    import subprocess
    import sys

    root = archive_root or default_archive_root()
    latest = find_latest_tape(root)
    if latest is None:
        console.print("[red]No session tape found.[/red]")
        raise typer.Exit(1)
    path = latest.tape_full if full and latest.tape_full else latest.tape
    if sys.platform != "darwin":
        console.print(path)
        raise typer.Exit(0)
    subprocess.run(["open", str(path)], check=False)
    console.print(f"[green]Opened[/green] {path}")


@app.command("index-rebuild")
def cmd_index_rebuild(
    archive_root: Path | None = typer.Option(None, "--root", help="Archive root"),
) -> None:
    """Regenerate archive index.md and latest.md symlink."""
    root = archive_root or default_archive_root()
    refresh_archive_navigation()
    out = write_archive_root_index(root)
    console.print(f"[green]Wrote[/green] {out}")
    link = root / "latest.md"
    if link.is_symlink():
        console.print(f"[green]Symlink[/green] latest.md → {link.readlink()}")


@app.command("retention-run-all")
def retention_run_all(
    dry_run: bool = typer.Option(False, "--dry-run", help="Only print what would be deleted"),
) -> None:
    """Apply retention to every workbook folder under the archive root."""
    from .navigation import discover_workbook_roots

    roots = discover_workbook_roots()
    legacy = default_archive_root() / "journal"
    if legacy.is_dir():
        console.print("[yellow]Note:[/yellow] legacy root journal/ is not pruned by this command.")
    if not roots:
        console.print("[yellow]No workbook archive folders found.[/yellow]")
        raise typer.Exit(0)
    for wb_root in roots:
        # Best-effort: find a workbook path from folder name is not possible; use global retention cfg.
        cfg = load_global_config().retention
        report = enforce_retention_for_workbook_root(wb_root, cfg=cfg, dry_run=dry_run)
        console.print(
            f"{wb_root.name}: deleted_files={report.deleted_files} "
            f"deleted_dirs={report.deleted_dirs}"
        )


@app.command()
def retention_run(
    workbook: Path | None = typer.Option(
        None,
        "--workbook",
        help="Workbook path used to locate workbook root folder",
    ),
    root: Path | None = typer.Option(
        None,
        "--root",
        help="Workbook root folder (encoded path folder). Overrides --workbook.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only print what would be deleted"),
) -> None:
    """Apply retention/rotation policy to a workbook archive root."""
    if root is None and workbook is None:
        console.print("[red]Provide --workbook or --root[/red]")
        raise typer.Exit(1)

    if root is None:
        from .paths import workbook_root_dir

        root = workbook_root_dir(workbook)  # type: ignore[arg-type]
        cfg = load_config_for_workbook(workbook).retention  # type: ignore[arg-type]
    else:
        cfg = load_global_config().retention

    report = enforce_retention_for_workbook_root(root, cfg=cfg, dry_run=dry_run)
    console.print(
        f"deleted_files={report.deleted_files} deleted_dirs={report.deleted_dirs} dry_run={dry_run}"
    )
    for n in report.notes:
        console.print(f"- {n}")


@app.command()
def config_init(
    workbook: Path | None = typer.Option(
        None,
        "--workbook",
        help="Workbook path to write <workbook_root>/excel-archive.json (preferred).",
    ),
    global_config: bool = typer.Option(
        False,
        "--global",
        help="Write ~/Documents/ExcelArchive/excel-archive.json (fallback defaults).",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite if file exists"),
) -> None:
    """Write a starter excel-archive.json configuration file."""
    if (workbook is None) == (not global_config):
        console.print("[red]Choose either --workbook or --global[/red]")
        raise typer.Exit(1)
    if workbook is not None:
        from .paths import workbook_root_dir

        root = workbook_root_dir(workbook)
        path = root / "excel-archive.json"
    else:
        root = default_archive_root()
        path = root / "excel-archive.json"

    if path.exists() and not force:
        console.print(f"[red]Config exists:[/red] {path} (use --force)")
        raise typer.Exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "retention": {
                    "keep_snapshot_dirs": 30,
                    "keep_workbook_copies": 20,
                    "keep_sessions": 20,
                    "max_artifacts_mb": 2048,
                },
                "tape": {
                    "truncate_tool_result_chars": 1200,
                    "truncate_tool_code_chars": 800,
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    console.print(f"[green]Wrote:[/green] {path}")


@app.command()
def config_show(
    workbook: Path | None = typer.Option(None, "--workbook", help="Workbook path"),
) -> None:
    """Show resolved config (workbook overrides global defaults)."""
    if workbook is None:
        cfg = load_global_config()
        console.print(json.dumps(cfg, default=lambda o: o.__dict__, indent=2))
        return
    cfg = load_config_for_workbook(workbook)
    console.print(json.dumps(cfg, default=lambda o: o.__dict__, indent=2))

if __name__ == "__main__":
    app()
