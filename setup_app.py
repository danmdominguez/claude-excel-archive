"""
Build menu-bar Excel Archive.app:

  pip install -e ".[app,build]"
  python setup_app.py py2app
"""

from setuptools import setup

APP = ["launcher.py"]

OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "Excel Archive",
        "CFBundleDisplayName": "Excel Archive",
        "CFBundleIdentifier": "com.dmd.excel-archive",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    },
    "packages": [
        "excel_archive",
        "typer",
        "rich",
        "click",
        "pygments",
        "markdown_it",
        "rumps",
    ],
    "includes": ["sqlite3"],
}

setup(
    name="Excel Archive",
    app=APP,
    options={"py2app": OPTIONS},
)
