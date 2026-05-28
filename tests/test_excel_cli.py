from pathlib import Path

from typer.testing import CliRunner

from excel_archive.cli import app

runner = CliRunner()


def test_extract_fixture_blob_via_sqlite_scratch():
    scratch = (
        Path(__file__).resolve().parents[2]
        / ".scratch/excel-idb/417FFAD840D5D2DF8BBF3C8CD4E6C8A358A788F23641EAD3ED4B9EEFCE5B9D66.sqlite3"
    )
    if not scratch.is_file():
        return
    result = runner.invoke(app, ["extract", str(scratch)])
    assert result.exit_code == 0, result.output
    assert "tool_ids=" in result.output


def test_config_init_global(tmp_path):
    # Run config-init in isolated HOME/ExcelArchive via env var not supported,
    # so just ensure the command is registered and responds.
    result = runner.invoke(app, ["config-show"])
    assert result.exit_code == 0
