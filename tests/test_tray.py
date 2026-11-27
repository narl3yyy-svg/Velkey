"""The tray icon must be cheap to serve: the panel re-reads it often."""
import time
import unittest

from tests.helpers import HAVE_GI, needs_gi

if HAVE_GI:
    from velkey import tray


@needs_gi
class TestTray(unittest.TestCase):
    def test_icon_is_built_once_and_served_fast(self):
        t = tray.Tray(lambda: None, [])
        first = t._sni_prop(None, None, None, None, "IconPixmap")
        self.assertEqual(first.get_type_string(), "a(iiay)")
        self.assertGreater(first.n_children(), 0)
        start = time.perf_counter()
        for _ in range(200):
            self.assertIs(t._sni_prop(None, None, None, None, "IconPixmap"), first)
            t._sni_prop(None, None, None, None, "ToolTip")
        self.assertLess(time.perf_counter() - start, 0.2)

    def test_unchanged_tooltip_is_not_resent(self):
        t = tray.Tray(lambda: None, [])
        sent = []
        t.ok, t.bus = True, type("Bus", (), {"emit_signal": lambda self, *a: sent.append(a)})()
        t.set_tooltip("Velkey — Demo")
        t.set_tooltip("Velkey — Demo")
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
