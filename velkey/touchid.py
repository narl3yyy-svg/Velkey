"""Touch ID unlock on macOS.

Velkey keeps the master password of a database in the macOS Keychain (login
keychain, this Mac only, readable only while the Mac is unlocked) and hands it
back after a fingerprint check. The work is done by the small Swift helper
tools/macos/velkey-touchid.swift, built by runmac.sh / installmac.sh; its path
arrives in $VELKEY_TOUCHID. Passwords only travel through pipes.

Without the helper (Linux, or no Apple command-line tools) nothing is offered.
"""
import hashlib
import os
import subprocess
import sys

_available = None


def helper():
    path = os.environ.get("VELKEY_TOUCHID", "")
    return path if sys.platform == "darwin" and path and os.access(path, os.X_OK) else ""


_reason = ""


def available():
    """Touch ID can be used on this Mac (sensor present and a finger enrolled)."""
    global _available, _reason
    if _available is None:
        h = helper()
        _available, _reason = False, ""
        if not h and sys.platform != "darwin":
            _reason = "Touch ID is only available on a Mac."
        elif not h:
            _reason = ("Velkey's small Touch ID helper is not built: Apple's command-line tools seem to be "
                       "missing. Install them in Terminal with  xcode-select --install  and then run "
                       "./installmac.sh (or ./runmac.sh) again.")
        else:
            try:
                r = subprocess.run([h, "available"], timeout=5, capture_output=True, text=True)
                _available = r.returncode == 0
                if not _available:
                    _reason = "Touch ID can't be used: " + (r.stdout.strip() or "macOS says it is not available") + "."
            except Exception as e:
                _reason = "The Touch ID helper did not run (%s)." % e
    return _available


def reason():
    """Why Touch ID is not available, in plain words ('' when it is)."""
    available()
    return _reason


def refresh():
    """Ask macOS again (e.g. after the user enrolled a fingerprint)."""
    global _available
    _available = None
    return available()


def account(db_path):
    """Keychain account name for a database (a hash: no file path in the Keychain)."""
    return "velkey-" + hashlib.sha256(os.path.abspath(db_path).encode()).hexdigest()[:32]


def store(db_path, password):
    try:
        return subprocess.run([helper(), "store", account(db_path)], input=password.encode(),
                              capture_output=True, timeout=15).returncode == 0
    except Exception:
        return False


STATUS = {0: "ok", 2: "cancelled", 3: "missing", 4: "unavailable"}


def unlock(db_path, reason):
    """Ask for the fingerprint. Returns (status, password or None); status is
    'ok', 'cancelled', 'missing' (nothing stored), 'unavailable' or 'error'.
    Blocks until the user answers: call it from a worker thread."""
    try:
        r = subprocess.run([helper(), "get", account(db_path), reason], capture_output=True, timeout=120)
    except Exception:
        return "error", None
    status = STATUS.get(r.returncode, "error")
    return status, (r.stdout.decode("utf-8") if status == "ok" else None)


def forget(db_path):
    try:
        subprocess.run([helper(), "delete", account(db_path)], capture_output=True, timeout=15)
    except Exception:
        pass
