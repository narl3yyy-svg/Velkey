"""Auto-Type: KeePass-compatible keystroke sequences, active window detection,
keyboard emulation and the global hot key.

Backends (picked automatically):
  * typing   : X11 XTest (ctypes)  |  Wayland: xdg-desktop-portal RemoteDesktop
               (KDE, GNOME), falling back to `wtype` / `ydotool` if installed
  * window   : KWin scripting (KDE X11 + Wayland)  |  X11 _NET_ACTIVE_WINDOW
  * hot key  : KDE kglobalaccel (X11 + Wayland)  |  X11 XGrabKey
               anything else: bind `velkey --auto-type` (install.sh does GNOME)
"""
import ctypes
import ctypes.util
import datetime
import fnmatch
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse

import gi
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib  # noqa: E402

from . import browser, otp

DEFAULT_SEQUENCE = "{USERNAME}{TAB}{PASSWORD}{ENTER}"

SPECIAL_KEYS = {
    "TAB": "Tab", "ENTER": "Return", "SPACE": "space", "BS": "BackSpace", "BKSP": "BackSpace",
    "BACKSPACE": "BackSpace", "DEL": "Delete", "DELETE": "Delete", "INS": "Insert", "INSERT": "Insert",
    "HOME": "Home", "END": "End", "PGUP": "Prior", "PGDN": "Next", "UP": "Up", "DOWN": "Down",
    "LEFT": "Left", "RIGHT": "Right", "ESC": "Escape", "WIN": "Super_L", "LWIN": "Super_L",
    "RWIN": "Super_R", "APPS": "Menu", "CAPSLOCK": "Caps_Lock", "NUMLOCK": "Num_Lock",
    "SCROLLLOCK": "Scroll_Lock", "PRTSC": "Print", "BREAK": "Break", "HELP": "Help",
    "ADD": "KP_Add", "SUBTRACT": "KP_Subtract", "MULTIPLY": "KP_Multiply", "DIVIDE": "KP_Divide",
}
for _i in range(10):
    SPECIAL_KEYS["NUMPAD%d" % _i] = "KP_%d" % _i
for _i in range(1, 25):
    SPECIAL_KEYS["F%d" % _i] = "F%d" % _i
LITERALS = set("+^%~(){}[]")
MOD_CHARS = {"+": "shift", "^": "ctrl", "%": "alt", "@": "super"}


# --------------------------------------------------------------------------
# placeholders / field references
# --------------------------------------------------------------------------
_REF_RE = re.compile(r"\{REF:([TUPAN])@([TUPANI]):([^}]*)\}", re.I)
_FIELD_CODES = {"T": "Title", "U": "UserName", "P": "Password", "A": "URL", "N": "Notes"}


def resolve_refs(db, text, depth=0):
    """Resolve KeePass {REF:X@Y:value} field references."""
    if not text or "{REF:" not in text.upper() or depth > 10:
        return text

    def repl(m):
        want, where, val = m.group(1).upper(), m.group(2).upper(), m.group(3)
        for e in db.all_entries(include_recycle=True):
            if where == "I":
                import base64
                try:
                    if base64.b64decode(e.uuid).hex().upper() != val.upper():
                        continue
                except Exception:
                    continue
            elif val.lower() not in e.get(_FIELD_CODES[where]).lower():
                continue
            if want == "I":
                import base64
                return base64.b64decode(e.uuid).hex().upper()
            return resolve_refs(db, e.get(_FIELD_CODES[want]), depth + 1)
        return m.group(0)
    return _REF_RE.sub(repl, text)


def field_value(entry, name):
    """Value for a placeholder name (already upper-cased), or None."""
    db = entry.db
    std = {"TITLE": "Title", "USERNAME": "UserName", "PASSWORD": "Password", "URL": "URL", "NOTES": "Notes"}
    if name in std:
        return resolve_refs(db, entry.get(std[name]))
    if name.startswith("S:"):
        key = name[2:]
        for k, v, _p in entry.fields():
            if k.upper() == key:
                return resolve_refs(db, v)
        return ""
    if name in ("TOTP", "TIMEOTP"):
        return otp.current_code(entry)
    if name == "GROUP":
        p = entry.parent
        return p.name if p else ""
    if name == "GROUP_PATH":
        p = entry.parent
        return p.path() if p else ""
    if name == "UUID":
        import base64
        return base64.b64decode(entry.uuid).hex().upper()
    if name.startswith("URL:"):
        u = urllib.parse.urlparse(entry.url)
        part = name[4:]
        return {"RMVSCM": entry.url.split("://", 1)[-1], "SCM": u.scheme, "HOST": u.hostname or "",
                "PORT": str(u.port or ""), "PATH": u.path, "QUERY": u.query,
                "USERINFO": u.username or ""}.get(part)
    now = datetime.datetime.now()
    dt = {"DT_SIMPLE": now.strftime("%Y%m%d%H%M%S"), "DT_YEAR": now.strftime("%Y"),
          "DT_MONTH": now.strftime("%m"), "DT_DAY": now.strftime("%d"), "DT_HOUR": now.strftime("%H"),
          "DT_MINUTE": now.strftime("%M"), "DT_SECOND": now.strftime("%S")}
    if name in dt:
        return dt[name]
    return None


