"""Touch ID module, with a stand-in for the Swift helper (no Mac needed)."""
import os
import stat
import subprocess
import tempfile
import unittest

from tests.helpers import ROOT
from velkey import touchid

FAKE = """#!/bin/sh
echo "$*" >> "%(dir)s/argv"
case "$1" in
  available) exit 0;;
  store) cat > "%(dir)s/keychain"; exit 0;;
  get) a=$(cat "%(dir)s/answer" 2>/dev/null || echo ok); [ "$a" = cancel ] && exit 2; [ "$a" = none ] && exit 4
       [ -f "%(dir)s/keychain" ] || exit 3; cat "%(dir)s/keychain"; exit 0;;
  delete) rm -f "%(dir)s/keychain"; exit 0;;
esac
"""


class TestTouchID(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.fake = os.path.join(self.dir, "velkey-touchid")
        with open(self.fake, "w") as f:
            f.write(FAKE % {"dir": self.dir})
        os.chmod(self.fake, stat.S_IRWXU)
        self.saved = (touchid.helper, touchid._available)
        touchid.helper = lambda: self.fake
        touchid._available = None

    def tearDown(self):
        touchid.helper, touchid._available = self.saved
        self.tmp.cleanup()

    def answer(self, a):
        with open(os.path.join(self.dir, "answer"), "w") as f:
            f.write(a)

    def test_store_and_unlock_never_put_the_password_on_the_command_line(self):
        self.assertTrue(touchid.available())
        self.assertTrue(touchid.store("/demo/Demo.kdbx", "demo-master-pw"))
        self.assertEqual(touchid.unlock("/demo/Demo.kdbx", "unlock Demo"), ("ok", "demo-master-pw"))
        with open(os.path.join(self.dir, "argv")) as f:
            argv = f.read()
        self.assertNotIn("demo-master-pw", argv)
        self.assertNotIn("/demo/Demo.kdbx", argv)   # only a hash of the path

    def test_statuses(self):
        touchid.store("/demo/Demo.kdbx", "pw")
        self.answer("cancel")
        self.assertEqual(touchid.unlock("/demo/Demo.kdbx", "r"), ("cancelled", None))
        self.answer("none")
        self.assertEqual(touchid.unlock("/demo/Demo.kdbx", "r")[0], "unavailable")
        self.answer("ok")
        touchid.forget("/demo/Demo.kdbx")
        self.assertEqual(touchid.unlock("/demo/Demo.kdbx", "r"), ("missing", None))

    def test_not_offered_without_helper(self):
        touchid.helper = lambda: ""
        touchid._available = None
        self.assertFalse(touchid.available())


class TestHelperBuild(unittest.TestCase):
    """tools/macos-env.sh builds the helper once and rebuilds it when the source changes."""

    def test_build_step(self):
        with tempfile.TemporaryDirectory() as d:
            bindir = os.path.join(d, "bin")
            os.mkdir(bindir)
            with open(os.path.join(bindir, "xcrun"), "w") as f:   # stand-in Swift compiler
                f.write('#!/bin/sh\necho built >> "%s/builds"\n'
                        'while [ "$1" != "-o" ]; do shift; done; printf "#!/bin/sh\\n" > "$2"; chmod +x "$2"\n' % d)
            os.chmod(os.path.join(bindir, "xcrun"), stat.S_IRWXU)
            out = os.path.join(d, "out", "velkey-touchid")
            script = ('. "%s/tools/macos-env.sh"; velkey_mac_touchid "%s/tools/macos" "%s"; echo "$VELKEY_TOUCHID"'
                      % (ROOT, ROOT, out))
            env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"])
            for _ in range(2):
                r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
                self.assertEqual(r.stdout.strip(), out)
            with open(os.path.join(d, "builds")) as f:
                self.assertEqual(f.read().count("built"), 1)   # second run reused it


if __name__ == "__main__":
    unittest.main()
