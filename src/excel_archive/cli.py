"""CLI for excel-archive (Claude for Excel IndexedDB archiver)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .copy import default_snapshots_dir
from .idb_extract import extract_from_snapshot_dir, extract_from_sqlite, write_extract_artifact
from .merge import diff_export_vs_session, write_diff_report
from .paths import (
    WEBKIT_WEBSITE_DATA,
    discover_indexeddb_databases,
    pick_primary_database,
    workbook_forensic_history_dir,
    workbook_journal_dir,
    workbook_snapshots_dir,
    default_archive_root,
    archive_forensic_live_dir,
)
from .copy import SnapshotStyle, copy_database, copy_database_checkpoint, copy_database_rolling
from .journal import default_journal_dir, ingest_sqlite, rebuild_journal_from_snapshots
from .render_tape import export_json_to_tape, write_session_tape
from .watch import IdbWatcher
from .match_workbook import default_mapping_path
from .workbook_attribution import analyze_session_events, load_events_jsonl
from .workbook_migration import find_unsaved_roots, migrate_unsaved_to_path
from .active_workbook import resolve_active_workbook
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

    wb_name = workbook.name if workbook else None
    if workbook is not None:
        from .paths import workbook_forensic_live_dir

        history = dest or workbook_forensic_history_dir(workbook)
        path = copy_database_checkpoint(db, history, workbook_name=wb_name)
        copy_database_rolling(db, workbook_forensic_live_dir(workbook))
        console.print(f"[green]Checkpoint:[/green] {path}")
        console.print("[green]forensic/live[/green] updated")
    else:
        out_dir = dest or default_snapshots_dir()
        from .copy import copy_database as _copy_database

        path = _copy_database(db, out_dir, workbook_name=wb_name)
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
        help="Optional saved workbook path — .xlsx copies and forensic paths only (not journal routing)",
    ),
    copy_workbook_file: bool = typer.Option(
        True,
        "--copy-workbook/--no-copy-workbook",
        help="Copy saved workbook .xlsx into archive (skipped for unsaved books)",
    ),
    allow_multiple_watchers: bool = typer.Option(
        False,
        "--allow-multiple-watchers",
        help="Do not enforce single watch process (not recommended)",
    ),
    infer_workbook: bool = typer.Option(
        True,
        "--infer-workbook/--no-infer-workbook",
        help="Best-effort infer workbook filename from IDB blob (used for snapshot naming and tape title)",
    ),
    dest: Path | None = typer.Option(None, "--dest", help="Override output dir (per-poll mode only)"),
    snapshot_style: SnapshotStyle | None = typer.Option(
        None,
        "--snapshot-style",
        help="rolling=forensic/live + journal append; per-poll=legacy snapshot folders; off=journal only",
    ),
    no_journal: bool = typer.Option(False, "--no-journal", help="Only copy sqlite, skip JSONL journal"),
    session: str = typer.Option("default", "--session", help="Journal subdirectory name"),
) -> None:
    """Poll IndexedDB WAL changes; append journal + rolling forensic copy."""
    wb_name = workbook.name if workbook else None
    style: SnapshotStyle = "rolling"
    if snapshot_style is not None:
        style = snapshot_style
    elif workbook is not None:
        style = load_config_for_workbook(workbook).snapshot_style  # type: ignore[assignment]
    resolved_dest = dest or (workbook_snapshots_dir(workbook) if workbook and style == "per-poll" else None)
    watcher = IdbWatcher(
        interval_sec=interval,
        dest_root=resolved_dest or default_snapshots_dir(),
        journal=not no_journal,
        session_key=session,
        workbook_name=wb_name,
        infer_workbook=infer_workbook,
        workbook_path=workbook,
        copy_workbook_file=copy_workbook_file,
        snapshot_style=style,
        enforce_single_watcher=not allow_multiple_watchers,
    )
    db = watcher._resolve_db()
    if not db:
        console.print("[red]No Excel IndexedDB found.[/red]")
        raise typer.Exit(1)
    archive_root = default_archive_root()
    console.print(f"Watching {db.sqlite}")
    if style == "rolling":
        from .paths import workbook_forensic_live_dir

        live = workbook_forensic_live_dir(workbook) if workbook else archive_forensic_live_dir()
        console.print(f"  forensic  → {live}/IndexedDB.sqlite3 (rolling)")
    elif style == "per-poll":
        console.print(f"  snapshots → {watcher.dest_root}")
    if not no_journal:
        console.print(f"  journal   → {archive_root}/<_workbook_|_unsaved_>/journal/{session}/ (fan-out)")
        console.print("              One events.jsonl per IndexedDB chat record / workbook identity")
        if infer_workbook and workbook is None:
            console.print(f"  mappings  → {default_mapping_path()}")
    try:
        watcher.run_forever()
    except KeyboardInterrupt:
        console.print("\nStopped.")
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


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
    n = ingest_sqlite(
        db,
        journal_root=journal_root if workbook else None,
        session_key=session,
        workbook_name=wb_name,
        fan_out=workbook is None,
    )
    session_dir = journal_root / session
    console.print(f"[green]+{n}[/green] events → {session_dir / 'events.jsonl'}")
    tape = session_dir / "session.tape.md"
    if tape.is_file():
        console.print(f"[green]Tape:[/green] {tape}")


@app.command("migrate-workbook")
def migrate_workbook(
    workbook: Annotated[
        Path,
        typer.Argument(help="Saved workbook path (.xlsx) to migrate archive data into"),
    ],
    from_unsaved: str | None = typer.Option(
        None,
        "--from-unsaved",
        help="Previous unsaved Excel name (e.g. Book3). Default: only matching _unsaved_* folder",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen without moving files"),
    use_active: bool = typer.Option(
        False,
        "--use-active",
        help="Use active Excel workbook save transition (unsaved name → this path)",
    ),
) -> None:
    """Move `_unsaved_<name>/` archive tree to the encoded folder for a saved workbook path."""
    archive_root = default_archive_root()
    wb_path = workbook.expanduser()

    if use_active:
        active = resolve_active_workbook()
        if not active or not active.saved or active.path is None:
            console.print("[red]Active workbook is not saved or Excel is not running.[/red]")
            raise typer.Exit(1)
        if active.path.resolve() != wb_path.resolve():
            console.print(
                f"[yellow]Warning:[/yellow] active path {active.path} differs from argument {wb_path}"
            )
        # Cannot know previous unsaved name without watch state; require --from-unsaved
        if not from_unsaved:
            console.print("[red]--from-unsaved NAME is required with --use-active[/red]")
            raise typer.Exit(1)

    unsaved_name = from_unsaved
    if not unsaved_name:
        candidates = find_unsaved_roots(archive_root)
        if len(candidates) == 1:
            unsaved_name = candidates[0][0]
            console.print(f"[dim]Using only unsaved archive: {candidates[0][1].name}[/dim]")
        elif not candidates:
            console.print("[red]No _unsaved_* archive folders found. Use --from-unsaved Book3[/red]")
            raise typer.Exit(1)
        else:
            console.print("[red]Multiple unsaved archives; specify --from-unsaved[/red]")
            for name, folder in candidates:
                console.print(f"  - {name} → {folder}")
            raise typer.Exit(1)

    report = migrate_unsaved_to_path(
        unsaved_name,
        wb_path,
        archive_root=archive_root,
        dry_run=dry_run,
    )
    table = Table("field", "value")
    table.add_row("unsaved_name", report.unsaved_name)
    table.add_row("source", str(report.source_root))
    table.add_row("dest", str(report.dest_root))
    table.add_row("ok", str(report.ok))
    console.print(table)
    for note in report.notes:
        console.print(f"- {note}")
    if dry_run:
        console.print("[dim]Dry run — no files changed[/dim]")
    elif report.ok:
        console.print(
            f"[green]Done.[/green] Future ingest for '{unsaved_name}' routes to {report.dest_root.name}"
        )
    else:
        raise typer.Exit(1)


@app.command("analyze-session")
def analyze_session(
    source: Annotated[
        Path,
        typer.Argument(help="Journal session dir or events.jsonl"),
    ],
    local_workbook: str | None = typer.Option(
        None,
        "--local-workbook",
        help="Session local workbook filename (e.g. EF Shop Model DD.xlsx) for hint context",
    ),
    json_out: Path | None = typer.Option(
        None,
        "--json",
        help="Write full analysis JSON to this path",
    ),
) -> None:
    """Print peer registry, workbook_hint lane counts, and sample timeline."""
    if source.is_dir():
        events_path = source / "events.jsonl"
        if not events_path.is_file():
            events_path = source
    else:
        events_path = source

    events = load_events_jsonl(events_path) if events_path.suffix == ".jsonl" else []
    if not events and source.is_dir() and (source / "state.json").is_file():
        from .render_tape import load_events_from_session

        events = load_events_from_session(source)

    if not events:
        console.print(f"[red]No events found at[/red] {source}")
        raise typer.Exit(1)

    analysis = analyze_session_events(events, local_workbook=local_workbook)
    table = Table("workbook_hint", "count")
    for hint, count in sorted(analysis.hint_counts.items(), key=lambda x: (-x[1], x[0])):
        table.add_row(hint, str(count))
    console.print("[bold]Workbook hints[/bold]")
    console.print(table)

    lane_table = Table("lane", "count")
    for lane, count in sorted(analysis.lane_counts.items(), key=lambda x: (-x[1], x[0])):
        lane_table.add_row(lane, str(count))
    console.print("\n[bold]Lanes[/bold]")
    console.print(lane_table)

    if analysis.registry.agents:
        reg = Table("agent_id", "workbook")
        for agent_id, wb in sorted(analysis.registry.agents.items()):
            reg.add_row(agent_id, wb)
        console.print("\n[bold]Peer registry[/bold]")
        console.print(reg)

    if analysis.send_message_timeline:
        console.print("\n[bold]send_message timeline[/bold]")
        for item in analysis.send_message_timeline[:20]:
            console.print(
                f"  #{item['index']} → {item.get('agent_id')} "
                f"({item.get('workbook')}) hint={item.get('hint')}"
            )

    if analysis.sample_events:
        console.print("\n[bold]Sample attributed events[/bold]")
        for sample in analysis.sample_events:
            console.print(f"  {sample}")

    if json_out:
        json_out.write_text(json.dumps(analysis.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"\n[green]Wrote[/green] {json_out}")


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
    split_workbooks: bool = typer.Option(
        False,
        "--split-workbooks",
        help="Also write session.ef.tape.md and session.gp.tape.md filtered views",
    ),
) -> None:
    """Render LLM-optimized session.tape.md from export JSON or journal."""
    if source.is_dir() and (source / "state.json").is_file():
        out = write_session_tape(source, split_workbook_tapes=split_workbooks)
    elif source.suffix == ".json" and source.is_file():
        out = export_json_to_tape(source, output)
    else:
        console.print("[red]Expected export .json or journal session directory[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Wrote[/green] {out}")
    if split_workbooks and source.is_dir():
        for suffix in ("ef", "gp"):
            p = source / f"session.{suffix}.tape.md"
            if p.is_file():
                console.print(f"[green]Wrote[/green] {p}")


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
                "snapshot_style": "rolling",
                "retention": {
                    "keep_snapshot_dirs": 30,
                    "keep_forensic_checkpoints": 30,
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
