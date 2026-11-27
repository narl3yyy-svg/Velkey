#!/bin/bash
# Install Velkey as a Mac app: ~/Applications/Velkey.app (just for you, no
# admin password). Afterwards open it from Spotlight, Launchpad or Finder;
# the downloaded folder is no longer needed.
#
#   ./installmac.sh               install or update
#   ./installmac.sh --uninstall   remove the app (databases and settings stay)
#
# Needs Homebrew; missing packages (GTK 3, PyGObject, OpenSSL 3, icons) are
# installed after you confirm.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APPS="$HOME/Applications"
APP="$APPS/Velkey.app"
BUNDLE_ID="io.github.velkey"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ✓\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "installmac.sh is for macOS. On Linux use ./install.sh"

is_ours() { [ -f "$1/Contents/Info.plist" ] && grep -q "<string>$BUNDLE_ID</string>" "$1/Contents/Info.plist"; }

remove_app() {
  if [ -e "$APP" ] && ! is_ours "$APP"; then
    fail "$APP exists but is not Velkey. Remove or rename it first."
  fi
  rm -rf "$APP"
}

case "${1:-}" in
  --uninstall)
    remove_app
    ok "Velkey removed. Your databases and settings (~/.config/velkey) were not touched."
    exit 0 ;;
  -h|--help)
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    exit 0 ;;
  "") ;;
  *) fail "Unknown option: $1 (use --uninstall or --help)" ;;
esac

# ------------------------------------------------------------ dependencies
# shellcheck source=tools/macos-env.sh
. "$SRC/tools/macos-env.sh"
velkey_mac_setup install || fail "$VELKEY_ENV_ERROR"
velkey_mac_check_openssl "$SRC" || fail "This OpenSSL has no Argon2 (needs 3.2 or newer). Run:  brew upgrade openssl@3"
ok "Homebrew packages: ${VELKEY_MAC_PACKAGES[*]}"
ok "Python: $PY"

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/velkey/__init__.py")"

# ------------------------------------------------------------- app bundle
say "Installing Velkey $VERSION to $APP"
mkdir -p "$APPS"
remove_app
RES="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$RES"
cp -R "$SRC/velkey" "$SRC/icons" "$RES/"
# Touch ID helper, built once at install time (needs Apple's command-line tools)
if velkey_mac_touchid "$SRC/tools/macos" "$APP/Contents/MacOS/velkey-touchid" && [ -n "${VELKEY_TOUCHID:-}" ]; then
  ok "Touch ID helper"
else
  say "Touch ID helper not built (Apple's command-line tools missing); Velkey works without it"
fi
cp "$SRC/tools/macos-env.sh" "$SRC/LICENSE" "$RES/"
find "$RES/velkey" -name '__pycache__' -prune -exec rm -rf {} +

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Velkey</string>
  <key>CFBundleDisplayName</key><string>Velkey</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Velkey</string>
  <key>CFBundleIconFile</key><string>Velkey</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>CFBundleDocumentTypes</key>
  <array>
    <dict>
      <key>CFBundleTypeName</key><string>KeePass Database</string>
      <key>CFBundleTypeExtensions</key><array><string>kdbx</string></array>
      <key>CFBundleTypeRole</key><string>Editor</string>
      <key>LSHandlerRank</key><string>Alternate</string>
    </dict>
  </array>
</dict>
</plist>
EOF

# the program Finder starts: set up Homebrew's GTK, then run Velkey
cat > "$APP/Contents/MacOS/Velkey" <<'EOF'
#!/bin/bash
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
. "$RES/macos-env.sh"
if ! velkey_mac_setup check; then
  msg="${VELKEY_ENV_ERROR//\"/\\\"}"
  /usr/bin/osascript -e "display alert \"Velkey cannot start\" message \"$msg\" as critical" >/dev/null 2>&1
  exit 1
fi
export PYTHONPATH="$RES"
[ -x "$(dirname "$0")/velkey-touchid" ] && export VELKEY_TOUCHID="$(cd "$(dirname "$0")" && pwd)/velkey-touchid"
export VELKEY_MAC_ICON="$RES/icons/hicolor/512x512/apps/velkey.png"

# Run through a copy of Homebrew Python's app program kept inside Velkey.app:
# macOS then names the app "Velkey" (menu bar, Dock, Accessibility settings)
# instead of "Python". Refreshed after a Python update; if the copy cannot load
# GTK for any reason, plain Homebrew Python is used as before.
OWN="$(cd "$(dirname "$0")" && pwd)/Velkey-python"
STAMP="$RES/.python-stamp"
SRC_INFO="$("$PY" -c 'import os, sys
p = os.path.join(sys.base_prefix, "Resources/Python.app/Contents/MacOS/Python")
print("%s %d %d" % (p, os.path.getsize(p), os.path.getmtime(p)) if os.path.isfile(p) else "")' 2>/dev/null)"
if [ -n "$SRC_INFO" ]; then
  if [ ! -x "$OWN" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$SRC_INFO" ]; then
    rm -f "$OWN" "$STAMP"
    if cp "${SRC_INFO%% *}" "$OWN" 2>/dev/null \
       && "$OWN" -c 'import gi; gi.require_version("Gtk", "3.0")' >/dev/null 2>&1; then
      printf '%s' "$SRC_INFO" > "$STAMP"
    else
      rm -f "$OWN"
    fi
  fi
  [ -x "$OWN" ] && PY="$OWN"
fi

cd "$HOME"
exec "$PY" -m velkey "$@"
EOF
chmod 755 "$APP/Contents/MacOS/Velkey"

# icon: build Velkey.icns from the PNGs (iconutil comes with macOS)
ICONSET="$(mktemp -d)/Velkey.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  one="$SRC/icons/hicolor/${s}x${s}/apps/velkey.png"
  two="$SRC/icons/hicolor/$((s * 2))x$((s * 2))/apps/velkey.png"
  if [ -f "$one" ]; then cp "$one" "$ICONSET/icon_${s}x${s}.png"; fi
  if [ -f "$two" ]; then cp "$two" "$ICONSET/icon_${s}x${s}@2x.png"; fi
done
if command -v iconutil >/dev/null 2>&1 && iconutil -c icns "$ICONSET" -o "$RES/Velkey.icns" 2>/dev/null; then
  ok "App icon"
else
  say "Could not build the app icon (iconutil missing); Velkey works without it"
fi
rm -rf "$(dirname "$ICONSET")"

"$PY" -m compileall -q "$RES/velkey" >/dev/null 2>&1 || true

# tell Finder / Launchpad about the new app
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[ -x "$LSREGISTER" ] && "$LSREGISTER" -f "$APP" >/dev/null 2>&1 || true
touch "$APP"

ok "Installed $APP"
echo
echo "  Open it:   Spotlight (⌘ Space) → type Velkey,  or:  open \"$APP\""
echo "  .kdbx files: right-click → Open With → Velkey"
echo "  Remove:    ./installmac.sh --uninstall"
