"""Database files: round trips, wrong keys, tampering, copy / export / import."""
import os
import unittest
from pathlib import Path

from tests.helpers import TempDir, demo_entry, fast_db, group
from velkey import kdbx


class TestRoundTrip(TempDir):
    def _roundtrip(self, cipher, kdf):
        path = self.path("demo.kdbx")
        db = fast_db(path)
        db.cipher = cipher
        if kdf == kdbx.KDF_AES:
            db.set_kdf(kdf, rounds=1000)
        else:
            db.set_kdf(kdf, iterations=2, memory=1024 * 1024, parallelism=2)
        e = demo_entry(group(db, "Internet"))
        e.notes = "Demo notes — ünïcödé ✓"
        e.set("Recovery code", "demo-recovery-0000", protected=True)
        e.set_attachments([("demo.txt", b"demo attachment")])
        db.save()
        back = kdbx.Database.open(path, "demo-master")
        [b] = back.all_entries()
        self.assertEqual((b.title, b.username, b.password, b.url),
                         ("Example Shop", "alex@example.com", "demo-password-1", "https://shop.example.com/login"))
        self.assertEqual(b.notes, "Demo notes — ünïcödé ✓")
        self.assertEqual(b.get("Recovery code"), "demo-recovery-0000")
        self.assertEqual(b.attachments(), [("demo.txt", b"demo attachment")])
        return path

    def test_aes_argon2d(self):
        self._roundtrip(kdbx.CIPHER_AES, kdbx.KDF_ARGON2D)

    def test_chacha_argon2id(self):
        self._roundtrip(kdbx.CIPHER_CHACHA, kdbx.KDF_ARGON2ID)

    def test_aes_kdf(self):
        self._roundtrip(kdbx.CIPHER_AES, kdbx.KDF_AES)

    def test_file_is_encrypted_and_private(self):
        path = self._roundtrip(kdbx.CIPHER_AES, kdbx.KDF_ARGON2D)
        raw = Path(path).read_bytes()
        for secret in (b"demo-password-1", b"alex@example.com", b"Example Shop", b"demo attachment"):
            self.assertNotIn(secret, raw)
        self.assertEqual(os.stat(path).st_mode & 0o077, 0, "new database must not be readable by others")

    def test_wrong_password(self):
        path = self.path("demo.kdbx")
        fast_db(path).save()
        with self.assertRaises(kdbx.InvalidKey):
            kdbx.Database.open(path, "not-the-password")

    def test_tampering_is_detected(self):
        path = self.path("demo.kdbx")
        db = fast_db(path)
        demo_entry(group(db, "Internet"))
        db.save()
        raw = bytearray(Path(path).read_bytes())
        raw[-40] ^= 0x01  # flip one bit in the encrypted payload
        Path(path).write_bytes(bytes(raw))
        with self.assertRaises(Exception):
            kdbx.Database.open(path, "demo-master")

    def test_keyfile(self):
        path, kf = self.path("demo.kdbx"), self.path("demo.keyx")
        kdbx.create_keyfile(kf)
        fast_db(path, "demo-master", kf).save()
        self.assertIsNotNone(kdbx.Database.open(path, "demo-master", kf))
        with self.assertRaises(kdbx.InvalidKey):
            kdbx.Database.open(path, "demo-master")


class TestCopyExportImport(TempDir):
    def setUp(self):
        super().setUp()
        self.a = fast_db(self.path("a.kdbx"), name="Demo A")
        self.e = demo_entry(group(self.a, "Internet"))
        self.e.set_attachments([("demo.txt", b"demo attachment")])
        self.e.backup()
        self.e.password = "demo-password-2"

    def test_export_then_import(self):
        out = self.path("export.kdbx")
        kdbx.export_entries([self.e], out, "demo-export")
        self.assertEqual(os.stat(out).st_mode & 0o077, 0)
        x = kdbx.Database.open(out, "demo-export")
        self.assertEqual(x.root_group.groups, [])  # no default groups in an export
        b = fast_db(self.path("b.kdbx"), name="Demo B")
        [c] = kdbx.copy_entries(x.all_entries(), b.root_group)
        self.assertEqual((c.title, c.password), ("Example Shop", "demo-password-2"))
        self.assertEqual(c.attachments(), [("demo.txt", b"demo attachment")])
        self.assertEqual(len(c.history()), 1)

    def test_copy_keeps_group_path_and_new_uuid_on_clash(self):
        b = fast_db(self.path("b.kdbx"), name="Demo B")
        [first] = kdbx.copy_entries([self.e], b.root_group)
        self.assertEqual(first.parent.name, "Internet")
        self.assertEqual(first.uuid, self.e.uuid)
        [second] = kdbx.copy_entries([self.e], b.root_group)
        self.assertNotEqual(second.uuid, first.uuid)
        b.save()
        self.assertEqual(len(kdbx.Database.open(self.path("b.kdbx"), "demo-master").all_entries()), 2)

    def test_original_untouched(self):
        b = fast_db(self.path("b.kdbx"))
        [c] = kdbx.copy_entries([self.e], b.root_group)
        c.password = "changed-in-copy"
        self.assertEqual(self.e.password, "demo-password-2")


if __name__ == "__main__":
    unittest.main()
