#!/bin/sh
# Velkey launcher — runs Velkey straight from this folder, no install needed.
if [ "$(uname -s)" = "Darwin" ]; then   # macOS: GTK comes from Homebrew
  exec "$(cd "$(dirname "$0")" && pwd)/runmac.sh" "$@"
fi
HERE="$(dirname "$(readlink -f "$0")")"
[ -d "$HERE/velkey" ] || HERE="${VELKEY_HOME:-$HOME/.local/share/velkey}"
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"

# Wayland desktops take a window's icon from the .desktop file named after
# its app id. Until install.sh has been run there is none, so the taskbar
# shows a generic icon. Register a hidden one pointing at this folder
# (install.sh replaces it, ./install.sh --uninstall removes it).
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESK="$APPS/io.github.velkey.desktop"
if [ -f "$HERE/run.sh" ] && { [ ! -f "$DESK" ] || grep -q '^X-Velkey-Dev=true' "$DESK"; }; then
  ICON="$HERE/icons/hicolor/256x256/apps/velkey.png"
  [ -f "$ICON" ] || ICON="$HERE/icons/hicolor/scalable/apps/velkey.svg"
  if ! grep -qs "^Icon=$ICON\$" "$DESK"; then
    mkdir -p "$APPS"
    cat > "$DESK" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Velkey
Comment=KeePass-compatible password manager (running from source)
Exec=$HERE/run.sh %f
Icon=$ICON
Terminal=false
NoDisplay=true
StartupWMClass=io.github.velkey
X-Velkey-Dev=true
DESKTOP
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$APPS" 2>/dev/null
  fi
fi

exec /usr/bin/env python3 -m velkey "$@"
