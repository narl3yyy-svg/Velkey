"""Crypto primitives against published test vectors."""
import unittest

from tests.helpers import ROOT  # noqa: F401  (sets sys.path)
from velkey import crypto

h = bytes.fromhex


class TestVectors(unittest.TestCase):
    def test_aes256_cbc_nist(self):
        # NIST SP 800-38A, F.2.5 (first block)
        key = h("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
        iv = h("000102030405060708090a0b0c0d0e0f")
        pt = h("6bc1bee22e409f96e93d7e117393172a")
        ct = crypto.aes_cbc_encrypt(key, iv, pt)
        self.assertEqual(ct[:16].hex(), "f58c4c04d6e5f1ba779eabfb5f7bfbd6")
        self.assertEqual(crypto.aes_cbc_decrypt(key, iv, ct), pt)

    def test_aes_cbc_rejects_wrong_key(self):
        ct = crypto.aes_cbc_encrypt(b"k" * 32, b"i" * 16, b"secret data")
        try:
            out = crypto.aes_cbc_decrypt(b"K" * 32, b"i" * 16, ct)
        except Exception:
            return  # bad padding detected
        self.assertNotEqual(out, b"secret data")

    def test_chacha20_rfc8439(self):
        # RFC 8439, section 2.4.2
        pt = (b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the "
              b"future, sunscreen would be it.")
        ct = crypto.chacha20(bytes(range(32)), h("000000000000004a00000000"), pt, counter=1)
        self.assertEqual(ct[:16].hex(), "6e2e359a2568f98041ba0728dd0d6981")
        self.assertEqual(ct[-4:].hex(), "5e42874d")
        self.assertEqual(crypto.chacha20(bytes(range(32)), h("000000000000004a00000000"), ct, counter=1), pt)

    def test_argon2_rfc9106(self):
        # RFC 9106, sections 5.1 and 5.3
        args = (b"\x01" * 32, b"\x02" * 16, 3, 32, 4)
        kw = dict(secret=b"\x03" * 8, ad=b"\x04" * 12)
        self.assertEqual(crypto.argon2(*args, variant="d", **kw).hex(),
                         "512b391b6f1162975371d30919734294f868e3be3984f3c1a13a4db9fabe4acb")
        self.assertEqual(crypto.argon2(*args, variant="id", **kw).hex(),
                         "0d640df58d78766c08c037a34a8b53c9d01ef0452d75b65eb52520e96b01e659")

    def test_salsa20_stream_is_symmetric(self):
        data = "demo-password-1".encode()
        enc = crypto.Salsa20Stream(b"k" * 32).process(data)
        self.assertNotEqual(enc, data)
        self.assertEqual(crypto.Salsa20Stream(b"k" * 32).process(enc), data)

    def test_random_bytes(self):
        a, b = crypto.random_bytes(32), crypto.random_bytes(32)
        self.assertEqual(len(a), 32)
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
