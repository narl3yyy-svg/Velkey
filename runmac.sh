#!/bin/bash
# Run Velkey on macOS (Apple silicon M1 or newer; Intel Macs work too),
# straight from this folder. To install it as an app, use ./installmac.sh
#
# Uses Homebrew for GTK 3, PyGObject and OpenSSL 3. Missing packages are
# listed and installed only after you confirm.
#
#   ./runmac.sh                 start Velkey
#   ./runmac.sh file.kdbx       open a database
#   ./runmac.sh --check-autotype   check Auto-Type step by step
#   ./runmac.sh --check-touchid    check whether Touch ID can be used, and why not
#
# The tray icon is Linux-only for now.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
fail() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "runmac.sh is for macOS. On Linux use ./run.sh"

# shellcheck source=tools/macos-env.sh
. "$HERE/tools/macos-env.sh"
velkey_mac_setup install || fail "$VELKEY_ENV_ERROR"
velkey_mac_check_openssl "$HERE" || fail "This OpenSSL has no Argon2 (needs 3.2 or newer). Run:  brew upgrade openssl@3"

velkey_mac_touchid "$HERE/tools/macos" "$HOME/Library/Application Support/Velkey/bin/velkey-touchid"
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
cd "$HERE"
if [ "${1:-}" = "--check-touchid" ]; then     # is Touch ID usable, and if not, why
  velkey_mac_touchid_report
  exit 0
fi
if [ "${1:-}" = "--check-autotype" ]; then   # step-by-step Auto-Type check
  shift
  exec "$PY" "$HERE/tools/mac_autotype_check.py" "$@"
fi
exec "$PY" -m velkey "$@"
