"""Which kind of field has keyboard focus in another app (Linux accessibility, AT-SPI).

Used by Auto-Type so a password is only typed into a real password box
(<input type="password"> in a browser, a hidden-text entry in an app),
never into a user name or search field.

Browsers, GTK and Qt apps report focus changes on the accessibility bus.
Velkey listens for those events and remembers, per process, which element
was focused last. When it is time to type the password, that element is
checked again live: it must still be focused and be a password field.

All of this runs in a small helper process (python3 -m velkey.a11y), never
inside the Velkey window process: accessibility calls into another app can
block for seconds when that app is busy or has accessibility switched off,
and libatspi can abort the process. Velkey only asks the helper with a
time limit (FocusClient) and kills it if it stops answering.
"""
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

from gi.repository import Gio, GLib

try:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
except (ImportError, ValueError):
    Atspi = None

DEBUG = bool(os.environ.get("VELKEY_DEBUG"))


def log(*a):
    if DEBUG:
        print("[velkey a11y]", *a, file=sys.stderr, flush=True)


class FieldInfo:
    __slots__ = ("known", "password", "editable", "role", "why")

    def __init__(self, known, password=False, editable=False, role="", why=""):
        self.known, self.password, self.editable, self.role, self.why = known, password, editable, role, why

    def __repr__(self):
        return "<field known=%s password=%s editable=%s role=%s %s>" % (
            self.known, self.password, self.editable, self.role, self.why)


