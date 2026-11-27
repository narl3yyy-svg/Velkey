# Shared macOS setup for runmac.sh, installmac.sh and the installed Velkey.app.
# Source it from bash, then call:
#
#   velkey_mac_setup install   # offer to install missing Homebrew packages
#   velkey_mac_setup check     # only check (used by Velkey.app, no Terminal)
#
# On success it sets BREW, PREFIX and PY, and exports VELKEY_LIBCRYPTO and
# XDG_DATA_DIRS. On failure it returns 1 with the reason in VELKEY_ENV_ERROR.

VELKEY_MAC_PACKAGES=(gtk+3 pygobject3 adwaita-icon-theme openssl@3)

velkey_mac_setup() {
  local mode="${1:-check}" b p c answer
  VELKEY_ENV_ERROR=""

  # Homebrew (Apple silicon: /opt/homebrew, Intel: /usr/local). Finder starts
  # apps with a minimal PATH, so look in both places, not only on PATH.
  BREW="$(command -v brew 2>/dev/null || true)"
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -z "$BREW" ] && [ -x "$b" ]; then BREW="$b"; fi
  done
  if [ -z "$BREW" ]; then
    VELKEY_ENV_ERROR="Homebrew is needed to install GTK. Install it from https://brew.sh (one command in Terminal), then try again."
    return 1
  fi
  eval "$("$BREW" shellenv)"
  PREFIX="$("$BREW" --prefix)"

  local missing=()
  for p in "${VELKEY_MAC_PACKAGES[@]}"; do
    "$BREW" list --formula "$p" >/dev/null 2>&1 || missing+=("$p")
  done
  if [ ${#missing[@]} -gt 0 ]; then
    if [ "$mode" != install ]; then
      VELKEY_ENV_ERROR="Velkey needs these Homebrew packages: ${missing[*]}. Install them with:  brew install ${missing[*]}"
      return 1
    fi
    printf '\033[1;34m==>\033[0m Velkey needs these Homebrew packages: %s\n' "${missing[*]}"
    printf 'Install them now? [Y/n] '
    read -r answer </dev/tty 2>/dev/null || read -r answer || answer=n
    case "$answer" in
      ""|y|Y|yes|YES) "$BREW" install "${missing[@]}" || { VELKEY_ENV_ERROR="brew install failed"; return 1; } ;;
      *) VELKEY_ENV_ERROR="Not installed. Run:  brew install ${missing[*]}"; return 1 ;;
    esac
  fi

  # a Homebrew Python that can load GTK
  local check='import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk'
  PY=""
  for c in "$PREFIX/bin/python3" "$PREFIX"/bin/python3.1[0-9]; do
    if [ -x "$c" ] && "$c" -c "$check" >/dev/null 2>&1; then PY="$c"; break; fi
  done
  if [ -z "$PY" ]; then
    VELKEY_ENV_ERROR="Homebrew's Python cannot load GTK. Try:  brew reinstall pygobject3 gtk+3"
    return 1
  fi

  VELKEY_LIBCRYPTO="$("$BREW" --prefix openssl@3)/lib/libcrypto.3.dylib"
  if [ ! -f "$VELKEY_LIBCRYPTO" ]; then
    VELKEY_ENV_ERROR="OpenSSL 3 was not found. Run:  brew install openssl@3"
    return 1
  fi
  export VELKEY_LIBCRYPTO
  # icons, GTK settings schemas and themes installed by Homebrew
  case ":${XDG_DATA_DIRS:-}:" in
    *":$PREFIX/share:"*) ;;
    *) export XDG_DATA_DIRS="$PREFIX/share${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}" ;;
  esac
  return 0
}

# Argon2 needs OpenSSL 3.2+. $1 = folder that contains the velkey package.
velkey_mac_check_openssl() {
  PYTHONPATH="$1${PYTHONPATH:+:$PYTHONPATH}" "$PY" - <<'EOF' >/dev/null 2>&1
import velkey.crypto as c
c.argon2(b"x", b"saltsaltsaltsalt", 1, 64, 1)
EOF
}
