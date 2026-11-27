"""Application entry point: single instance, command line, global hot key,
tray icon and session lock monitoring."""
import os
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from . import autotype, theme  # noqa: E402
from .config import Config  # noqa: E402

VERSION = "1.0.0"
APP_ID = "io.github.velkey"


class VelkeyApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.version = VERSION
        self.config = None
        self.window = None
        self.hotkey = None
        self.tray = None
        self.typer = None
        self.probe = None
        self._start_minimized = False
        GLib.set_application_name("Velkey")
        GLib.set_prgname(APP_ID)  # = Wayland app id: must match the .desktop file name

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.config = Config()
        here = os.path.dirname(os.path.abspath(__file__))
        icons = os.path.join(os.path.dirname(here), "icons")
        if os.path.isdir(icons):
            Gtk.IconTheme.get_default().prepend_search_path(icons)
        Gtk.Window.set_default_icon_name("velkey")
        theme.apply(self.config.get("accent"))
        self.typer = autotype.AutoTyper(self.config)
        conn = self.get_dbus_connection()
        self.probe = autotype.WindowProbe(conn)
        self.hotkey = autotype.GlobalHotkey(conn, self.config.get("autotype_hotkey"), self._on_hotkey)
        self.hotkey.start()
        self._watch_screensaver(conn)

    def restart_hotkey(self):
        """Apply a hot key chosen in Options right away (no restart needed)."""
        conn = self.get_dbus_connection()
        if self.hotkey is not None:
            self.hotkey.stop()
        self.hotkey = autotype.GlobalHotkey(conn, self.config.get("autotype_hotkey"), self._on_hotkey)
        self.hotkey.start(force=True)
        self.config.set("autotype_hotkey", self.hotkey.spec)

    def _on_hotkey(self):
        if self.window is not None:
            self.window.global_autotype()

    def _watch_screensaver(self, conn):
        if conn is None:
            return

        def on_active(_c, _s, _p, _i, _sig, params):
            active = params.unpack()[0]
            if self.window is not None:
                self.window.on_screensaver(bool(active))
        for iface in ("org.freedesktop.ScreenSaver", "org.gnome.ScreenSaver"):
            conn.signal_subscribe(None, iface, "ActiveChanged", None, None, Gio.DBusSignalFlags.NONE, on_active)

    def update_tray(self):
        want = self.config.get("show_tray")
        if want and (self.tray is None or not self.tray.ok):
            from .tray import Tray
            self.tray = Tray(self._tray_toggle, [
                ("show", "Show / Hide Velkey", self._tray_toggle),
                ("autotype", "Global Auto-Type", lambda: self.window and self.window.global_autotype()),
                ("lock", "Lock / Unlock Workspace", lambda: self.window and self.window.toggle_lock()),
                ("sep", None, None),
                ("quit", "Exit", lambda: self.window and self.window.quit_app()),
            ])
            self.tray.start()
        elif not want and self.tray is not None:
            self.tray.stop()
            self.tray = None

    def _tray_toggle(self):
        if self.window is not None:
            self.window.toggle_visible()

    def notify(self, title, body):
        n = Gio.Notification.new(title)
        n.set_body(body)
        n.set_icon(Gio.ThemedIcon.new("velkey"))
        self.send_notification("velkey-msg", n)

    def do_activate(self):
        if self.window is None:
            self.window = __import__("velkey.window", fromlist=["MainWindow"]).MainWindow(self)
            self.update_tray()
            self.window.status_right.set_text(self.window._hotkey_text())
            if self._start_minimized or self.config.get("start_minimized"):
                if self.tray and self.tray.ok:
                    self.window.hide()
                else:
                    self.window.iconify()
                return
        self.window.show()
        self.window.present()

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        auto = "--auto-type" in args or "--autotype" in args
        lock = "--lock" in args
        if "--minimized" in args:
            self._start_minimized = True
        if "--version" in args:
            cmdline.print_literal("Velkey %s\n" % VERSION)
            return 0
        if "--help" in args or "-h" in args:
            cmdline.print_literal(
                "Usage: velkey [OPTIONS] [FILE.kdbx]\n\n"
                "  --auto-type   perform global Auto-Type in the running instance\n"
                "  --lock        lock the workspace of the running instance\n"
                "  --minimized   start minimized (to tray if available)\n"
                "  --version     show version\n")
            return 0
        files = [a for a in args if not a.startswith("--")]
        if auto and self.window is not None:
            self.window.global_autotype()
            return 0
        if lock and self.window is not None:
            self.window.lock(force=True)
            return 0
        first = self.window is None
        self.activate()
        if files:
            path = files[0]
            if path.startswith("file://"):
                path = Gio.File.new_for_uri(path).get_path()
            path = os.path.abspath(os.path.join(cmdline.get_cwd() or os.getcwd(), path))
            self.window.open_path(path)
        elif not first and not auto:
            self.window.show()
            self.window.present()
        return 0


def main():
    if "--version" in sys.argv[1:]:
        print("Velkey %s" % VERSION)
        return 0
    app = VelkeyApp()
    return app.run(sys.argv)
