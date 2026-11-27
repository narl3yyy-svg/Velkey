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

    def test_find_duplicates(self):
        b = fast_db(self.path("b.kdbx"))
        same = demo_entry(group(b, "General"), password="demo-password-2")  # identical details
        other = demo_entry(group(self.a, "Internet"), title="Demo Forum", user="alex_demo")
        dups = kdbx.find_duplicates([self.e, other], b)
        self.assertEqual([(d[0].title, d[1].uuid, d[2]) for d in dups], [("Example Shop", same.uuid, True)])

    def test_same_entry_with_new_password_is_flagged(self):
        b = fast_db(self.path("b.kdbx"))
        [c] = kdbx.copy_entries([self.e], b.root_group)
        self.e.password = "demo-password-3"  # changed after it was copied
        [(_e, have, same)] = kdbx.find_duplicates([self.e], b)
        self.assertIs(same, False)
        self.assertEqual(have.uuid, c.uuid)

    def test_recycle_bin_does_not_count(self):
        b = fast_db(self.path("b.kdbx"))
        [c] = kdbx.copy_entries([self.e], b.root_group)
        c.move_to(b.recycle_bin(create=True))
        self.assertEqual(kdbx.find_duplicates([self.e], b), [])

    def test_original_untouched(self):
        b = fast_db(self.path("b.kdbx"))
        [c] = kdbx.copy_entries([self.e], b.root_group)
        c.password = "changed-in-copy"
        self.assertEqual(self.e.password, "demo-password-2")


class TestPendingDeletion(TempDir):
    """Deletions can be undone, and left out of automatic saves."""

    def setUp(self):
        super().setUp()
        self.db = fast_db(self.path("demo.kdbx"))
        self.g = group(self.db, "Internet")
        self.keep = demo_entry(self.g, "Example Shop")
        self.gone = demo_entry(self.g, "Demo Forum", "alex_demo", "demo-2")
        self.db.save()

    def titles(self, db=None):
        return sorted(e.title for e in (db or self.db).all_entries())

    def on_disk(self):
        return sorted(e.title for e in kdbx.Database.open(self.path("demo.kdbx"), "demo-master").all_entries())

    def test_recycle_and_undo_restores_exact_position(self):
        before = kdbx.ET.tostring(self.db.root)
        log = kdbx.ChangeLog(self.db)
        log.move(self.gone, log.recycle_bin())
        self.assertEqual(self.titles(), ["Example Shop"])
        log.revert()
        self.assertEqual(kdbx.ET.tostring(self.db.root), before)

    def test_permanent_delete_and_undo(self):
        before = kdbx.ET.tostring(self.db.root)
        log = kdbx.ChangeLog(self.db)
        log.remove(self.gone)
        self.assertIsNotNone(self.db.root.find("Root/DeletedObjects/DeletedObject"))
        log.revert()
        self.assertEqual(kdbx.ET.tostring(self.db.root), before)

    def test_autosave_leaves_deletion_out(self):
        log = kdbx.ChangeLog(self.db)
        log.move(self.gone, log.recycle_bin())
        demo_entry(self.g, "Sample Mail", "alex@example.org", "demo-3")  # added after the delete
        kdbx.save_without(self.db, [log])
        self.assertEqual(self.on_disk(), ["Demo Forum", "Example Shop", "Sample Mail"])
        self.assertEqual(self.titles(), ["Example Shop", "Sample Mail"])  # still deleted in memory
        self.assertTrue(self.db.modified)
        self.db.save()  # explicit save writes the deletion
        self.assertEqual(self.on_disk(), ["Example Shop", "Sample Mail"])

    def test_two_deletions_undo_in_order(self):
        a, b = kdbx.ChangeLog(self.db), kdbx.ChangeLog(self.db)
        a.remove(self.gone)
        b.move(self.keep, b.recycle_bin())
        kdbx.save_without(self.db, [a, b])
        self.assertEqual(self.on_disk(), ["Demo Forum", "Example Shop"])
        b.revert()
        self.assertEqual(self.titles(), ["Example Shop"])
        a.revert()
        self.assertEqual(self.titles(), ["Demo Forum", "Example Shop"])


if __name__ == "__main__":
    unittest.main()
