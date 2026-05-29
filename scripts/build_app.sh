#!/usr/bin/env bash
# Build Excel Archive.app and install to /Applications.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
APP_NAME="Excel Archive.app"
INSTALL_PATH="/Applications/${APP_NAME}"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -e ".[app,build]"
.venv/bin/pip install -q pyinstaller

rm -rf build dist
.venv/bin/pyinstaller "Excel Archive.spec" --noconfirm

if [[ ! -d "dist/${APP_NAME}" ]]; then
  echo "Build failed: dist/${APP_NAME} not found" >&2
  exit 1
fi

# Ensure menu-bar-only (no Dock icon)
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "dist/${APP_NAME}/Contents/Info.plist" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Set :LSUIElement true" "dist/${APP_NAME}/Contents/Info.plist"

echo "Installing to ${INSTALL_PATH} ..."
rm -rf "${INSTALL_PATH}"
ditto "dist/${APP_NAME}" "${INSTALL_PATH}"

echo ""
echo "Installed: ${INSTALL_PATH}"
echo "Launch:    open -a \"Excel Archive\""
echo "Note: Grant Full Disk Access to Excel Archive in System Settings if capture is empty."
