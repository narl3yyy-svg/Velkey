#!/bin/bash
# Run Velkey on macOS (Apple silicon M1 or newer; Intel Macs work too).
#
# Uses Homebrew for GTK 3, PyGObject and OpenSSL 3. Missing packages are
# listed and installed only after you confirm.
#
#   ./runmac.sh                 start Velkey
#   ./runmac.sh file.kdbx       open a database
#
# Auto-Type, the global hot key and the tray icon are Linux-only for now.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PACKAGES=(gtk+3 pygobject3 adwaita-icon-theme openssl@3)

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "runmac.sh is for macOS. On Linux use ./run.sh"

# ---------------------------------------------------------------- Homebrew
BREW="$(command -v brew || true)"
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do   # Apple silicon, Intel
  [ -z "$BREW" ] && [ -x "$b" ] && BREW="$b"
done
if [ -z "$BREW" ]; then
  fail "Homebrew is needed to install GTK. Install it from https://brew.sh (one command in Terminal), then run ./runmac.sh again."
fi
eval "$("$BREW" shellenv)"
PREFIX="$("$BREW" --prefix)"

missing=()
for p in "${PACKAGES[@]}"; do
  "$BREW" list --formula "$p" >/dev/null 2>&1 || missing+=("$p")
done
if [ ${#missing[@]} -gt 0 ]; then
  say "Velkey needs these Homebrew packages: ${missing[*]}"
  printf 'Install them now? [Y/n] '
  read -r answer </dev/tty 2>/dev/null || read -r answer || answer=n
  case "$answer" in
    ""|y|Y|yes|YES) "$BREW" install "${missing[@]}" ;;
    *) fail "Not installed. Run:  brew install ${missing[*]}" ;;
  esac
fi

# ------------------------------------------- a Python that can import GTK
check='import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk'
PY=""
for c in "$PREFIX/bin/python3" "$PREFIX"/bin/python3.1[0-9]; do
  if [ -x "$c" ] && "$c" -c "$check" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || fail "Homebrew's Python cannot load GTK. Try:  brew reinstall pygobject3 gtk+3"

# ----------------------------------------------------------- environment
export VELKEY_LIBCRYPTO="$("$BREW" --prefix openssl@3)/lib/libcrypto.3.dylib"
[ -f "$VELKEY_LIBCRYPTO" ] || fail "OpenSSL 3 not found. Run:  brew install openssl@3"
# icons, GTK settings schemas and themes installed by Homebrew
export XDG_DATA_DIRS="$PREFIX/share${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}"
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"

"$PY" - <<'EOF' || fail "This OpenSSL has no Argon2 (needs 3.2 or newer). Run:  brew upgrade openssl@3"
import velkey.crypto as c
c.argon2(b"x", b"saltsaltsaltsalt", 1, 64, 1)
EOF

cd "$HERE"
exec "$PY" -m velkey "$@"