def parse_sequence(seq, entry):
    """Parse a KeePass auto-type sequence into a list of actions:
    ('text', str) | ('key', keysym_name, mods) | ('char', ch, mods) |
    ('delay', ms) | ('setdelay', ms) | ('clearfield',)"""
    out = []
    mods = set()
    i = 0
    n = len(seq)
    while i < n:
        c = seq[i]
        if c in MOD_CHARS:
            mods.add(MOD_CHARS[c])
            i += 1
            continue
        if c == "~":
            out.append(("key", "Return", frozenset(mods)))
            mods = set()
            i += 1
            continue
        if c == "(" and mods:
            j = seq.find(")", i)
            if j < 0:
                j = n
            for ch in seq[i + 1:j]:
                out.append(("char", ch, frozenset(mods)))
            mods = set()
            i = j + 1
            continue
        if c == "{":
            # literal braces: {{} and {}}
            if seq.startswith("{}}", i):
                out.append(("char", "}", frozenset(mods)))
                mods = set()
                i += 3
                continue
            j = seq.find("}", i + 1)
            if j < 0:
                out.append(("text", seq[i:]))
                break
            token = seq[i + 1:j]
            i = j + 1
            _token(token, entry, mods, out)
            mods = set()
            continue
        out.append(("char", c, frozenset(mods)))
        mods = set()
        i += 1
    return out


def _token(token, entry, mods, out):
    up = token.upper()
    if up.startswith("REF:"):
        out.append(("text", resolve_refs(entry.db, "{" + token + "}")))
        return
    val = field_value(entry, up) if entry is not None else None
    if val is not None:
        out.append(("text", val))
        return
    m = re.match(r"^(\S+?)(?:\s+|=)(\d+)$", token)
    name, count, is_eq = up, 1, False
    if m:
        name = m.group(1).upper()
        count = int(m.group(2))
        is_eq = "=" in token
    if name == "DELAY":
        out.append(("setdelay" if is_eq else "delay", count))
        return
    if name == "CLEARFIELD":
        out.append(("clearfield",))
        return
    if name in SPECIAL_KEYS:
        for _ in range(count):
            out.append(("key", SPECIAL_KEYS[name], frozenset(mods)))
        return
    if len(token.split()[0]) == 1 if token.split() else False:
        ch = token.split()[0]
        for _ in range(count):
            out.append(("char", ch, frozenset(mods)))
        return
    # unknown placeholder: ignore silently (KeePass would complain)


# --------------------------------------------------------------------------
# window matching
# --------------------------------------------------------------------------
def _wild(pattern, title):
    p = pattern.strip()
    if len(p) > 2 and p.startswith("//") and p.endswith("//"):
        try:
            return re.search(p[2:-2], title, re.I) is not None
        except re.error:
            return False
    return fnmatch.fnmatchcase(title.lower(), p.lower())


_SLD = {"co", "com", "org", "net", "ac", "gov", "edu"}


def _domain_hit(text, title_lower):
    """An entry titled 'google.com' (or with URL accounts.google.com) matches a
    browser window titled 'Google - Mozilla Firefox': compare the main domain
    label as a whole word."""
    if " " in text or "." not in text:
        return False
    host = text.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return False
    label = parts[-2]
    if label in _SLD and len(parts) >= 3:
        label = parts[-3]
    if len(label) < 3:
        return False
    return re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(label), title_lower) is not None


def _assoc_hit(pattern, title, url):
    p = (pattern or "").strip()
    if p[:7].upper() == "??:URL:":  # KeePass syntax: match the browser's URL
        return bool(url) and _wild(p[7:], url)
    return bool(title) and _wild(p, title)


def match_sequence(e, window_title, match_url=True, url=""):
    """The sequence to type if entry `e` belongs to the window titled
    `window_title`, else None. `url` is the page URL when the window is a
    browser and it could be found (see browser.py)."""
    title = window_title or ""
    if not title and not url:
        return None
    tl = title.lower()
    for w, s in e.associations():
        if w and _assoc_hit(w, title, url):
            return s or e.effective_sequence()
    if url and e.url and "://" in url:
        # the real URL is known: the entry's site must match it, a look-alike
        # page title is not enough (protects against phishing pages)
        return e.effective_sequence() if browser.site(e.url if "://" in e.url else "http://" + e.url) \
            == browser.site(url) else None
    t = e.title.strip().lower()
    if t and (t in tl or _domain_hit(t, tl)):
        return e.effective_sequence()
    if match_url and e.url:
        host = urllib.parse.urlparse(e.url if "://" in e.url else "http://" + e.url).hostname
        if host and (host.lower().removeprefix("www.") in tl or _domain_hit(host.lower(), tl)):
            return e.effective_sequence()
    return None


def find_matches(db, window_title, match_url=True, url=""):
    res = []
    if not window_title and not url:
        return res
    for e in db.all_entries():
        if not e.autotype_enabled:
            continue
        g = e.parent
        if g is not None and not g.effective("autotype_enabled"):
            continue
        seq = match_sequence(e, window_title, match_url, url)
        if seq is not None:
            res.append((e, seq))
    return res


def session_type():
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
        return "wayland"
    return "x11"


def is_kde():
    return "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()


def is_gnome():
    return "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()


