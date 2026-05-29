# PyInstaller spec for Excel Archive menu bar app.
# Build: .venv/bin/pyinstaller "Excel Archive.spec"

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None

rumps_datas, rumps_binaries, rumps_hidden = collect_all("rumps")

_pkg_resources = [
    (str(Path("src/excel_archive/resources/menu_icon.png").resolve()), "excel_archive/resources"),
]

a = Analysis(
    ["launcher.py"],
    pathex=["src"],
    binaries=rumps_binaries,
    datas=rumps_datas + _pkg_resources,
    hiddenimports=[
        "excel_archive",
        "excel_archive.cli",
        "excel_archive.app",
        "typer",
        "rich",
        "click",
        "pygments",
        "markdown_it",
        "sqlite3",
        *rumps_hidden,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Excel Archive",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Excel Archive",
)

app = BUNDLE(
    coll,
    name="Excel Archive.app",
    icon=None,
    bundle_identifier="com.dmd.excel-archive",
    info_plist={
        "CFBundleName": "Excel Archive",
        "CFBundleDisplayName": "Excel Archive",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    },
)
