"""Install and manage a macOS LaunchAgent for excel-archive watch."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LaunchAgentSpec:
    label: str
    plist_path: Path


def launchagents_dir() -> Path:
    return Path.home() / "Library/LaunchAgents"


def build_plist(
    *,
    label: str,
    python_executable: Path,
    repo_root: Path,
    workbook: Path | None,
    session: str,
    interval: float,
    infer_workbook: bool,
    copy_workbook: bool,
    stdout_log: Path,
    stderr_log: Path,
) -> str:
    """
    Minimal LaunchAgent plist that runs:
      PYTHONPATH=src <python> -m excel_archive.cli watch ...
    """
    args = [
        str(python_executable),
        "-m",
        "excel_archive.cli",
        "watch",
        "--interval",
        str(interval),
        "--session",
        session,
    ]
    if workbook is not None:
        args.extend(["--workbook", str(workbook)])
    args.append("--infer-workbook" if infer_workbook else "--no-infer-workbook")
    args.append("--copy-workbook" if copy_workbook else "--no-copy-workbook")

    program_args = "".join(f"\n    <string>{_xml_escape(a)}</string>" for a in args)

    env = {"PYTHONPATH": "src"}
    env_xml = "".join(
        f"\n      <key>{_xml_escape(k)}</key>\n      <string>{_xml_escape(v)}</string>"
        for k, v in env.items()
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{_xml_escape(label)}</string>

  <key>WorkingDirectory</key>
  <string>{_xml_escape(str(repo_root))}</string>

  <key>ProgramArguments</key>
  <array>{program_args}
  </array>

  <key>EnvironmentVariables</key>
  <dict>{env_xml}
  </dict>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>{_xml_escape(str(stdout_log))}</string>

  <key>StandardErrorPath</key>
  <string>{_xml_escape(str(stderr_log))}</string>
</dict>
</plist>
"""


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def install_launchagent(plist_contents: str, *, label: str) -> LaunchAgentSpec:
    dest_dir = launchagents_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    plist_path = dest_dir / f"{label}.plist"
    plist_path.write_text(plist_contents, encoding="utf-8")
    return LaunchAgentSpec(label=label, plist_path=plist_path)


def launchctl_bootstrap(spec: LaunchAgentSpec) -> None:
    subprocess.run(
        ["launchctl", "bootstrap", "gui/%d" % _uid(), str(spec.plist_path)],
        check=False,
    )
    subprocess.run(["launchctl", "enable", f"gui/%d/{spec.label}" % _uid()], check=False)


def launchctl_kickstart(label: str) -> None:
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/%d/{label}" % _uid()], check=False)


def launchctl_unload(label: str) -> None:
    subprocess.run(["launchctl", "bootout", f"gui/%d/{label}" % _uid()], check=False)


def _uid() -> int:
    import os

    return os.getuid()

