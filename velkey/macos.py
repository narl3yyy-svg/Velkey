"""macOS support for Auto-Type, through ctypes (nothing extra to install).

  * global hot key      Carbon RegisterEventHotKey (no permission needed)
  * typing              Quartz keyboard events (CGEventPost)
  * active window       NSWorkspace frontmost app + Accessibility window title
  * password-box check  Accessibility: the focused element's subrole is
                        AXSecureTextField in a password field
  * Safari's URL        AppleScript (Chrome-family and Firefox use their
                        session files, see browser.py)

Typing and reading window titles need the Accessibility permission:
System Settings → Privacy & Security → Accessibility.

Every entry point catches its own errors: if something here fails, Auto-Type
reports it instead of taking Velkey down.
"""
import ctypes
import ctypes.util
import subprocess
import sys
import time

IS_MAC = sys.platform == "darwin"

_vp = ctypes.c_void_p
_FW = "/System/Library/Frameworks/%s.framework/%s"
_libs = {}

PERMISSION_HELP = ("Velkey needs permission to type: open System Settings → Privacy & Security → "
                   "Accessibility and turn Velkey on (when started from Terminal, turn on Terminal). "
                   "Then use Auto-Type again.")


def _lib(name):
    if name not in _libs:
        path = "/usr/lib/libobjc.A.dylib" if name == "objc" else _FW % (name, name)
        _libs[name] = ctypes.cdll.LoadLibrary(path)
        if name == "objc":
            ctypes.cdll.LoadLibrary(_FW % ("AppKit", "AppKit"))  # NSWorkspace, NSApplication…
    return _libs[name]


def _fn(lib, name, restype, *argtypes):
    f = getattr(_lib(lib), name)
    f.restype = restype
    f.argtypes = list(argtypes)
    return f


# ------------------------------------------------------------ CoreFoundation
UTF8 = 0x08000100


def cfstr(text):
    return _fn("CoreFoundation", "CFStringCreateWithCString", _vp, _vp, ctypes.c_char_p, ctypes.c_uint32)(
        None, text.encode(), UTF8)


def cf_release(ref):
    if ref:
        _fn("CoreFoundation", "CFRelease", None, _vp)(ref)


def pystr(ref):
    """CFString / NSString -> str ('' for anything else)."""
    if not ref:
        return ""
    cf = "CoreFoundation"
    if _fn(cf, "CFGetTypeID", ctypes.c_ulong, _vp)(ref) != _fn(cf, "CFStringGetTypeID", ctypes.c_ulong)():
        return ""
    n = _fn(cf, "CFStringGetLength", ctypes.c_long, _vp)(ref)
    size = n * 4 + 1
    buf = ctypes.create_string_buffer(size)
    ok = _fn(cf, "CFStringGetCString", ctypes.c_bool, _vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32)(
        ref, buf, size, UTF8)
    return buf.value.decode("utf-8", "replace") if ok else ""


# ------------------------------------------------------------- Objective-C
def _send(obj, sel, *args, restype=_vp):
    """[obj sel args…] with an exact (non-variadic) objc_msgSend signature."""
    objc = _lib("objc")
    objc.sel_registerName.restype = _vp
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    proto = ctypes.CFUNCTYPE(restype, _vp, _vp, *[type(a) for a in args])
    return proto(("objc_msgSend", objc))(obj, objc.sel_registerName(sel), *args)


def _cls(name):
    objc = _lib("objc")
    objc.objc_getClass.restype = _vp
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    return objc.objc_getClass(name)