def bus_problem():
    """Why the accessibility bus cannot be used, or '' if it works.

    libatspi aborts the whole program (g_error) when it cannot connect, so
    the connection is tried here first with GIO, which fails gracefully."""
    addr = os.environ.get("AT_SPI_BUS_ADDRESS", "")
    if not addr:
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            addr = conn.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress", None,
                                  GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, 1500, None).unpack()[0]
        except Exception:
            return "the accessibility service (at-spi) is not running"
    try:
        c = Gio.DBusConnection.new_for_address_sync(
            addr, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
        c.close_sync(None)
    except Exception as e:
        log("bus check failed:", e)
        return ("the accessibility service is broken (its socket is missing). Fix it with:  "
                "systemctl --user restart at-spi-dbus-bus.service")
    return ""


class FocusTracker:
    def __init__(self, ignore_pid=0):
        self.ok = False
        self.error = ""
        self._last = {}  # pid -> (accessible, time)
        self._mypid = os.getpid()
        self._ignore = {self._mypid, ignore_pid}
        self._listener = None
        self._tried = 0
        self._start()

    def _start(self):
        """Connect to the accessibility bus (again). Safe to call repeatedly."""
        if self.ok:
            return
        self._tried = time.time()
        if Atspi is None:
            self.error = "the AT-SPI library (gir1.2-atspi-2.0) is not installed"
            return
        problem = bus_problem()
        if problem:
            self.error = problem
            log(problem)
            return
        try:
            Atspi.init()
            try:
                Atspi.set_timeout(800, -1)
            except Exception:
                pass
            self._listener = Atspi.EventListener.new(self._on_event)
            self._listener.register("object:state-changed:focused")
            self.ok = True
            log("listening for focus changes")
        except Exception as e:
            self.error = "accessibility bus not available (%s)" % e
            log(self.error)

    # ------------------------------------------------------------ events
    def _on_event(self, ev, *_):
        try:
            if not ev.detail1:
                return
            src = ev.source
            pid = src.get_process_id()  # asks the bus daemon, never ourselves
            if pid in self._ignore:
                return  # never query our own process from the main loop (deadlock)
            self._last[pid] = (src, time.time())
            if DEBUG:
                log("focus pid=%d role=%s" % (pid, src.get_role_name()))
        except Exception as e:
            log("event error", e)

    # ------------------------------------------------------------- query
    def _describe(self, acc):
        role = acc.get_role()
        states = acc.get_state_set()
        focused = states.contains(Atspi.StateType.FOCUSED)
        editable = states.contains(Atspi.StateType.EDITABLE)
        password = role == Atspi.Role.PASSWORD_TEXT
        name = acc.get_role_name()
        return focused, FieldInfo(True, password, editable or password, name)

    def _apps(self, pid):
        desktop = Atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            try:
                if app is not None and app.get_process_id() == pid:
                    yield app
            except Exception:
                continue

    def _why_unknown(self, pid):
        """Explain why no focused field was found for `pid`."""
        apps = list(self._apps(pid))
        if not apps:
            return "the app is not connected to the accessibility system; restart it"
        try:
            name = apps[0].get_name() or "the app"
            def real_children(x):
                return [c for c in (x.get_child_at_index(k) for k in range(x.get_child_count())) if c is not None]
            # Chromium with accessibility off: windows claim children but return none
            empty = all(not real_children(f) for a in apps for f in real_children(a))
        except Exception:
            name, empty = "the app", False
        if empty:
            return ("%s has its accessibility support switched off. For Chromium-based browsers start them "
                    "with --force-renderer-accessibility, or turn on chrome://accessibility → "
                    "Native accessibility API support" % name)
        return "%s does not report its focused field to the accessibility system" % name

    def _find_focused(self, pid, budget=0.6):
        """Look for the focused element of process `pid` directly (used when
        no focus event was seen, e.g. the field was clicked before Velkey started)."""
        end = time.time() + budget
        for app in self._apps(pid):
            stack = [app]
            seen = 0
            while stack and time.time() < end and seen < 6000:
                a = stack.pop()
                seen += 1
                try:
                    st = a.get_state_set()
                    if st.contains(Atspi.StateType.FOCUSED) and a is not app:
                        return a
                    # only descend into what is visible / active
                    if a is not app and not st.contains(Atspi.StateType.SHOWING):
                        continue
                    n = a.get_child_count()
                    if n > 400:
                        continue
                    for k in range(n - 1, -1, -1):
                        c = a.get_child_at_index(k)
                        if c is not None:
                            stack.append(c)
                except Exception:
                    continue
        return None

    def field(self, pid):
        """FieldInfo for the element that has keyboard focus in process `pid`.
        Main thread only."""
        if not self.ok and time.time() - self._tried > 5:
            self._start()  # the bus may have been fixed since Velkey started
        if not self.ok:
            return FieldInfo(False, why=self.error)
        if not pid or pid in self._ignore:
            return FieldInfo(False, why="the target window is unknown")
        got = self._last.get(pid)
        try:
            if got is not None:
                focused, info = self._describe(got[0])
                if focused:
                    log("field pid=%d %r" % (pid, info))
                    return info
            acc = self._find_focused(pid)
            if acc is not None:
                self._last[pid] = (acc, time.time())
                focused, info = self._describe(acc)
                log("field (search) pid=%d %r" % (pid, info))
                return info
        except Exception as e:
            log("query error", e)
            return FieldInfo(False, why="the app did not answer (%s)" % e)
        try:
            why = self._why_unknown(pid)
        except Exception:
            why = "the app does not report its focused field to the accessibility system"
        return FieldInfo(False, why=why)


# ---------------------------------------------------------------- helper side
def _serve(ignore_pid):
    """Answer 'field <pid>' lines on stdin with one JSON line each."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    tracker = FocusTracker(ignore_pid)
    loop = GLib.MainLoop()

    def on_input(fd, cond):
        line = sys.stdin.readline() if cond & GLib.IO_IN else ""
        if not line:
            loop.quit()  # Velkey exited
            return False
        parts = line.split()
        if len(parts) == 2 and parts[0] == "field":
            try:
                info = tracker.field(int(parts[1]))
            except Exception as e:
                info = FieldInfo(False, why="the accessibility check failed (%s)" % e)
            sys.stdout.write(json.dumps({k: getattr(info, k) for k in FieldInfo.__slots__}) + "\n")
            sys.stdout.flush()
        return True
    GLib.io_add_watch(sys.stdin.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, on_input)
    loop.run()


# ---------------------------------------------------------------- Velkey side
class FocusClient:
    """Velkey's handle on the helper process. Thread-safe; every call has a
    time limit, so a hanging app can never freeze Velkey."""

    def __init__(self):
        self._proc = None
        self._lines = None
        self._lock = threading.Lock()
        self._spawned = 0.0
        self.disabled = sys.platform == "darwin"  # AT-SPI is Linux-only; macOS uses macos.py
        if not self.disabled:
            self._spawn()

    def _spawn(self):
        self._spawned = time.time()
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        try:
            self._proc = subprocess.Popen([sys.executable, "-m", "velkey.a11y", str(os.getpid())],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                                          text=True, bufsize=1, close_fds=True)
        except OSError as e:
            log("could not start helper:", e)
            self._proc = None
            return
        lines = self._lines = queue.Queue()
        out = self._proc.stdout

        def read():
            for ln in out:
                lines.put(ln)
            lines.put(None)
        threading.Thread(target=read, daemon=True).start()

    def _kill(self):
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
                p.wait(1)
            except Exception:
                pass
            for f in (p.stdin, p.stdout):
                try:
                    f.close()
                except Exception:
                    pass

    def stop(self):
        self._kill()

    def field_from_thread(self, pid, timeout=2.0):
        """FieldInfo for the focused element of process `pid`. Any thread."""
        if self.disabled:  # macOS: ask the Accessibility API directly (no helper needed)
            from . import macos
            known, password, role = macos.focused_field(pid)
            return FieldInfo(known, password, known, role,
                             "" if known else "the app does not report its focused field")
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                if time.time() - self._spawned < 3:
                    return FieldInfo(False, why="the accessibility helper is not running")
                self._kill()
                self._spawn()
                if self._proc is None:
                    return FieldInfo(False, why="the accessibility helper could not start")
            try:
                while not self._lines.empty():  # drop a late answer to an earlier question
                    self._lines.get_nowait()
                self._proc.stdin.write("field %d\n" % int(pid or 0))
                self._proc.stdin.flush()
                ln = self._lines.get(timeout=timeout)
            except (queue.Empty, OSError, ValueError):
                ln = None
            if not ln:
                log("helper did not answer; restarting it")
                self._kill()
                self._spawned = 0.0
                return FieldInfo(False, why="the app did not answer the accessibility check in time")
            try:
                d = json.loads(ln)
                return FieldInfo(d["known"], d["password"], d["editable"], d["role"], d["why"])
            except Exception:
                return FieldInfo(False, why="bad answer from the accessibility helper")

    def wait_for_password_field(self, pid, timeout=1.0):
        """After a Tab, focus moves a moment later: poll until a password
        field is focused or the time is up. Worker thread."""
        end = time.time() + timeout
        info = self.field_from_thread(pid)
        while not (info.known and info.password) and time.time() < end:
            time.sleep(0.12)
            info = self.field_from_thread(pid)
        return info


if __name__ == "__main__":
    try:
        import ctypes
        ctypes.CDLL(None).prctl(15, b"velkey-a11y", 0, 0, 0)  # name shown in task managers
    except Exception:
        pass
    _serve(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
