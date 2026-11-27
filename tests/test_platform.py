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
class TestMacSwitches(unittest.TestCase):
    def test_linux_only_parts_switch_off(self):
        from velkey import a11y, autotype
        old = autotype.IS_MAC
        try:
            autotype.IS_MAC = True
            self.assertEqual(autotype.session_type(), "macos")
            self.assertIsNone(autotype.AutoTyper({}).backend())
            hk = autotype.GlobalHotkey(None, "Ctrl+Alt+Shift+Z", lambda: None)
            hk.start()
            self.assertEqual(hk.backend, "unavailable")
        finally:
            autotype.IS_MAC = old
        c = a11y.FocusClient.__new__(a11y.FocusClient)
        c.disabled = True
        self.assertFalse(c.field_from_thread(1).known)


if __name__ == "__main__":
    unittest.main()