# ------------------------------------------------------------ permissions
def is_trusted(prompt=False):
    """Accessibility permission. prompt=True also opens the system dialog."""
    try:
        if not prompt:
            return bool(_fn("ApplicationServices", "AXIsProcessTrusted", ctypes.c_bool)())
        cf = _lib("CoreFoundation")
        key = _vp.in_dll(_lib("ApplicationServices"), "kAXTrustedCheckOptionPrompt")
        true = _vp.in_dll(cf, "kCFBooleanTrue")
        keys, values = (_vp * 1)(key.value), (_vp * 1)(true.value)
        opts = _fn("CoreFoundation", "CFDictionaryCreate", _vp, _vp, _vp, _vp, ctypes.c_long, _vp, _vp)(
            None, keys, values, 1,
            ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")),
            ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks")))
        try:
            return bool(_fn("ApplicationServices", "AXIsProcessTrustedWithOptions", ctypes.c_bool, _vp)(opts))
        finally:
            cf_release(opts)
    except Exception:
        return False


# ------------------------------------------------------- Accessibility (AX)
def _ax_copy(elem, attr):
    out = _vp()
    name = cfstr(attr)
    try:
        err = _fn("ApplicationServices", "AXUIElementCopyAttributeValue", ctypes.c_int32, _vp, _vp,
                  ctypes.POINTER(_vp))(elem, name, ctypes.byref(out))
    finally:
        cf_release(name)
    return out.value if err == 0 else None


def _ax_app(pid):
    return _fn("ApplicationServices", "AXUIElementCreateApplication", _vp, ctypes.c_int32)(int(pid))


# -------------------------------------------------------- active window
def frontmost():
    """(window title, app name, pid) of the frontmost app; title falls back to
    the app name when the window title cannot be read."""
    try:
        ws = _send(_cls(b"NSWorkspace"), b"sharedWorkspace")
        app = _send(ws, b"frontmostApplication")
        if not app:
            return "", "", 0
        pid = _send(app, b"processIdentifier", restype=ctypes.c_int32)
        name = pystr(_send(app, b"localizedName"))
    except Exception:
        return "", "", 0
    title = ""
    try:
        ax = _ax_app(pid)
        win = _ax_copy(ax, "AXFocusedWindow") or _ax_copy(ax, "AXMainWindow")
        if win:
            t = _ax_copy(win, "AXTitle")
            title = pystr(t)
            cf_release(t)
            cf_release(win)
        cf_release(ax)
    except Exception:
        pass
    return title or name, name, pid


def activate(pid):
    """Bring the app with this pid to the front."""
    try:
        app = _send(_cls(b"NSRunningApplication"), b"runningApplicationWithProcessIdentifier:",
                    ctypes.c_int32(int(pid)))
        if app:
            # NSApplicationActivateIgnoringOtherApps = 1 << 1
            return bool(_send(app, b"activateWithOptions:", ctypes.c_ulong(2), restype=ctypes.c_bool))
    except Exception:
        pass
    return False


def hide_self():
    """[NSApp hide:nil]: Velkey steps back and the previous app comes forward."""
    try:
        app = _send(_cls(b"NSApplication"), b"sharedApplication")
        _send(app, b"hide:", _vp(None))
        return True
    except Exception:
        return False


# ------------------------------------------------------ password-box check
TEXT_ROLES = ("AXTextField", "AXTextArea", "AXComboBox", "AXSearchField")


def focused_field(pid):
    """(known, password, role) for the focused element of app `pid`.
    Only real text fields count as 'known'; anything else (e.g. a browser
    whose page content is not exposed) is unknown, so Velkey types the whole
    sequence like KeePass does."""
    try:
        ax = _ax_app(pid)
        el = _ax_copy(ax, "AXFocusedUIElement")
        cf_release(ax)
        if not el:
            return False, False, ""
        role_ref, sub_ref = _ax_copy(el, "AXRole"), _ax_copy(el, "AXSubrole")
        role, sub = pystr(role_ref), pystr(sub_ref)
        for r in (role_ref, sub_ref, el):
            cf_release(r)
        if sub == "AXSecureTextField":
            return True, True, sub
        if role in TEXT_ROLES:
            return True, False, role
        return False, False, role
    except Exception:
        return False, False, ""


# ------------------------------------------------------------- browsers
APPLESCRIPT_URL = {
    "Safari": 'tell application "Safari" to return URL of front document',
    "Safari Technology Preview": 'tell application "Safari Technology Preview" to return URL of front document',
}


def browser_url(app_name, timeout=3):
    """Safari's page URL via AppleScript (macOS asks once for permission)."""
    script = APPLESCRIPT_URL.get(app_name)
    if not script:
        return ""
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
        url = r.stdout.strip()
        return url if url.startswith(("http://", "https://")) else ""
    except Exception:
        return ""


# -------------------------------------------------------------- keyboard
# virtual key codes (ANSI layout) for the keys Auto-Type and hot keys use
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
    "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
    "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
    "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
    ".": 47, "`": 50,
    "Return": 36, "Tab": 48, "space": 49, "BackSpace": 51, "Escape": 53, "Delete": 117, "Insert": 114,
    "Help": 114, "Home": 115, "End": 119, "Prior": 116, "Page_Up": 116, "Next": 121, "Page_Down": 121,
    "Left": 123, "Right": 124, "Down": 125, "Up": 126, "KP_Enter": 76,
    "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97, "F7": 98, "F8": 100, "F9": 101,
    "F10": 109, "F11": 103, "F12": 111, "F13": 105, "F14": 107, "F15": 113, "F16": 106, "F17": 64,
    "F18": 79, "F19": 80, "F20": 90,
}
for _i in range(10):
    KEYCODES["KP_%d" % _i] = (82, 83, 84, 85, 86, 87, 88, 89, 91, 92)[_i]