# --------------------------------------------------------------------------
# X11 via ctypes (no python-xlib needed)
# --------------------------------------------------------------------------
class _X11:
    def __init__(self):
        self.x = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        x = self.x
        x.XInitThreads()
        x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XInternAtom.restype = ctypes.c_ulong
        x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x.XGetWindowProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long,
                                         ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
                                         ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
                                         ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                                         ctypes.POINTER(ctypes.c_void_p)]
        x.XFree.argtypes = [ctypes.c_void_p]
        x.XKeysymToKeycode.restype = ctypes.c_ubyte
        x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XkbKeycodeToKeysym.restype = ctypes.c_ulong
        x.XkbKeycodeToKeysym.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_int, ctypes.c_int]
        x.XFlush.argtypes = [ctypes.c_void_p]
        x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        x.XDisplayKeycodes.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        x.XGetKeyboardMapping.restype = ctypes.POINTER(ctypes.c_ulong)
        x.XGetKeyboardMapping.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        x.XChangeKeyboardMapping.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                             ctypes.POINTER(ctypes.c_ulong), ctypes.c_int]
        x.XGrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int]
        x.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        x.XPending.argtypes = [ctypes.c_void_p]
        x.XConnectionNumber.argtypes = [ctypes.c_void_p]
        x.XSelectInput.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long]
        x.XSetErrorHandler.restype = ctypes.c_void_p
        self._errh = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda d, e: 0)
        x.XSetErrorHandler(self._errh)
        self.xt = None
        try:
            self.xt = ctypes.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")
            self.xt.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        except OSError:
            self.xt = None

    def open(self):
        d = self.x.XOpenDisplay(None)
        if not d:
            raise OSError("cannot open X display")
        return d

    def prop(self, dpy, win, name, typ=0):
        atom = self.x.XInternAtom(dpy, name.encode(), 0)
        at, af, ni, ba, data = (ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong(),
                                ctypes.c_void_p())
        if self.x.XGetWindowProperty(dpy, win, atom, 0, 65536, 0, typ, ctypes.byref(at), ctypes.byref(af),
                                     ctypes.byref(ni), ctypes.byref(ba), ctypes.byref(data)) != 0:
            return None, 0, 0
        if not data.value:
            return None, 0, 0
        fmt, n = af.value, ni.value
        if fmt == 8:
            val = ctypes.string_at(data.value, n)
        elif fmt == 32:
            val = list((ctypes.c_ulong * n).from_address(data.value))
        else:
            val = None
        self.x.XFree(data)
        return val, fmt, n

    def active_window_title(self):
        dpy = self.open()
        try:
            root = self.x.XDefaultRootWindow(dpy)
            val, fmt, n = self.prop(dpy, root, "_NET_ACTIVE_WINDOW")
            if not val:
                return ""
            win = val[0]
            title, _, _ = self.prop(dpy, win, "_NET_WM_NAME")
            if not title:
                title, _, _ = self.prop(dpy, win, "WM_NAME")
            return title.decode("utf-8", "replace") if title else ""
        finally:
            self.x.XCloseDisplay(dpy)


_x11 = None


def x11():
    global _x11
    if _x11 is None:
        _x11 = _X11()
    return _x11


# --------------------------------------------------------------------------
# keystroke compilation (main thread) -> low level events (worker thread)
# --------------------------------------------------------------------------
def _char_keysym(ch):
    return Gdk.unicode_to_keyval(ord(ch))


def _name_keysym(name):
    return Gdk.keyval_from_name(name)


MOD_KEYSYMS = {"shift": "Shift_L", "ctrl": "Control_L", "alt": "Alt_L", "super": "Super_L"}


def flatten(actions, default_delay):
    """Turn parsed actions into [('press', keysym, mods) | ('sleep', s)]"""
    out = []
    delay = default_delay
    for a in actions:
        kind = a[0]
        if kind == "text":
            for ch in a[1]:
                if ch == "\n":
                    out.append(("press", _name_keysym("Return"), frozenset()))
                elif ch == "\r":
                    continue
                elif ch == "\t":
                    out.append(("press", _name_keysym("Tab"), frozenset()))
                else:
                    out.append(("press", _char_keysym(ch), frozenset()))
                out.append(("sleep", delay / 1000.0))
        elif kind == "char":
            ch = a[1]
            mods = a[2]
            ks = _char_keysym(ch.lower() if mods and ch.isalpha() else ch)
            out.append(("press", ks, mods))
            out.append(("sleep", delay / 1000.0))
        elif kind == "key":
            out.append(("press", _name_keysym(a[1]), a[2]))
            out.append(("sleep", delay / 1000.0))
        elif kind == "delay":
            out.append(("sleep", a[1] / 1000.0))
        elif kind == "setdelay":
            delay = a[1]
        elif kind == "clearfield":
            out.append(("press", _char_keysym("a"), frozenset(["ctrl"])))
            out.append(("sleep", delay / 1000.0))
            out.append(("press", _name_keysym("BackSpace"), frozenset()))
            out.append(("sleep", delay / 1000.0))
    return out


class TyperError(Exception):
    pass


