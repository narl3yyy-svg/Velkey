#!/usr/bin/env bash
# Velkey installer — installs for the current user (no root needed unless
# a missing system package has to be installed).
#
#   ./install.sh               install / update
#   ./install.sh --autostart   also start Velkey (minimized) at login
#   ./install.sh --uninstall   remove Velkey (your databases and settings are kept)
set -euo pipefail

APP_ID="io.github.velkey"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
PREFIX="$DATA_HOME/velkey"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
AUTOSTART_FILE="$CONFIG_HOME/autostart/$APP_ID.desktop"
HOTKEY_GNOME="<Primary><Alt><Shift>z"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  \033[38;5;111m•\033[0m %s\n' "$*"; }
ok()   { printf '  \033[38;5;78m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[38;5;214m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[38;5;203m✗\033[0m %s\n' "$*" >&2; exit 1; }

AUTOSTART=0
UNINSTALL=0
for a in "$@"; do
  case "$a" in
    --autostart) AUTOSTART=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) die "unknown option: $a" ;;
  esac
done

desktop_is() { [[ "${XDG_CURRENT_DESKTOP:-}" == *"$1"* ]]; }

gnome_remove_hotkey() {
  command -v gsettings >/dev/null 2>&1 || return 0
  local base="org.gnome.settings-daemon.plugins.media-keys"
  local path="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/velkey/"
  local list
  list="$(gsettings get $base custom-keybindings 2>/dev/null)" || return 0
  if [[ "$list" == *"$path"* ]]; then
    list="$(python3 -c "import ast,sys; l=ast.literal_eval(sys.argv[1].replace('@as ','')); l=[x for x in l if x!=sys.argv[2]]; print(str(l))" "$list" "$path")"
    gsettings set $base custom-keybindings "$list"
  fi
}

# ------------------------------------------------------------------ uninstall
if [[ $UNINSTALL == 1 ]]; then
  bold "Uninstalling Velkey"
  rm -rf "$PREFIX"
  rm -f "$BIN_DIR/velkey" "$APPS_DIR/$APP_ID.desktop" "$ICON_DIR/velkey.svg" "$AUTOSTART_FILE"
  rm -f "$DATA_HOME"/icons/hicolor/*x*/apps/velkey.png
  gnome_remove_hotkey || true
  if command -v qdbus6 >/dev/null 2>&1; then
    qdbus6 org.kde.kglobalaccel /kglobalaccel org.kde.KGlobalAccel.unregister velkey auto-type >/dev/null 2>&1 || true
  fi
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$APPS_DIR" || true
  ok "Removed. Your settings in $CONFIG_HOME/velkey and your databases were left untouched."
  exit 0
fi

bold "Installing Velkey"

# ------------------------------------------------------------- dependencies
missing=()
command -v python3 >/dev/null 2>&1 || missing+=("python3")
if command -v python3 >/dev/null 2>&1; then
  python3 - <<'EOF' 2>/dev/null || missing+=("gtk")
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Atspi", "2.0")
from gi.repository import Gtk, Atspi
EOF
fi