# CGEventFlags for Auto-Type modifiers ({+} shift, {^} ctrl, {%} alt/option, {@} cmd)
EVENT_FLAGS = {"shift": 0x20000, "ctrl": 0x40000, "alt": 0x80000, "super": 0x100000}
MODIFIER_MASK = 0x20000 | 0x40000 | 0x80000 | 0x100000


class MacTyper:
    """Auto-Type backend: posts keyboard events with CGEventPost."""

    name = "macOS"

    def available(self):
        return IS_MAC

    def prepare(self, cb):
        if is_trusted():
            cb(True, None)
        else:
            is_trusted(prompt=True)  # shows the system dialog the first time
            cb(False, PERMISSION_HELP)

    def compile(self, events):
        return events

    def _wait_modifiers(self, limit=2.0):
        """Wait until the user has let go of Ctrl/Option/Shift/⌘ (the hot key),
        or typed letters would become shortcuts."""
        state = _fn("ApplicationServices", "CGEventSourceFlagsState", ctypes.c_uint64, ctypes.c_int32)
        end = time.time() + limit
        while state(1) & MODIFIER_MASK and time.time() < end:  # 1 = HID system state
            time.sleep(0.03)

    def run(self, events):
        from gi.repository import Gdk
        create = _fn("ApplicationServices", "CGEventCreateKeyboardEvent", _vp, _vp, ctypes.c_uint16, ctypes.c_bool)
        set_uni = _fn("ApplicationServices", "CGEventKeyboardSetUnicodeString", None, _vp, ctypes.c_long,
                      ctypes.POINTER(ctypes.c_uint16))
        set_flags = _fn("ApplicationServices", "CGEventSetFlags", None, _vp, ctypes.c_uint64)
        post = _fn("ApplicationServices", "CGEventPost", None, ctypes.c_uint32, _vp)
        self._wait_modifiers()
        for ev in events:
            if ev[0] == "sleep":
                time.sleep(ev[1])
                continue
            _, ks, mods = ev
            name = Gdk.keyval_name(ks) or ""
            uni = Gdk.keyval_to_unicode(ks)
            flags = 0
            for m in mods:
                flags |= EVENT_FLAGS.get(m, 0)
            # named keys (Return, Tab, F5…) by key code; characters as text, so they
            # come out right on any keyboard layout (a key code is a physical key)
            code = KEYCODES.get(name) if len(name) > 1 else None
            text = None
            if code is None and uni:
                ch = chr(uni)
                if mods and ch.lower() in KEYCODES:   # shortcuts like {^}a need the real key
                    code = KEYCODES[ch.lower()]
                else:
                    code, text = 0, ch                # any character, any layout
            if code is None:
                continue
            for down in (True, False):
                e = create(None, code, down)
                if not e:
                    raise RuntimeError("macOS refused to create a key event")
                if text is not None:
                    units = text.encode("utf-16-le")
                    arr = (ctypes.c_uint16 * (len(units) // 2)).from_buffer_copy(units)
                    set_uni(e, len(arr), arr)
                set_flags(e, flags)
                post(0, e)  # kCGHIDEventTap
                cf_release(e)
            time.sleep(0.004)


# --------------------------------------------------------------- hot key
_KEYB = 0x6B657962            # 'keyb' (kEventClassKeyboard)
_PRESSED, _RELEASED = 5, 6    # kEventHotKeyPressed / kEventHotKeyReleased
HOTKEY_MODS = {"Ctrl": 0x1000, "Alt": 0x0800, "Shift": 0x0200, "Meta": 0x0100}  # control, option, shift, cmd


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class _HotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, _vp, _vp, _vp)


def hotkey_code(mods, key, named_keys):
    """(Carbon modifier bits, virtual key code) for parse_hotkey() output, or None."""
    bits = 0
    for m in mods:
        bits |= HOTKEY_MODS.get(m, 0)
    if len(key) == 1:
        code = KEYCODES.get(key.lower())
    else:
        code = KEYCODES.get(named_keys.get(key, (0, key))[1])
    return None if code is None else (bits, code)


class HotKey:
    """A system-wide hot key. on_press / on_release run on the main thread."""

    def __init__(self, on_press, on_release=None):
        self.on_press = on_press
        self.on_release = on_release
        self._ref = None
        self._handler = None
        self._cb = None   # keep the ctypes callback alive

    def register(self, bits, code):
        """Returns None on success, else an error message."""
        carbon = "Carbon"
        target = _fn(carbon, "GetApplicationEventTarget", _vp)()
        if self._handler is None:
            def handle(_call, event, _data):
                kind = _fn(carbon, "GetEventKind", ctypes.c_uint32, _vp)(event)
                cb = self.on_press if kind == _PRESSED else self.on_release
                if cb is not None:
                    cb()
                return 0
            self._cb = _HANDLER(handle)
            specs = (_EventTypeSpec * 2)(_EventTypeSpec(_KEYB, _PRESSED), _EventTypeSpec(_KEYB, _RELEASED))
            ref = _vp()
            err = _fn(carbon, "InstallEventHandler", ctypes.c_int32, _vp, _HANDLER, ctypes.c_ulong,
                      ctypes.POINTER(_EventTypeSpec), _vp, ctypes.POINTER(_vp))(
                target, self._cb, 2, specs, None, ctypes.byref(ref))
            if err != 0:
                return "macOS refused the hot key handler (error %d)" % err
            self._handler = ref
        self.unregister()
        ref = _vp()
        err = _fn(carbon, "RegisterEventHotKey", ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint32, _HotKeyID,
                  _vp, ctypes.c_uint32, ctypes.POINTER(_vp))(
            code, bits, _HotKeyID(0x564C4B59, 1), target, 0, ctypes.byref(ref))   # 'VLKY'
        if err != 0:
            return ("this key combination is already used by another app" if err == -9878
                    else "macOS refused the hot key (error %d)" % err)
        self._ref = ref
        return None

    def unregister(self):
        if self._ref is not None and self._ref.value:
            try:
                _fn("Carbon", "UnregisterEventHotKey", ctypes.c_int32, _vp)(self._ref)
            except Exception:
                pass
        self._ref = None

