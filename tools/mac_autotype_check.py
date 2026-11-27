#!/usr/bin/env python3
"""Check Auto-Type on macOS step by step.

Run it from the Velkey folder through runmac.sh's environment:

    ./runmac.sh --check-autotype           # permission, active window, hot key
    ./runmac.sh --check-autotype --type    # also type a test word

It never reads or types a password. The output shows the app, window title
and page address you switch to; remove anything private before sharing it.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from velkey import autotype, browser, macos  # noqa: E402

TEST_WORD = "velkey-test-123"


def line(ok, text):
    print(("  ✓ " if ok else "  ✗ ") + text, flush=True)


def countdown(what, seconds=5):
    for s in range(seconds, 0, -1):
        print("\r  %s in %d s… " % (what, s), end="", flush=True)
        time.sleep(1)
    print("\r" + " " * 60 + "\r", end="")


def main():
    if not macos.IS_MAC:
        print("This check is for macOS.")
        return 1
    print("\n1. Accessibility permission (needed to type and to read window titles)")
    trusted = macos.is_trusted()
    line(trusted, "granted" if trusted else "not granted yet: " + macos.PERMISSION_HELP)
    if not trusted:
        macos.is_trusted(prompt=True)

    print("\n2. Active window: switch to your browser (on a login page) now")
    countdown("Looking")
    title, app, pid = macos.frontmost()
    line(bool(app), "app: %s (pid %s)" % (app or "?", pid))
    line(bool(title) and title != app, "window title: %s" % (title or "(could not read it)"))
    url = browser.url_for_window(title) or macos.browser_url(app)
    line(bool(url), "page address: %s" % (url or "(not found: that is fine for apps that are not browsers)"))

    print("\n3. Focused field in that window")
    known, password, role = macos.focused_field(pid)
    if not known:
        line(True, "not reported (%s): Velkey types user name → Tab → password, like KeePass" % (role or "nothing"))
    else:
        line(True, "password box" if password else "a normal text field (%s)" % role)

    print("\n4. Global hot key")
    spec = "Ctrl+Alt+Shift+Z"
    mods, key = autotype.parse_hotkey(spec)
    got = []
    Gtk.init(sys.argv[:1])  # the hot key arrives through the app's event loop
    hk = macos.HotKey(lambda: (got.append(time.time()), Gtk.main_quit()))
    err = hk.register(*macos.hotkey_code(mods, key, autotype.NAMED_KEYS))
    if err:
        line(False, "could not register Control+Option+Shift+Z: %s" % err)
    else:
        print("  Press Control+Option+Shift+Z now (10 s)…", flush=True)
        GLib.timeout_add_seconds(10, Gtk.main_quit)
        Gtk.main()
        hk.unregister()
        line(bool(got), "hot key received" if got else "hot key NOT received")

    if "--type" in sys.argv:
        print("\n5. Typing: click into a text field (e.g. TextEdit) now")
        countdown("Typing %r" % TEST_WORD)
        typer = macos.MacTyper()
        if not macos.is_trusted():
            line(False, "skipped: " + macos.PERMISSION_HELP)
        else:
            events = autotype.flatten(autotype.parse_sequence(TEST_WORD, None), 10)
            typer.run(events)
            line(True, "typed %r: check that it appeared exactly like that" % TEST_WORD)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