if (( ${#missing[@]} )); then
  warn "Missing: Python 3 GTK bindings (${missing[*]})"
  pkgs=""; pm=""
  if command -v apt-get >/dev/null 2>&1; then pm="sudo apt-get install -y"; pkgs="python3 python3-gi gir1.2-gtk-3.0 gir1.2-atspi-2.0"
  elif command -v dnf >/dev/null 2>&1; then pm="sudo dnf install -y"; pkgs="python3 python3-gobject gtk3 at-spi2-core"
  elif command -v pacman >/dev/null 2>&1; then pm="sudo pacman -S --needed --noconfirm"; pkgs="python python-gobject gtk3 at-spi2-core"
  elif command -v zypper >/dev/null 2>&1; then pm="sudo zypper install -y"; pkgs="python3 python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-3_0 typelib-1_0-Atspi-2_0"
  fi
  [[ -n "$pm" ]] || die "Please install Python 3 with PyGObject and GTK 3 using your package manager, then re-run."
  read -r -p "  Install them now with: $pm $pkgs ? [Y/n] " ans || ans="n"
  [[ "${ans:-y}" =~ ^[Yy]$ ]] || die "Cannot continue without these packages."
  $pm $pkgs
fi
ok "Python $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])') with GTK 3"

python3 - <<'EOF' || die "OpenSSL libcrypto 3.2+ (with Argon2) is required. Please update OpenSSL (package libssl3 / openssl-libs)."
import ctypes, ctypes.util
n = ctypes.util.find_library("crypto") or "libcrypto.so.3"
lib = ctypes.CDLL(n)
lib.EVP_KDF_fetch.restype = ctypes.c_void_p
lib.EVP_KDF_fetch.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
assert lib.EVP_KDF_fetch(None, b"ARGON2D", None), "no argon2"
EOF
ok "OpenSSL with Argon2 / AES / ChaCha20"

# --------------------------------------------------------------- copy files
mkdir -p "$PREFIX" "$BIN_DIR" "$APPS_DIR" "$ICON_DIR"
rm -rf "$PREFIX/velkey" "$PREFIX/icons"
cp -r "$SRC/velkey" "$PREFIX/velkey"
cp -r "$SRC/icons" "$PREFIX/icons"
find "$PREFIX/velkey" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
python3 -m compileall -q "$PREFIX/velkey" >/dev/null 2>&1 || true
cp "$SRC/icons/hicolor/scalable/apps/velkey.svg" "$ICON_DIR/velkey.svg"
for d in "$SRC"/icons/hicolor/*x*/apps; do
  size="$(basename "$(dirname "$d")")"
  mkdir -p "$DATA_HOME/icons/hicolor/$size/apps"
  cp "$d/velkey.png" "$DATA_HOME/icons/hicolor/$size/apps/velkey.png"
done
ok "Program files → $PREFIX"

cat > "$BIN_DIR/velkey" <<EOF
#!/bin/sh
export PYTHONPATH="$PREFIX\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m velkey "\$@"
EOF
chmod 755 "$BIN_DIR/velkey"
ok "Launcher → $BIN_DIR/velkey"

cat > "$APPS_DIR/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Velkey
GenericName=Password Manager
Comment=KeePass-compatible password manager
Exec=$BIN_DIR/velkey %f
Icon=velkey
Terminal=false
Categories=Utility;Security;
Keywords=keepass;kdbx;password;otp;totp;
MimeType=application/x-keepass2;
StartupNotify=true
StartupWMClass=io.github.velkey
Actions=AutoType;Lock;

[Desktop Action AutoType]
Name=Global Auto-Type
Exec=$BIN_DIR/velkey --auto-type

[Desktop Action Lock]
Name=Lock Workspace
Exec=$BIN_DIR/velkey --lock
EOF
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$APPS_DIR" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true
command -v xdg-mime >/dev/null 2>&1 && xdg-mime default "$APP_ID.desktop" application/x-keepass2 2>/dev/null || true
ok "Menu entry and .kdbx file association"

# ------------------------------------------------------------ global hot key
if desktop_is KDE; then
  ok "Global Auto-Type Ctrl+Alt+Shift+Z: registered by Velkey with KDE when it starts"
  info "(change it later in System Settings → Shortcuts → Velkey)"
elif desktop_is GNOME && command -v gsettings >/dev/null 2>&1; then
  base="org.gnome.settings-daemon.plugins.media-keys"
  path="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/velkey/"
  list="$(gsettings get $base custom-keybindings)"
  if [[ "$list" != *"$path"* ]]; then
    list="$(python3 -c "import ast,sys; l=ast.literal_eval(sys.argv[1].replace('@as ','')); l.append(sys.argv[2]); print(str(l))" "$list" "$path")"
    gsettings set $base custom-keybindings "$list"
  fi
  kb="$base.custom-keybinding:$path"
  gsettings set "$kb" name "Velkey Global Auto-Type"
  gsettings set "$kb" command "$BIN_DIR/velkey --auto-type"
  gsettings set "$kb" binding "$HOTKEY_GNOME"
  ok "Global Auto-Type Ctrl+Alt+Shift+Z added to GNOME keyboard shortcuts"
elif [[ "${XDG_SESSION_TYPE:-}" == "x11" ]]; then
  ok "Global Auto-Type Ctrl+Alt+Shift+Z: grabbed by Velkey directly (X11)"
else
  warn "Bind Ctrl+Alt+Shift+Z to the command:  $BIN_DIR/velkey --auto-type"
  info "in your desktop/compositor keyboard settings (e.g. Sway/Hyprland config)."
fi

# ---------------------------------------------------------------- autostart
if [[ $AUTOSTART == 0 && -t 0 && ! -f "$AUTOSTART_FILE" ]]; then
  read -r -p "  Start Velkey automatically at login (minimized, so global Auto-Type is ready)? [y/N] " ans || ans="n"
  [[ "${ans:-n}" =~ ^[Yy]$ ]] && AUTOSTART=1
fi
if [[ $AUTOSTART == 1 ]]; then
  mkdir -p "$(dirname "$AUTOSTART_FILE")"
  sed -e "s|^Exec=$BIN_DIR/velkey %f|Exec=$BIN_DIR/velkey --minimized|" "$APPS_DIR/$APP_ID.desktop" > "$AUTOSTART_FILE"
  ok "Autostart at login enabled"
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not in your PATH — add it to run 'velkey' from a terminal." ;;
esac

echo
bold "Done!  Launch Velkey from your application menu, or run:  velkey"
info "Wayland note: the first Auto-Type asks once to allow keyboard input (remote input portal)."
