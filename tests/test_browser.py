"""Reading the page URL from browser session files (demo data only)."""
import struct
import unittest

from tests.helpers import ROOT  # noqa: F401
from velkey import browser


def snss(*commands):
    out = b"SNSS" + struct.pack("<i", 3)
    for cid, body in commands:
        out += struct.pack("<H", len(body) + 1) + bytes([cid]) + body
    return out


def nav(tab, idx, url, title):
    u = url.encode()
    t = title.encode("utf-16-le")
    p = struct.pack("<ii", tab, idx) + struct.pack("<i", len(u)) + u + b"\0" * ((-len(u)) % 4)
    p += struct.pack("<i", len(title)) + t + b"\0" * ((-len(t)) % 4)
    return 6, struct.pack("<i", len(p)) + p


class TestSite(unittest.TestCase):
    def test_registrable_domain(self):
        self.assertEqual(browser.site("https://login.example.com/x"), "example.com")
        self.assertEqual(browser.site("https://www.example.co.uk/"), "example.co.uk")
        self.assertEqual(browser.site("https://user@example.org:8443/a"), "example.org")

    def test_lookalikes_are_different_sites(self):
        self.assertNotEqual(browser.site("https://example.com.evil.test/"), browser.site("https://example.com/"))
        self.assertNotEqual(browser.site("https://examp1e.com/"), browser.site("https://example.com/"))

    def test_page_titles_only_strip_a_trailing_browser_name(self):
        self.assertEqual(browser._page_titles("Example Mail - Helium"), ["Example Mail - Helium", "Example Mail"])
        self.assertEqual(browser._page_titles("Login - Example Bank - Evil"), ["Login - Example Bank - Evil"])


class TestChromiumSession(unittest.TestCase):
    def test_current_page_and_selected_tab(self):
        data = snss(
            (0, struct.pack("<ii", 1, 10)),            # tab 10 in window 1
            (0, struct.pack("<ii", 1, 11)),            # tab 11 in window 1
            (2, struct.pack("<ii", 10, 0)),
            (2, struct.pack("<ii", 11, 1)),
            nav(10, 0, "https://shop.example.com/", "Example Shop"),
            nav(10, 1, "https://shop.example.com/cart", "Cart - Example Shop"),
            (7, struct.pack("<ii", 10, 1)),            # tab 10 shows its 2nd page
            nav(11, 0, "https://login.example.org/", ""),  # title not saved
            (8, struct.pack("<ii", 1, 1)),             # window 1 has tab index 1 selected
        )
        tabs = browser._chromium_tabs(data)
        self.assertIn(("Cart - Example Shop", "https://shop.example.com/cart", False), tabs)
        self.assertIn(("", "https://login.example.org/", True), tabs)

    def test_garbage_is_ignored(self):
        self.assertEqual(browser._chromium_tabs(b"not a session file"), [])
        self.assertIsInstance(browser._chromium_tabs(b"SNSS\x03\0\0\0\xff\xff\x06"), list)


class TestLz4(unittest.TestCase):
    def test_literals_and_match(self):
        self.assertEqual(browser._lz4_block(b"\x50hello"), b"hello")
        # "abc" then copy 9 bytes from 3 back -> "abcabcabcabc"
        self.assertEqual(browser._lz4_block(b"\x35abc\x03\x00"), b"abcabcabcabc")

    def test_title_names_site(self):
        self.assertTrue(browser._title_names_site("Login - Example Bank", "https://login.examplebank.com/"))
        self.assertFalse(browser._title_names_site("Login - Example Bank", "https://evil.test/"))


if __name__ == "__main__":
    unittest.main()
