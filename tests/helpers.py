"""Shared helpers for the tests. All data here is made up (demo only)."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from velkey import kdbx  # noqa: E402

try:
    import gi
    gi.require_version("Gdk", "3.0")
    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib  # noqa: F401
    HAVE_GI = True
except (ImportError, ValueError):
    HAVE_GI = False

needs_gi = unittest.skipUnless(HAVE_GI, "PyGObject / GTK 3 not installed")


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="velkey-test-")
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def path(self, name):
        return os.path.join(self.dir, name)


def fast_db(path, password="demo-master", keyfile=None, name="Demo"):
    """A new database with a cheap key derivation so tests run fast."""
    db = kdbx.Database.create(path, password, keyfile, name)
    db.set_kdf(kdbx.KDF_ARGON2D, iterations=2, memory=1024 * 1024, parallelism=1)
    return db


def group(db, name):
    return next(g for g in db.root_group.groups if g.name == name)


def demo_entry(g, title="Example Shop", user="alex@example.com", password="demo-password-1",
               url="https://shop.example.com/login"):
    e = g.add_entry()
    e.title, e.username, e.password, e.url = title, user, password, url
    return e
