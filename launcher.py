#!/usr/bin/env python3
"""App bundle entry: menu bar UI, or `--run-cli` for embedded excel-archive commands."""

from __future__ import annotations

import sys


def _run_cli_main() -> None:
    from excel_archive.cli import app

    sys.argv = ["excel-archive", *sys.argv[2:]]
    app()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--run-cli":
        _run_cli_main()
    else:
        from excel_archive.app import main

        main()
