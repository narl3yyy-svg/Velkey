"""Custom icons that are too dark for the dark theme get a light backdrop."""
import unittest

from tests.helpers import HAVE_GI, needs_gi

if HAVE_GI:
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib
    from velkey import theme

N = 24


def icon(fn):
    buf = bytearray(N * N * 4)
    for y in range(N):
        for x in range(N):
            c = fn(x, y)
            if c:
                buf[(y * N + x) * 4:(y * N + x) * 4 + 4] = bytes(c)
    return GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(bytes(buf)), GdkPixbuf.Colorspace.RGB, True, 8, N, N, N * 4)


def pixel(pb, x, y):
    i = y * pb.get_rowstride() + x * pb.get_n_channels()
    return tuple(pb.get_pixels()[i:i + 4])


@needs_gi
class TestDarkIcons(unittest.TestCase):
    def test_black_logo_gets_a_light_tile(self):
        dark = icon(lambda x, y: (0, 0, 0, 255) if abs(x - y) < 2 else None)
        out = theme.visible_on_dark(dark)
        self.assertIsNot(out, dark)
        self.assertEqual(pixel(out, 12, 3)[:3], theme.BACKDROP)  # tile behind the logo
        self.assertEqual(pixel(out, 0, 0)[3], 0)                  # rounded corner stays transparent
        self.assertLess(sum(pixel(out, 12, 12)[:3]), 100)         # the logo itself is still drawn

    def test_visible_icons_are_unchanged(self):
        for c in ((215, 30, 40, 255), (0, 156, 222, 255), (240, 240, 240, 255)):
            pb = icon(lambda x, y, c=c: c if 4 < x < 20 and 4 < y < 20 else None)
            self.assertIs(theme.visible_on_dark(pb), pb)

    def test_dark_tile_with_light_text_is_unchanged(self):
        pb = icon(lambda x, y: (240, 240, 240, 255) if 8 < y < 15 and 5 < x < 19 else
                  (12, 12, 12, 255) if 3 < x < 20 and 1 < y < 22 else None)
        self.assertIs(theme.visible_on_dark(pb), pb)

    def test_loaded_from_png(self):
        dark = icon(lambda x, y: (40, 40, 40, 255) if 6 < x < 18 and 6 < y < 18 else None)
        ok, data = dark.save_to_bufferv("png", [], [])
        out = theme.pixbuf_from_bytes(data, 16)
        self.assertEqual((out.get_width(), out.get_height()), (16, 16))
        self.assertEqual(pixel(out, 8, 1)[:3], theme.BACKDROP)


if __name__ == "__main__":
    unittest.main()
