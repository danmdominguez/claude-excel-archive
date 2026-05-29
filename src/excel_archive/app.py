"""macOS menu bar controller for excel-archive (dev + py2app entry)."""

from __future__ import annotations

import subprocess
import threading
import traceback
from pathlib import Path

import rumps

from .active_workbook import resolve_active_workbook
from .navigation import find_latest_tape, refresh_archive_navigation
from .paths import default_archive_root
from .runtime import (
    cli_module_argv,
    is_pid_alive,
    python_executable,
    run_cli,
    stop_watch,
    watch_lock_pid,
)
from .watch_lock import default_watch_lock_path
from .resources import menu_icon_path
from .runtime import is_frozen
from .updater import (
    UpdateError,
    check_for_updates,
    discover_repo_root,
    local_version_label,
    run_update,
    set_repo_root,
)


def _notify(title: str, subtitle: str = "") -> None:
    try:
        rumps.notification("Excel Archive", title, subtitle)
    except Exception:
        pass


class ExcelArchiveApp(rumps.App):
    def __init__(self) -> None:
        icon = menu_icon_path()
        super().__init__(
            "Excel Archive",
            title=None,
            icon=str(icon) if icon else None,
            quit_button=None,
        )
        self._archive_root = default_archive_root()
        self._lock_path = default_watch_lock_path(self._archive_root)
        self._watch_proc: subprocess.Popen[str] | None = None
        self._status_item = rumps.MenuItem("Status: …", callback=None)

        self.menu = [
            self._status_item,
            None,
            rumps.MenuItem("Start Watching", callback=self.on_start_watch),
            rumps.MenuItem("Stop Watching", callback=self.on_stop_watch),
            None,
            rumps.MenuItem("Open Latest Session", callback=self.on_open_latest),
            rumps.MenuItem("Open Archive Folder", callback=self.on_open_archive),
            rumps.MenuItem("Rebuild Archive Index", callback=self.on_rebuild_index),
            None,
            rumps.MenuItem("Check for Updates…", callback=self.on_check_updates),
            rumps.MenuItem("Set Repo Path…", callback=self.on_set_repo_path),
            None,
            rumps.MenuItem("Permissions Help…", callback=self.on_permissions),
            None,
            rumps.MenuItem("Quit", callback=self.on_quit),
        ]

        self._refresh_status()
        self._timer = rumps.Timer(self._refresh_status, 15)

    def _watch_pid(self) -> int | None:
        if self._watch_proc is not None and self._watch_proc.poll() is None:
            return self._watch_proc.pid
        pid = watch_lock_pid(self._lock_path)
        if pid is not None and is_pid_alive(pid):
            return pid
        return None

    def _refresh_status(self, _=None) -> None:
        pid = self._watch_pid()
        latest = find_latest_tape(self._archive_root)
        active = resolve_active_workbook()
        parts: list[str] = []
        if pid:
            parts.append(f"watching (pid {pid})")
        else:
            parts.append("stopped")
        if active:
            parts.append(active.display[:40])
        if latest:
            parts.append(f"tape: {latest.workbook_root.name if latest.workbook_root else 'legacy'}/{latest.session}")
        parts.append(local_version_label(discover_repo_root()))
        self._status_item.title = " · ".join(parts) if parts else "Status: —"
        # Menu bar shows spreadsheet icon only (no title text).
        self.title = None

    def on_quit(self, _) -> None:
        self.on_stop_watch(_)
        rumps.quit_application()

    def on_start_watch(self, _) -> None:
        if self._watch_pid():
            _notify("Already watching")
            return

        def worker() -> None:
            try:
                import os

                env = os.environ.copy()
                src = Path(__file__).resolve().parents[1]
                prev = env.get("PYTHONPATH", "")
                env["PYTHONPATH"] = f"{src}{os.pathsep}{prev}" if prev else str(src)
                cmd = cli_module_argv(
                    "watch",
                    "--interval",
                    "5",
                    "--session",
                    "live",
                    "--no-copy-workbook",
                )
                self._watch_proc = subprocess.Popen(
                    cmd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                _notify("Watch started", "Fan-out to per-workbook journals")
                self._refresh_status()
            except Exception as exc:
                _notify("Start failed", str(exc)[:80])
                traceback.print_exc()

        threading.Thread(target=worker, daemon=True).start()

    def on_stop_watch(self, _) -> None:
        stopped = False
        if self._watch_proc is not None and self._watch_proc.poll() is None:
            self._watch_proc.terminate()
            try:
                self._watch_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._watch_proc.kill()
            stopped = True
            self._watch_proc = None
        if stop_watch(self._lock_path):
            stopped = True
        _notify("Watch stopped" if stopped else "Watch was not running")
        self._refresh_status()

    def on_open_latest(self, _) -> None:
        try:
            run_cli("open-latest")
        except Exception as exc:
            _notify("Open failed", str(exc)[:80])

    def on_open_archive(self, _) -> None:
        self._archive_root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(self._archive_root)], check=False)

    def on_rebuild_index(self, _) -> None:
        def worker() -> None:
            try:
                refresh_archive_navigation()
                run_cli("index-rebuild")
                _notify("Index rebuilt")
                self._refresh_status()
            except Exception as exc:
                _notify("Index failed", str(exc)[:80])

        threading.Thread(target=worker, daemon=True).start()

    def on_check_updates(self, _) -> None:
        def worker() -> None:
            try:
                repo = discover_repo_root()
                if repo is None:
                    raise UpdateError(
                        "No git repo found. Use Set Repo Path… or set EXCEL_ARCHIVE_REPO."
                    )
                status = check_for_updates(repo)
                if status.up_to_date:
                    rumps.alert("Up to date", status.summary)
                    _notify("Up to date", status.summary)
                    self._refresh_status()
                    return
                clicked = rumps.alert(
                    "Update available",
                    f"{status.summary}\n\nPull and reinstall now?",
                    ok="Update",
                    cancel="Cancel",
                )
                if clicked != 1000:  # NSAlertFirstButtonReturn (Update)
                    return
                self.on_stop_watch(None)
                _, post = run_update(check_only=False, frozen=is_frozen(), repo=repo)
                if post:
                    rumps.alert("Update complete", post)
                    _notify("Update complete", "Restart the app")
                self._refresh_status()
            except UpdateError as exc:
                rumps.alert("Update failed", str(exc))
                _notify("Update failed", str(exc)[:80])
            except Exception as exc:
                rumps.alert("Update failed", str(exc)[:500])
                _notify("Update failed", str(exc)[:80])

        threading.Thread(target=worker, daemon=True).start()

    def on_set_repo_path(self, _) -> None:
        def worker() -> None:
            try:
                proc = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'POSIX path of (choose folder with prompt "Select claude-excel-archive clone")',
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc.returncode != 0 or not proc.stdout.strip():
                    return
                chosen = Path(proc.stdout.strip())
                resolved = set_repo_root(chosen)
                rumps.alert("Repo path saved", str(resolved))
                _notify("Repo path saved", resolved.name)
                self._refresh_status()
            except UpdateError as exc:
                rumps.alert("Invalid repo", str(exc))
            except Exception as exc:
                rumps.alert("Failed", str(exc)[:500])

        threading.Thread(target=worker, daemon=True).start()

    def on_permissions(self, _) -> None:
        msg = (
            "Excel Archive reads Claude for Excel chat history from:\n\n"
            "  ~/Library/Containers/com.microsoft.Excel/...\n\n"
            "If capture stays empty, grant Full Disk Access to Terminal "
            "(dev) or to this app (after you build the .app):\n\n"
            "  System Settings → Privacy & Security → Full Disk Access\n\n"
            f"Archive root:\n  {self._archive_root}\n\n"
            f"Python: {python_executable()}"
        )
        rumps.alert("Permissions", msg)


def main() -> None:
    ExcelArchiveApp().run()


if __name__ == "__main__":
    main()
