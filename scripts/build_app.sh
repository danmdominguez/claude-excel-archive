#!/usr/bin/env bash
# Build Excel Archive.app and install to /Applications.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
APP_NAME="Excel Archive.app"
INSTALL_PATH="/Applications/${APP_NAME}"

# Remove Finder/resource-fork xattrs that break codesign ("detritus not allowed").
strip_macos_detritus() {
  local target="$1"
  [[ -e "$target" ]] || return 0
  xattr -cr "$target" 2>/dev/null || true
  find "$target" -name .DS_Store -delete 2>/dev/null || true
}

sign_app_bundle() {
  local app="$1"
  strip_macos_detritus "$app"
  # Drop PyInstaller's broken/partial signature before re-signing.
  codesign --remove-signature "$app" 2>/dev/null || true
  strip_macos_detritus "$app"
  if dot_clean -m "$app" >/dev/null 2>&1; then
    strip_macos_detritus "$app"
  fi
  if codesign --force --deep --sign - "$app"; then
    echo "codesign: ad-hoc signed $(basename "$app")"
    return 0
  fi
  echo "codesign: failed (app may still run locally)" >&2
  return 1
}

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -e ".[app,build]"
.venv/bin/pip install -q pyinstaller

rm -rf build dist

GIT_SHA=""
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || true)"
fi
export EXCEL_ARCHIVE_GIT_SHA="${GIT_SHA}"

# Strip xattrs from bundle inputs so PyInstaller/codesign do not inherit detritus.
strip_macos_detritus "${ROOT}/src"
strip_macos_detritus "${ROOT}/launcher.py"
for _rumps in .venv/lib/python*/site-packages/rumps; do
  [[ -d "${_rumps}" ]] && strip_macos_detritus "${_rumps}"
done

.venv/bin/pyinstaller "Excel Archive.spec" --noconfirm

if [[ ! -d "dist/${APP_NAME}" ]]; then
  echo "Build failed: dist/${APP_NAME} not found" >&2
  exit 1
fi

# LSUIElement + version come from Excel Archive.spec (avoid PlistBuddy — it adds Finder detritus).
sign_app_bundle "dist/${APP_NAME}"

echo "Installing to ${INSTALL_PATH} ..."
rm -rf "${INSTALL_PATH}"
ditto "dist/${APP_NAME}" "${INSTALL_PATH}"
sign_app_bundle "${INSTALL_PATH}"

echo ""
echo "Installed: ${INSTALL_PATH}"
echo "Launch:    open -a \"Excel Archive\""
echo "Note: Grant Full Disk Access to Excel Archive in System Settings if capture is empty."
