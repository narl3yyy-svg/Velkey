"""macOS support: the right OpenSSL is used and Linux-only parts switch off."""
import unittest

from tests.helpers import HAVE_GI, needs_gi
from velkey import crypto


class TestLibcryptoCandidates(unittest.TestCase):
    def test_macos_uses_homebrew_never_the_system_library(self):
        names = crypto.libcrypto_candidates("darwin", {}, lambda n: "/usr/lib/libcrypto.dylib")
        self.assertEqual(names, ["/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib",
                                 "/usr/local/opt/openssl@3/lib/libcrypto.3.dylib"])
        self.assertFalse(set(names) & set(crypto.MAC_SYSTEM_LIBCRYPTO))

    def test_env_override_comes_first(self):
        env = {"VELKEY_LIBCRYPTO": "/custom/libcrypto.3.dylib"}
        self.assertEqual(crypto.libcrypto_candidates("darwin", env)[0], "/custom/libcrypto.3.dylib")
        self.assertEqual(crypto.libcrypto_candidates("linux", env, lambda n: None)[0], "/custom/libcrypto.3.dylib")

    def test_linux_unchanged(self):
        self.assertEqual(crypto.libcrypto_candidates("linux", {}, lambda n: "libcrypto.so.3"),
                         ["libcrypto.so.3", "libcrypto.so.3"])


@needs_gi
class TestMacAutoType(unittest.TestCase):
    """macOS Auto-Type logic, with the macOS system calls replaced by recorders."""

    def setUp(self):
        from velkey import autotype, macos
        self.at, self.mac = autotype, macos
        self.saved = (autotype.IS_MAC, macos.IS_MAC, macos._fn, macos.frontmost, macos.HotKey,
                      macos.focused_field)
        autotype.IS_MAC = macos.IS_MAC = True

    def tearDown(self):
        (self.at.IS_MAC, self.mac.IS_MAC, self.mac._fn, self.mac.frontmost, self.mac.HotKey,
         self.mac.focused_field) = self.saved

    def test_app_name_fails_safely_without_macos(self):
        import sys
        if sys.platform != "darwin":
            self.assertFalse(self.mac.set_app_name("Velkey"))  # no AppKit here: no crash, just False

    def test_hotkey_codes(self):
        code = self.mac.hotkey_code
        self.assertEqual(code({"Ctrl", "Alt", "Shift"}, "Z", self.at.NAMED_KEYS), (0x1000 | 0x0800 | 0x0200, 6))
        self.assertEqual(code({"Meta"}, "F5", self.at.NAMED_KEYS), (0x0100, 96))
        self.assertEqual(code({"Ctrl"}, "PgUp", self.at.NAMED_KEYS), (0x1000, 116))
        self.assertIsNone(code({"Ctrl"}, "Pause", self.at.NAMED_KEYS))

    def test_backend_is_mac_typer(self):
        self.assertIsInstance(self.at.AutoTyper({}).backend(), self.mac.MacTyper)

    def test_typing_turns_into_key_events(self):
        posted = []

        class Ev:
            def __init__(self, code, down):
                self.code, self.down, self.text, self.flags = code, down, None, 0

        def fake_fn(lib, name, *types):
            return {
                "CGEventCreateKeyboardEvent": lambda src, code, down: Ev(code, down),
                "CGEventKeyboardSetUnicodeString": lambda e, n, arr: setattr(
                    e, "text", bytes(arr).decode("utf-16-le")),
                "CGEventSetFlags": lambda e, f: setattr(e, "flags", f),
                "CGEventPost": lambda tap, e: posted.append(e),
                "CGEventSourceFlagsState": lambda state: 0,
                "CFRelease": lambda ref: None,
            }[name]
        self.mac._fn = fake_fn
        events = self.at.flatten(self.at.parse_sequence("aé{TAB}^v{ENTER}", None), 0)
        self.mac.MacTyper().run([e for e in events if e[0] != "pwgate"])
        downs = [(e.code, e.text, e.flags) for e in posted if e.down]
        self.assertEqual(downs, [(0, "a", 0), (0, "é", 0), (48, None, 0), (9, None, 0x40000), (36, None, 0)])
        self.assertEqual(len(posted), 2 * len(downs))  # every key is pressed and released

    def test_hotkey_registers_or_reports_conflict(self):
        results = [None, "this key combination is already used by another app"]

        class FakeHotKey:
            def __init__(self, on_press, on_release=None):
                pass

            def register(self, bits, code):
                return results.pop(0)

            def unregister(self):
                pass
        self.mac.HotKey = FakeHotKey
        hk = self.at.GlobalHotkey(None, "Ctrl+Alt+Shift+Z", lambda: None)
        hk.start()
        self.assertEqual((hk.backend, hk.error), ("macOS", None))
        hk.start()
        self.assertEqual(hk.backend, "none")
        self.assertIn("already used", hk.error)

    def test_active_window_and_own_window(self):
        import os
        got = []
        probe = self.at.WindowProbe(None)
        self.mac.frontmost = lambda: ("Example Shop – Sign in", "Safari", 4321)
        probe.get_active(lambda title, app: got.append((title, app)))
        self.assertEqual((got[-1], probe.last_pid, probe.last_id), (("Example Shop – Sign in", "Safari"), 4321, "4321"))
        self.mac.frontmost = lambda: ("Velkey", "Python", os.getpid())
        probe.get_active(lambda title, app: got.append((title, app)))
        self.assertEqual(got[-1][1], "velkey")  # Velkey itself in front: global Auto-Type ignores it

    def test_password_field_check(self):
        from velkey import a11y
        c = a11y.FocusClient.__new__(a11y.FocusClient)
        c.disabled = True
        for answer, known, password in (((True, True, "AXSecureTextField"), True, True),
                                        ((True, False, "AXTextField"), True, False),
                                        ((False, False, "AXWebArea"), False, False)):
            self.mac.focused_field = lambda pid, a=answer: a
            info = c.field_from_thread(1)
            self.assertEqual((info.known, info.password), (known, password))

if __name__ == "__main__":
    unittest.main()
