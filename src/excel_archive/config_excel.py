"""Per-workbook configuration for excel-archive."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import default_archive_root, workbook_root_dir


@dataclass(frozen=True)
class RetentionConfig:
    keep_snapshot_dirs: int = 30  # legacy per-poll snapshot/ folders only
    keep_forensic_checkpoints: int = 30  # flat files in forensic/history/
    keep_workbook_copies: int = 20
    keep_sessions: int = 20
    max_artifacts_mb: int = 2048  # per session artifacts folder


@dataclass(frozen=True)
class TapeConfig:
    truncate_tool_result_chars: int = 1200
    truncate_tool_code_chars: int = 800


@dataclass(frozen=True)
class ExcelArchiveConfig:
    retention: RetentionConfig = RetentionConfig()
    tape: TapeConfig = TapeConfig()
    # rolling = forensic/live + append journal; per-poll = legacy snapshot dirs; off = journal only
    snapshot_style: str = "rolling"


def config_path_for_workbook(workbook_path: Path) -> Path:
    return workbook_root_dir(workbook_path) / "excel-archive.json"


def load_config_for_workbook(workbook_path: Path) -> ExcelArchiveConfig:
    path = config_path_for_workbook(workbook_path)
    if not path.is_file():
        return ExcelArchiveConfig()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return parse_config_dict(raw)


def parse_config_dict(raw: dict[str, Any]) -> ExcelArchiveConfig:
    r = raw.get("retention") or {}
    t = raw.get("tape") or {}
    retention = RetentionConfig(
        keep_snapshot_dirs=int(r.get("keep_snapshot_dirs", RetentionConfig.keep_snapshot_dirs)),
        keep_forensic_checkpoints=int(
            r.get("keep_forensic_checkpoints", RetentionConfig.keep_forensic_checkpoints)
        ),
        keep_workbook_copies=int(r.get("keep_workbook_copies", RetentionConfig.keep_workbook_copies)),
        keep_sessions=int(r.get("keep_sessions", RetentionConfig.keep_sessions)),
        max_artifacts_mb=int(r.get("max_artifacts_mb", RetentionConfig.max_artifacts_mb)),
    )
    snapshot_style = str(raw.get("snapshot_style", "rolling"))
    tape = TapeConfig(
        truncate_tool_result_chars=int(t.get("truncate_tool_result_chars", TapeConfig.truncate_tool_result_chars)),
        truncate_tool_code_chars=int(t.get("truncate_tool_code_chars", TapeConfig.truncate_tool_code_chars)),
    )
    return ExcelArchiveConfig(retention=retention, tape=tape, snapshot_style=snapshot_style)


def default_global_config_path() -> Path:
    return default_archive_root() / "excel-archive.json"


def load_global_config() -> ExcelArchiveConfig:
    path = default_global_config_path()
    if not path.is_file():
        return ExcelArchiveConfig()
    return parse_config_dict(json.loads(path.read_text(encoding="utf-8")))