class X11Typer:
    name = "X11 XTest"

    def available(self):
        try:
            return x11().xt is not None and bool(os.environ.get("DISPLAY"))
        except OSError:
            return False

    def prepare(self, cb):
        cb(True, None)

    def run(self, events):
        X = x11()
        x, xt = X.x, X.xt
        dpy = X.open()
        try:
            self._wait_modifiers(dpy, X)
            mn, mx = ctypes.c_int(), ctypes.c_int()
            x.XDisplayKeycodes(dpy, ctypes.byref(mn), ctypes.byref(mx))
            scratch = self._find_scratch(dpy, X, mn.value, mx.value)
            modkc = {m: x.XKeysymToKeycode(dpy, _name_keysym(n)) for m, n in MOD_KEYSYMS.items()}
            lvl3 = x.XKeysymToKeycode(dpy, _name_keysym("ISO_Level3_Shift"))
            for ev in events:
                if ev[0] == "sleep":
                    time.sleep(ev[1])
                    continue
                _, ks, mods = ev
                kc = x.XKeysymToKeycode(dpy, ks)
                extra = []
                remapped = False
                if kc:
                    if x.XkbKeycodeToKeysym(dpy, kc, 0, 0) == ks:
                        pass
                    elif x.XkbKeycodeToKeysym(dpy, kc, 0, 1) == ks:
                        extra.append(modkc["shift"])
                    elif lvl3 and x.XkbKeycodeToKeysym(dpy, kc, 0, 2) == ks:
                        extra.append(lvl3)
                    elif lvl3 and x.XkbKeycodeToKeysym(dpy, kc, 0, 3) == ks:
                        extra += [modkc["shift"], lvl3]
                    else:
                        kc = 0
                if not kc and scratch:
                    arr = (ctypes.c_ulong * 2)(ks, ks)
                    x.XChangeKeyboardMapping(dpy, scratch, 2, arr, 1)
                    x.XSync(dpy, 0)
                    time.sleep(0.01)
                    kc = scratch
                    remapped = True
                if not kc:
                    continue
                held = [modkc[m] for m in mods if modkc.get(m)] + extra
                for k in held:
                    xt.XTestFakeKeyEvent(dpy, k, 1, 0)
                xt.XTestFakeKeyEvent(dpy, kc, 1, 0)
                xt.XTestFakeKeyEvent(dpy, kc, 0, 0)
                for k in reversed(held):
                    xt.XTestFakeKeyEvent(dpy, k, 0, 0)
                x.XFlush(dpy)
                if remapped:
                    x.XSync(dpy, 0)
                    time.sleep(0.01)
            if scratch:
                arr = (ctypes.c_ulong * 2)(0, 0)
                x.XChangeKeyboardMapping(dpy, scratch, 2, arr, 1)
            x.XSync(dpy, 0)
        finally:
            x.XCloseDisplay(dpy)

    @staticmethod
    def _find_scratch(dpy, X, mn, mx):
        per = ctypes.c_int()
        m = X.x.XGetKeyboardMapping(dpy, mn, mx - mn + 1, ctypes.byref(per))
        if not m:
            return 0
        try:
            for kc in range(mx, mn - 1, -1):
                base = (kc - mn) * per.value
                if all(m[base + i] == 0 for i in range(per.value)):
                    return kc
        finally:
            X.x.XFree(m)
        return 0

    @staticmethod
    def _wait_modifiers(dpy, X):
        names = ["Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Super_L", "Super_R",
                 "Meta_L", "Meta_R", "ISO_Level3_Shift"]
        codes = [X.x.XKeysymToKeycode(dpy, _name_keysym(n)) for n in names]
        codes = [c for c in codes if c]
        buf = ctypes.create_string_buffer(32)
        end = time.time() + 3
        while time.time() < end:
            X.x.XQueryKeymap(dpy, buf)
            raw = buf.raw
            if not any(raw[c // 8] & (1 << (c % 8)) for c in codes):
                break
            time.sleep(0.02)
        time.sleep(0.05)


class PortalTyper:
    """Keyboard emulation through org.freedesktop.portal.RemoteDesktop.
    The compositor asks the user once to allow remote input; Velkey asks the
    portal to remember the grant (restore token) where supported."""
    name = "RemoteDesktop portal"
    DEST = "org.freedesktop.portal.Desktop"
    PATH = "/org/freedesktop/portal/desktop"
    IFACE = "org.freedesktop.portal.RemoteDesktop"

    def __init__(self, config):
        self.config = config
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.session = None
        self._closed_sub = None

    def available(self):
        try:
            r = self.bus.call_sync(self.DEST, self.PATH, "org.freedesktop.DBus.Properties", "Get",
                                   GLib.Variant("(ss)", (self.IFACE, "AvailableDeviceTypes")),
                                   None, Gio.DBusCallFlags.NONE, 2000, None)
            return bool(r.unpack()[0] & 1)
        except GLib.Error:
            return False

    def _request(self, method, params_fn, cb):
        token = "velkey%d" % random.randint(1, 1 << 30)
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        path = "/org/freedesktop/portal/desktop/request/%s/%s" % (sender, token)
        holder = {}

        def on_response(conn, snd, p, iface, sig, params):
            self.bus.signal_unsubscribe(holder["sub"])
            code, results = params.unpack()
            cb(code, results)
        holder["sub"] = self.bus.signal_subscribe(self.DEST, "org.freedesktop.portal.Request", "Response",
                                                  path, None, Gio.DBusSignalFlags.NO_MATCH_RULE, on_response)

        def done(bus, res):
            try:
                bus.call_finish(res)
            except GLib.Error as e:
                self.bus.signal_unsubscribe(holder["sub"])
                cb(2, {"error": e.message})
        self.bus.call(self.DEST, self.PATH, self.IFACE, method, params_fn(token), None,
                      Gio.DBusCallFlags.NONE, -1, None, done)

    def prepare(self, cb):
        if self.session:
            cb(True, None)
            return

        def created(code, res):
            if code != 0 or "session_handle" not in res:
                cb(False, "The remote input portal refused to create a session")
                return
            session = res["session_handle"]

            def selected(code, res):
                if code != 0:
                    cb(False, "Keyboard access was not granted")
                    return

                def started(code, res):
                    if code != 0:
                        cb(False, "Keyboard access was denied")
                        return
                    tok = res.get("restore_token")
                    if tok:
                        self.config.set("portal_restore_token", tok)
                    self.session = session
                    self._closed_sub = self.bus.signal_subscribe(
                        self.DEST, "org.freedesktop.portal.Session", "Closed", session, None,
                        Gio.DBusSignalFlags.NONE, lambda *a: self._on_closed())
                    cb(True, None)
                self._request("Start", lambda t: GLib.Variant("(osa{sv})", (session, "", {
                    "handle_token": GLib.Variant("s", t)})), started)

            def sel_params(t):
                opts = {"handle_token": GLib.Variant("s", t), "types": GLib.Variant("u", 1),
                        "persist_mode": GLib.Variant("u", 2)}
                tok = self.config.get("portal_restore_token")
                if tok:
                    opts["restore_token"] = GLib.Variant("s", tok)
                return GLib.Variant("(oa{sv})", (session, opts))
            self._request("SelectDevices", sel_params, selected)

        self._request("CreateSession", lambda t: GLib.Variant("(a{sv})", ({
            "handle_token": GLib.Variant("s", t),
            "session_handle_token": GLib.Variant("s", "velkeys%d" % random.randint(1, 1 << 30))},)),
            created)

    def _on_closed(self):
        self.session = None
        if self._closed_sub:
            self.bus.signal_unsubscribe(self._closed_sub)
            self._closed_sub = None

    def compile(self, events):
        """Map keysyms to evdev keycodes using the compositor's keymap (main thread)."""
        km = Gdk.Keymap.get_for_display(Gdk.Display.get_default())

        def lookup(ks):
            ok, keys = km.get_entries_for_keyval(ks)
            if not ok or not keys:
                return None
            keys = sorted(keys, key=lambda k: (k.group != 0, k.level))
            return keys[0]

        def code_of(name):
            k = lookup(_name_keysym(name))
            return k.keycode - 8 if k else None
        modcodes = {m: code_of(n) for m, n in MOD_KEYSYMS.items()}
        lvl3 = code_of("ISO_Level3_Shift")
        out = []
        for ev in events:
            if ev[0] == "sleep":
                out.append(ev)
                continue
            _, ks, mods = ev
            k = lookup(ks)
            held = [("code", modcodes[m]) for m in mods if modcodes.get(m) is not None]
            if k is not None and k.group == 0 and k.level <= 3:
                if k.level in (1, 3) and modcodes["shift"] is not None:
                    held.append(("code", modcodes["shift"]))
                if k.level in (2, 3) and lvl3 is not None:
                    held.append(("code", lvl3))
                main = ("code", k.keycode - 8)
            else:
                main = ("sym", ks)
            out.append(("press", main, held))
        return out

    def run(self, compiled):
        time.sleep(0.15)
        s = self.session

        def send(item, down):
            kind, val = item
            if kind == "code":
                self.bus.call_sync(self.DEST, self.PATH, self.IFACE, "NotifyKeyboardKeycode",
                                   GLib.Variant("(oa{sv}iu)", (s, {}, val, 1 if down else 0)),
                                   None, Gio.DBusCallFlags.NONE, 2000, None)
            else:
                self.bus.call_sync(self.DEST, self.PATH, self.IFACE, "NotifyKeyboardKeysym",
                                   GLib.Variant("(oa{sv}iu)", (s, {}, val, 1 if down else 0)),
                                   None, Gio.DBusCallFlags.NONE, 2000, None)
        for ev in compiled:
            if ev[0] == "sleep":
                time.sleep(ev[1])
                continue
            _, main, held = ev
            for h in held:
                send(h, True)
            send(main, True)
            send(main, False)
            for h in reversed(held):
                send(h, False)


class ToolTyper:
    """Fallback using wtype (wlroots) or ydotool if one of them is installed."""

    def __init__(self):
        self.tool = shutil.which("wtype") or shutil.which("ydotool")
        self.name = os.path.basename(self.tool) if self.tool else "none"

    def available(self):
        return self.tool is not None

    def prepare(self, cb):
        cb(True, None)

    def run(self, events):
        is_wtype = self.name == "wtype"
        for ev in events:
            if ev[0] == "sleep":
                time.sleep(ev[1])
                continue
            _, ks, mods = ev
            name = Gdk.keyval_name(ks) or ""
            uni = Gdk.keyval_to_unicode(ks)
            if is_wtype:
                args = [self.tool]
                for m in mods:
                    args += ["-M", {"super": "logo"}.get(m, m)]
                if uni and not mods and len(name) != 0 and name not in ("Return", "Tab", "BackSpace"):
                    args += [chr(uni)]
                else:
                    args += ["-k", name]
                for m in mods:
                    args += ["-m", {"super": "logo"}.get(m, m)]
            else:
                if uni and not mods:
                    args = [self.tool, "type", "--", chr(uni)]
                else:
                    continue
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class AutoTyper:
    def __init__(self, config):
        self.config = config
        self._backend = None
        self.busy = False

    def backend(self):
        if self._backend is not None:
            return self._backend
        cands = []
        if session_type() == "x11":
            cands.append(X11Typer())
        else:
            cands.append(PortalTyper(self.config))
            cands.append(ToolTyper())
        for c in cands:
            try:
                if c.available():
                    self._backend = c
                    return c
            except Exception:
                continue
        return None

    def type_sequence(self, entry, sequence, on_done=None, initial_delay=0.0, wait_before=None):
        """Parse and type `sequence` for `entry` (must be called on main thread).
        `wait_before` is called on the worker thread before typing starts, e.g.
        to wait until the user has let go of the global hot key."""
        if self.busy:
            return
        b = self.backend()
        if b is None:
            if on_done:
                on_done("No keyboard emulation backend available on this system")
            return
        delay = int(self.config.get("autotype_delay", 10))
        actions = parse_sequence(sequence, entry)
        events = flatten(actions, delay)
        self.busy = True

        def ready(ok, err):
            if not ok:
                self.busy = False
                if isinstance(b, PortalTyper):
                    # allow a fresh grant next time
                    self.config.set("portal_restore_token", "")
                if on_done:
                    on_done(err)
                return
            compiled = b.compile(events) if hasattr(b, "compile") else events

            def work():
                err = None
                try:
                    if wait_before is not None:
                        wait_before()
                    time.sleep(initial_delay)
                    b.run(compiled)
                except Exception as e:  # noqa
                    err = str(e)
                    if isinstance(b, PortalTyper):
                        b.session = None
                finally:
                    self.busy = False
                if on_done:
                    GLib.idle_add(lambda: (on_done(err), False)[1])
            threading.Thread(target=work, daemon=True).start()
        b.prepare(ready)


# --------------------------------------------------------------------------
# active window detection
# --------------------------------------------------------------------------
KWIN_IFACE_XML = """<node><interface name="io.github.velkey.KWin">
<method name="Window"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/></method>
</interface></node>"""


class WindowProbe:
    def __init__(self, conn):
        self.conn = conn
        self._pending = []
        self._reg = None
        self.last_id = ""
        if is_kde() and conn is not None:
            try:
                info = Gio.DBusNodeInfo.new_for_xml(KWIN_IFACE_XML)
                self._reg = conn.register_object("/io/github/velkey/KWin", info.interfaces[0],
                                                 self._on_call, None, None)
            except Exception:
                self._reg = None

    def _on_call(self, conn, sender, path, iface, method, params, inv):
        caption, cls, wid = params.unpack()
        self.last_id = wid
        inv.return_value(None)
        pend, self._pending = self._pending, []
        for cb in pend:
            cb(caption, cls)

    def get_active(self, cb):
        """Calls cb(title, wm_class) asynchronously; title '' if unknown.
        On KDE, `self.last_id` is the KWin id of that window afterwards."""
        self.last_id = ""
        if self._reg is not None:
            try:
                self._kwin(cb)
                return
            except Exception:
                pass
        if session_type() == "x11":
            try:
                cb(x11().active_window_title(), "")
                return
            except Exception:
                pass
        cb("", "")

    def _kwin(self, cb):
        name = self.conn.get_unique_name()
        js = ('var w = workspace.activeWindow || workspace.activeClient;'
              'callDBus("%s", "/io/github/velkey/KWin", "io.github.velkey.KWin", "Window",'
              ' w ? String(w.caption) : "", w ? String(w.resourceClass) : "",'
              ' w ? String(w.internalId) : "");' % name)
        done = {"v": False}
        cleanup = self._run_kwin(js)

        def finish(title, cls):
            if done["v"]:
                return
            done["v"] = True
            cleanup()
            cb(title, cls)
        self._pending.append(finish)
        GLib.timeout_add(800, lambda: (finish("", ""), False)[1])

    def activate(self, window_id=None, caption=None):
        """Bring a window to the front and give it keyboard focus (KDE only).
        Wayland does not let an app focus windows itself, KWin can."""
        if self._reg is None or not (window_id or caption):
            return False
        import json
        js = ('var ws = workspace.windowList ? workspace.windowList() : workspace.clientList();'
              'var id = %s, cap = %s;'
              'for (var i = 0; i < ws.length; i++) {'
              '  var w = ws[i];'
              '  if ((id && String(w.internalId) == id) || (!id && String(w.caption) == cap)) {'
              '    if (w.minimized) w.minimized = false;'
              '    if (workspace.windowList) workspace.activeWindow = w; else workspace.activeClient = w;'
              '    break;'
              '  }'
              '}' % (json.dumps(window_id or ""), json.dumps(caption or "")))
        try:
            cleanup = self._run_kwin(js)
        except Exception:
            return False
        GLib.timeout_add(1500, lambda: (cleanup(), False)[1])
        return True

    def _run_kwin(self, js):
        """Load and run a one-shot KWin script; returns a function that unloads it."""
        fd, path = tempfile.mkstemp(suffix=".js", prefix="velkey-")
        with os.fdopen(fd, "w") as f:
            f.write(js)
        plugin = "velkey-%d" % random.randint(1, 1 << 30)

        def cleanup():
            try:
                os.unlink(path)
            except OSError:
                pass
            try:
                self.conn.call("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript",
                               GLib.Variant("(s)", (plugin,)), None, Gio.DBusCallFlags.NONE, -1, None, None)
            except Exception:
                pass
        try:
            r = self.conn.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "loadScript",
                                    GLib.Variant("(ss)", (path, plugin)), None, Gio.DBusCallFlags.NONE,
                                    2000, None)
            sid = r.unpack()[0]
            for p in ("/Scripting/Script%d" % sid, "/%d" % sid):
                try:
                    self.conn.call_sync("org.kde.KWin", p, "org.kde.kwin.Script", "run", None, None,
                                        Gio.DBusCallFlags.NONE, 2000, None)
                    return cleanup
                except GLib.Error:
                    continue
        except Exception:
            cleanup()
            raise
        cleanup()
        raise RuntimeError("could not run KWin script")


# --------------------------------------------------------------------------
# global hot key
# --------------------------------------------------------------------------
QT_SHIFT, QT_CTRL, QT_ALT, QT_META = 0x02000000, 0x04000000, 0x08000000, 0x10000000


MOD_ORDER = ("Ctrl", "Alt", "Shift", "Meta")
QT_MODS = {"Ctrl": QT_CTRL, "Alt": QT_ALT, "Shift": QT_SHIFT, "Meta": QT_META}
# display name -> (Qt key code, Gdk key name)
NAMED_KEYS = {
    "Space": (0x20, "space"), "Tab": (0x01000001, "Tab"), "Return": (0x01000004, "Return"),
    "Insert": (0x01000006, "Insert"), "Delete": (0x01000007, "Delete"), "Pause": (0x01000008, "Pause"),
    "Print": (0x01000009, "Print"), "Home": (0x01000010, "Home"), "End": (0x01000011, "End"),
    "Left": (0x01000012, "Left"), "Up": (0x01000013, "Up"), "Right": (0x01000014, "Right"),
    "Down": (0x01000015, "Down"), "PgUp": (0x01000016, "Page_Up"), "PgDown": (0x01000017, "Page_Down"),
    "Backspace": (0x01000003, "BackSpace"),
}
for _i in range(1, 36):
    NAMED_KEYS["F%d" % _i] = (0x01000030 + _i - 1, "F%d" % _i)


def parse_hotkey(spec):
    """'Ctrl+Alt+Shift+Z' -> (set of canonical modifiers, key name)"""
    parts = [p.strip() for p in (spec or "").replace(" ", "").split("+")]
    if spec and spec.rstrip().endswith("++"):
        parts = parts[:-2] + ["+"]
    parts = [p for p in parts if p]
    alias = {"ctrl": "Ctrl", "control": "Ctrl", "primary": "Ctrl", "alt": "Alt", "shift": "Shift",
             "meta": "Meta", "super": "Meta", "win": "Meta"}
    mods = {alias[p.lower()] for p in parts[:-1] if p.lower() in alias}
    key = parts[-1] if parts else "Z"
    if len(key) == 1:
        key = key.upper()
    else:
        for name in NAMED_KEYS:
            if name.lower() == key.lower():
                key = name
    return mods, key


def format_hotkey(mods, key):
    return "+".join([m for m in MOD_ORDER if m in mods] + [key])


def hotkey_valid(spec):
    mods, key = parse_hotkey(spec)
    return bool(mods) and (len(key) == 1 and ord(key) < 127 or key in NAMED_KEYS)


def spec_to_qt(spec):
    mods, key = parse_hotkey(spec)
    q = 0
    for m in mods:
        q |= QT_MODS[m]
    if key in NAMED_KEYS:
        q |= NAMED_KEYS[key][0]
    elif len(key) == 1:
        q |= ord(key.upper())
    return q


def qt_to_spec(q):
    mods = {m for m, bit in QT_MODS.items() if q & bit}
    code = q & 0x01FFFFFF
    key = None
    for name, (qc, _g) in NAMED_KEYS.items():
        if qc == code:
            key = name
    if key is None and 0x20 < code < 0x7F:
        key = chr(code).upper()
    return format_hotkey(mods, key) if key else None


def spec_to_gdk_name(key):
    if key in NAMED_KEYS:
        return NAMED_KEYS[key][1]
    return key.lower()


def spec_to_gnome(spec):
    mods, key = parse_hotkey(spec)
    pre = {"Ctrl": "<Primary>", "Alt": "<Alt>", "Shift": "<Shift>", "Meta": "<Super>"}
    return "".join(pre[m] for m in MOD_ORDER if m in mods) + spec_to_gdk_name(key)


def hotkey_from_event(ev):
    """Build a spec from a Gdk key event, or None if only modifiers are held."""
    kv = ev.keyval
    if kv in (Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R, Gdk.KEY_Alt_L,
              Gdk.KEY_Alt_R, Gdk.KEY_Super_L, Gdk.KEY_Super_R, Gdk.KEY_Meta_L, Gdk.KEY_Meta_R,
              Gdk.KEY_ISO_Level3_Shift, Gdk.KEY_Caps_Lock, Gdk.KEY_Num_Lock):
        return None
    st = ev.state
    mods = set()
    if st & Gdk.ModifierType.CONTROL_MASK:
        mods.add("Ctrl")
    if st & Gdk.ModifierType.MOD1_MASK:
        mods.add("Alt")
    if st & Gdk.ModifierType.SHIFT_MASK:
        mods.add("Shift")
    if st & (Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.META_MASK | Gdk.ModifierType.MOD4_MASK):
        mods.add("Meta")
    # the unshifted key on that physical key (Shift+1 -> "1", not "!")
    try:
        km = Gdk.Keymap.get_for_display(Gdk.Display.get_default())
        ok, _keys, vals = km.get_entries_for_keycode(ev.hardware_keycode)
        if ok and vals:
            kv = vals[0]
    except Exception:
        pass
    name = Gdk.keyval_name(kv) or ""
    for disp, (_q, gname) in NAMED_KEYS.items():
        if gname == name:
            return format_hotkey(mods, disp)
    uni = Gdk.keyval_to_unicode(kv)
    if uni and 0x20 < uni < 0x7F:
        return format_hotkey(mods, chr(uni).upper())
    return None


KDE_ACTION = ["velkey", "auto-type", "Velkey", "Global Auto-Type"]
GNOME_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/velkey/"
GNOME_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"


class GlobalHotkey:
    def __init__(self, conn, spec, callback):
        self.conn = conn
        self.spec = spec
        self.callback = callback
        self.backend = "none"
        self.error = None
        self._sub = None
        self._last = 0
        self._x11 = None
        self._stopped = False
        self._sub_rel = None
        self._released = threading.Event()
        self._released.set()

    def wait_released(self, timeout=2.0):
        """Block (worker thread) until the hot key and its modifiers are let go.
        Typing while Ctrl+Alt+Shift are still held would turn every typed
        character into a shortcut instead of text."""
        if self.backend.startswith("KDE") and self._sub_rel is not None:
            self._released.wait(timeout)
            time.sleep(0.25)  # the modifiers usually come up just after the key
        elif self.backend != "X11":  # X11 typing checks the real key state itself
            time.sleep(0.7)

    def _fire(self):
        if self._stopped:
            return
        now = time.time()
        if now - self._last < 0.4:
            return
        self._last = now
        self._released.clear()
        GLib.timeout_add(3000, lambda: (self._released.set(), False)[1])  # never wait forever
        GLib.idle_add(lambda: (self.callback(), False)[1])

    def start(self, force=False):
        """force=True: the user picked a new key in Velkey, override what the
        desktop remembers; otherwise keep a key the user set in KDE settings."""
        self.error = None
        if not hotkey_valid(self.spec):
            self.error = "invalid hot key %s" % self.spec
        if is_kde() and self.conn is not None:
            try:
                self._kde(force)
                self.backend = "KDE global shortcuts"
                return
            except Exception as e:
                self.error = str(e)
        if is_gnome():
            self.backend = "GNOME keyboard shortcut"
            if force:
                self._gnome()
            return
        if session_type() == "x11":
            try:
                self._start_x11()
                self.backend = "X11"
                return
            except Exception as e:
                self.error = str(e)
        self.backend = "external"

    def stop(self):
        self._stopped = True
        if self._sub is not None and self.conn is not None:
            self.conn.signal_unsubscribe(self._sub)
            self._sub = None
        if self._sub_rel is not None and self.conn is not None:
            self.conn.signal_unsubscribe(self._sub_rel)
            self._sub_rel = None
        if self._x11 is not None:
            X, dpy, kc, mask = self._x11
            try:
                root = X.x.XDefaultRootWindow(dpy)
                X.x.XUngrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong]
                for extra in (0, 2, 16, 18):
                    X.x.XUngrabKey(dpy, kc, mask | extra, root)
                X.x.XFlush(dpy)
            except Exception:
                pass
            self._x11 = None

    def block(self, blocked):
        """Temporarily disable all desktop global shortcuts (KDE) while recording."""
        if self.backend.startswith("KDE") and self.conn is not None:
            try:
                self.conn.call_sync("org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel",
                                    "blockGlobalShortcuts", GLib.Variant("(b)", (blocked,)), None,
                                    Gio.DBusCallFlags.NONE, 2000, None)
            except Exception:
                pass

    # ---- KDE
    def _kde(self, force):
        c = self.conn
        call = lambda m, v: c.call_sync("org.kde.kglobalaccel", "/kglobalaccel", "org.kde.KGlobalAccel",
                                        m, v, None, Gio.DBusCallFlags.NONE, 3000, None)
        call("doRegister", GLib.Variant("(as)", (KDE_ACTION,)))
        keys = [([spec_to_qt(self.spec), 0, 0, 0],)]
        call("setShortcutKeys", GLib.Variant("(asa(ai)u)", (KDE_ACTION, keys, 1)))  # IsDefault
        flags = 2 | (4 if force else 0)                                            # SetPresent [| NoAutoloading]
        active = call("setShortcutKeys", GLib.Variant("(asa(ai)u)", (KDE_ACTION, keys, flags))).unpack()[0]
        q = active[0][0][0] if active and active[0][0] else 0
        if not q:
            self.error = "%s is already used by another application" % self.spec
        else:
            actual = qt_to_spec(q)
            if actual and actual != self.spec:
                self.spec = actual  # user changed it in KDE System Settings
        if self._sub is None:
            self._sub = c.signal_subscribe("org.kde.kglobalaccel", "org.kde.kglobalaccel.Component",
                                           "globalShortcutPressed", "/component/velkey", None,
                                           Gio.DBusSignalFlags.NONE, self._on_kde)
        if self._sub_rel is None:
            self._sub_rel = c.signal_subscribe("org.kde.kglobalaccel", "org.kde.kglobalaccel.Component",
                                               "globalShortcutReleased", "/component/velkey", None,
                                               Gio.DBusSignalFlags.NONE, self._on_kde_released)

    def _on_kde(self, conn, sender, path, iface, sig, params):
        comp, shortcut, _ts = params.unpack()
        if shortcut == "auto-type":
            self._fire()

    def _on_kde_released(self, conn, sender, path, iface, sig, params):
        if params.unpack()[1] == "auto-type":
            self._released.set()

    # ---- GNOME (binding created by install.sh)
    def _gnome(self):
        try:
            out = subprocess.run(["gsettings", "get", "org.gnome.settings-daemon.plugins.media-keys",
                                  "custom-keybindings"], capture_output=True, text=True).stdout
            if GNOME_PATH not in out:
                self.error = "run install.sh to create the GNOME shortcut"
                return
            subprocess.run(["gsettings", "set", "%s:%s" % (GNOME_SCHEMA, GNOME_PATH), "binding",
                            spec_to_gnome(self.spec)], check=True)
        except Exception as e:
            self.error = str(e)

    # ---- X11
    def _start_x11(self):
        X = x11()
        mods, key = parse_hotkey(self.spec)
        mask = 0
        if "Shift" in mods:
            mask |= 1
        if "Ctrl" in mods:
            mask |= 4
        if "Alt" in mods:
            mask |= 8
        if "Meta" in mods:
            mask |= 64
        dpy = X.open()
        root = X.x.XDefaultRootWindow(dpy)
        kc = X.x.XKeysymToKeycode(dpy, _name_keysym(spec_to_gdk_name(key)))
        if not kc:
            raise RuntimeError("unknown hot key")
        for extra in (0, 2, 16, 18):  # CapsLock / NumLock variants
            X.x.XGrabKey(dpy, kc, mask | extra, root, 0, 1, 1)
        X.x.XSync(dpy, 0)
        self._x11 = (X, dpy, kc, mask)
        ev = ctypes.create_string_buffer(256)

        def loop():
            while not self._stopped:
                X.x.XNextEvent(dpy, ev)
                if ctypes.c_int.from_buffer(ev).value == 2:  # KeyPress
                    self._fire()
        threading.Thread(target=loop, daemon=True).start()
