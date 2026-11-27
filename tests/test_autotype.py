"""Auto-Type safety: which windows match, and the password-box rule."""
import unittest

from tests.helpers import HAVE_GI, TempDir, demo_entry, fast_db, group, needs_gi

if HAVE_GI:
    from gi.repository import GLib
    from velkey import autotype
    from velkey.a11y import FieldInfo


@needs_gi
class TestMatching(TempDir):
    def setUp(self):
        super().setUp()
        self.db = fast_db(self.path("demo.kdbx"))
        self.e = demo_entry(group(self.db, "Internet"), url="https://shop.example.com/login")

    def match(self, title, url=""):
        return autotype.match_sequence(self.e, title, True, url) is not None

    def test_real_url_must_match_the_entry_site(self):
        self.assertTrue(self.match("Sign in - Helium", "https://shop.example.com/signin"))
        self.assertTrue(self.match("Sign in - Helium", "https://accounts.example.com/"))  # same site
        self.assertFalse(self.match("Example Shop - Helium", "https://example.com.evil.test/"))
        self.assertFalse(self.match("Example Shop - Helium", "https://examp1e.com/"))

    def test_lookalike_title_is_not_enough_when_url_known(self):
        self.assertFalse(self.match("Example Shop login", "https://phish.test/"))

    def test_title_fallback_without_url(self):
        self.assertTrue(self.match("Example Shop — Mozilla Firefox"))
        self.assertFalse(self.match("Some Other Site"))

    def test_url_window_rules(self):
        self.e.set_associations([("https://login.example.org/users/*", "")])
        self.assertTrue(self.match("Login", "https://login.example.org/users/sign_in?x=1"))
        self.assertFalse(self.match("Login", "https://evil.test/?https://login.example.org/users/"))
        self.e.set_associations([("??:URL:https://login.example.org/*", "")])
        self.assertTrue(self.match("Login", "https://login.example.org/a"))
        self.assertFalse(self.match("Login", ""))

    def test_disabled_entries_are_never_offered(self):
        self.e.autotype_enabled = False
        self.assertEqual(autotype.find_matches(self.db, "Example Shop", True, "https://shop.example.com/"), [])


class FakeBackend:
    def __init__(self):
        self.typed = []

    def available(self):
        return True

    def prepare(self, cb):
        cb(True, None)

    def run(self, seg):
        self.typed.append([e for e in seg if e[0] == "press"])


@needs_gi
class TestPasswordBox(TempDir):
    """The password is only typed into a password box (when the app reports fields)."""

    def setUp(self):
        super().setUp()
        db = fast_db(self.path("demo.kdbx"))
        self.e = demo_entry(group(db, "Internet"), user="u", password="p")
        self.typer = autotype.AutoTyper({"autotype_delay": 0})
        self.typer.config = type("C", (), {"get": lambda s, k, d=None: d, "set": lambda *a: None})()
        self.be = self.typer._backend = FakeBackend()

    def run_seq(self, first, later):
        loop = GLib.MainLoop()
        res = {}

        def done(err):
            res["err"] = err
            loop.quit()
        self.typer.type_sequence(self.e, "{USERNAME}{TAB}{PASSWORD}{ENTER}", done,
                                 field_check=lambda wait: later if wait else first)
        GLib.timeout_add(5000, loop.quit)
        loop.run()
        return res.get("err"), len(self.be.typed)

    def test_username_box_then_password_box(self):
        err, segments = self.run_seq(FieldInfo(True, False, True), FieldInfo(True, True, True))
        self.assertIsNone(err)
        self.assertEqual(segments, 2)

    def test_password_never_goes_into_a_normal_field(self):
        err, segments = self.run_seq(FieldInfo(True, False, True), FieldInfo(True, False, True))
        self.assertIn("password was not", err)
        self.assertEqual(segments, 1)  # only the user name part

    def test_already_in_password_box_skips_user_name(self):
        err, segments = self.run_seq(FieldInfo(True, True, True), FieldInfo(True, True, True))
        self.assertIsNone(err)
        self.assertEqual(segments, 1)

    def test_unknown_field_types_like_keepass(self):
        err, segments = self.run_seq(FieldInfo(False), FieldInfo(False))
        self.assertIsNone(err)
        self.assertEqual(segments, 2)


@needs_gi
class TestKeyChoice(unittest.TestCase):
    """Typing on Wayland: characters must go out on normal keyboard keys."""

    def test_dollar_uses_shift_4_not_the_special_dollar_key(self):
        from gi.repository import Gdk

        class Key:
            def __init__(self, evdev, level):
                self.keycode, self.group, self.level = evdev + 8, 0, level

        layout = {   # like a pc105 layout that also has KEY_DOLLAR / KEY_KPLEFTPAREN
            Gdk.keyval_from_name("dollar"): [Key(434, 0), Key(5, 1)],
            Gdk.keyval_from_name("parenleft"): [Key(179, 0), Key(10, 1)],
            Gdk.keyval_from_name("less"): [Key(86, 0), Key(51, 1)],
            Gdk.keyval_from_name("a"): [Key(30, 0)],
            Gdk.keyval_from_name("Shift_L"): [Key(42, 0)],
        }

        class Keymap:
            def get_entries_for_keyval(self, ks):
                keys = layout.get(ks, [])
                return bool(keys), keys
        real = Gdk.Keymap.get_for_display
        Gdk.Keymap.get_for_display = staticmethod(lambda display: Keymap())
        try:
            typer = autotype.PortalTyper({})
            events = [("press", Gdk.keyval_from_name(n), frozenset()) for n in ("dollar", "parenleft", "less", "a")]
            out = [(main, held) for _, main, held in typer.compile(events)]
        finally:
            Gdk.Keymap.get_for_display = real
        self.assertEqual(out, [(("code", 5), [("code", 42)]), (("code", 10), [("code", 42)]),
                               (("code", 51), [("code", 42)]), (("code", 30), [])])


@needs_gi
class TestSequence(unittest.TestCase):
    def test_braces_and_modifiers(self):
        acts = autotype.parse_sequence("{{}x{}}^a~", None)
        self.assertEqual(acts[0], ("char", "{", frozenset()))
        self.assertEqual(acts[-1][0:2], ("key", "Return"))


@needs_gi
class TestAccessibilityHelper(unittest.TestCase):
    def test_hung_helper_never_blocks(self):
        import os
        import signal
        import time
        from velkey import a11y
        c = a11y.FocusClient()
        try:
            if c._proc is None:
                self.skipTest("helper could not start")
            os.kill(c._proc.pid, signal.SIGSTOP)
            t = time.time()
            info = c.field_from_thread(1, timeout=0.5)
            self.assertLess(time.time() - t, 2)
            self.assertFalse(info.known)
        finally:
            c.stop()


if __name__ == "__main__":
    unittest.main()
